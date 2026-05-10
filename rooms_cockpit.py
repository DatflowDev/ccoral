"""
CCORAL Rooms — Multi-Room Cockpit (Phase 11)
=============================================

Textual ``App`` for observing + interjecting across N concurrent rooms.

Discovers all live rooms under ``~/.ccoral/rooms/`` (per the Phase 3
state-dir contract), spawns one background tailer per room that follows
``transcript.jsonl``, and routes operator input back through the per-room
control FIFO contract from Phase 6.

Read-only on lifecycle. The cockpit does not call ``run_room``, does not
spawn ``relay_loop``, does not start tmux sessions, does not write to
``transcript.jsonl``. The only outbound traffic is JSON lines on the
per-room control sink, written via ``room.write_control_event`` — the
same producer-side helper Phase 6's watch / serve sidecars use. Quitting
the cockpit (``Ctrl+C`` / ``q``) leaves every room running.

Public surface (consumed by the ``ccoral rooms`` CLI verb wired in C6):

    class RoomsCockpit(App)
        - CSS_PATH = "rooms_cockpit.tcss"
        - BINDINGS: Ctrl+N/P cycle tabs, Ctrl+U toggle unified mode,
          Ctrl+L clear activity badges, Ctrl+C/q quit.
        - compose() yields TabbedContent with one TabPane + RichLog per
          discovered room, plus a hidden unified-log RichLog and the
          input prompt.

    discover_room_ids(base) -> list[str]
        Resolve the live + recently-stopped room ids the cockpit will tail.

C1 ships the tabs skeleton: discovery, compose with one TabPane per
room, Ctrl+N/P navigation, and a no-op prompt. The background tailers,
activity badges, unified mode, input dispatch, and stopped/broken
handling land in C2..C5.

References (verified against installed textual==8.2.5):

  - App + BINDINGS:    https://textual.textualize.io/tutorial/
  - TabbedContent:     https://textual.textualize.io/widgets/tabbed_content/
  - RichLog:           https://textual.textualize.io/widgets/rich_log/
  - @work(thread):     https://textual.textualize.io/guide/workers/
  - App.call_from_thread:
                       https://textual.textualize.io/api/app/#textual.app.App.call_from_thread
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import (
    Footer,
    Header,
    Input,
    RichLog,
    TabbedContent,
    TabPane,
)


# ---------------------------------------------------------------------------
# Slot-color resolution. Mirrors room_watch.py so a per-room line in the
# cockpit looks identical to the same line under `ccoral room watch`.
# ---------------------------------------------------------------------------

_SLOT_CLASS = {1: "speaker-1", 2: "speaker-2"}


def _classify(rec: dict, *, user_name: str) -> str:
    """Return the css class for a record: speaker-1 / speaker-2 / system / host."""
    slot = rec.get("slot")
    if isinstance(slot, int) and slot in _SLOT_CLASS:
        return _SLOT_CLASS[slot]
    name = (rec.get("name") or "").upper()
    if name == "SYSTEM" or name == "ROOM":
        return "system"
    if name == user_name.upper():
        return "host"
    return "system"


def _escape_markup(text: str) -> str:
    return text.replace("[", r"\[")


# ---------------------------------------------------------------------------
# Discovery — list rooms the cockpit should tail.
#
# Late-imports `room._list_room_dirs` so this module stays importable in
# tooling contexts that don't pull room.py's heavy graph (yaml, profiles,
# subprocess). The tests exercise discovery via the `base` override.
# ---------------------------------------------------------------------------


def discover_room_ids(base: "Path | None" = None) -> list[str]:
    """Return room ids under the per-room state archive, newest first.

    Wraps ``room._list_room_dirs`` so callers don't have to know the
    archive path. Includes both ``state: live`` and ``state: stopped``
    rooms — the cockpit shows recently-ended rooms with a (stopped)
    badge for a grace window before removing them (Phase 11 task 7,
    landing in C5). Filtering by state happens at render time, not
    here, so the discovery surface stays uniform.
    """
    from room import _list_room_dirs

    return [d.name for d in _list_room_dirs(base)]


# ---------------------------------------------------------------------------
# Custom messages — the only thread-safe way to mutate App state from a
# background @work(thread=True) tailer. The worker posts a Message; the
# App's main loop dispatches to the @on handler which owns the widget
# mutation. Same pattern Phase 9 used for EventReady.
# ---------------------------------------------------------------------------


class RoomLine(Message):
    """A new transcript line landed for ``room_id``.

    Carries the parsed record so the App handler can render with the
    same slot-color mapping room_watch.py uses, and update the
    last-active-room tracker that C4's unified-mode input dispatch
    targets when no `/room` prefix is present.
    """

    def __init__(self, room_id: str, record: dict) -> None:
        super().__init__()
        self.room_id = room_id
        self.record = record


class RoomActivity(Message):
    """A worker wrote a line to a non-active room. Tab label gains a
    `+` badge until the operator focuses the tab (or hits Ctrl+L to
    clear all badges).

    Posted from the App's main thread inside the RoomLine handler —
    the activity decision needs the active-tab state which only the
    main thread can read consistently. Kept as a Message rather than
    a direct mutation so the badge-update path is auditable in tests
    via the same Pilot.pause() rhythm everywhere else uses.
    """

    def __init__(self, room_id: str) -> None:
        super().__init__()
        self.room_id = room_id


# ---------------------------------------------------------------------------
# RoomsCockpit — the App itself.
# ---------------------------------------------------------------------------


class RoomsCockpit(App):
    """Multi-room observer + interjector. Read-only on lifecycle."""

    CSS_PATH = "rooms_cockpit.tcss"
    TITLE = "ccoral rooms"

    BINDINGS = [
        # priority=True so Ctrl+C / q exit even when the Input has focus.
        Binding("ctrl+n", "next_tab", "Next room", priority=True),
        Binding("ctrl+p", "prev_tab", "Prev room", priority=True),
        Binding("ctrl+u", "toggle_unified", "Unified ↔ tabs"),
        Binding("ctrl+l", "clear_badges", "Clear badges"),
        Binding("ctrl+c,q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        *,
        room_ids: "list[str] | None" = None,
        base: "Path | None" = None,
        user_name: str = "CASSIUS",
        poll_interval: float = 0.25,
        **kwargs: Any,
    ) -> None:
        """Construct the cockpit.

        ``room_ids`` lets the CLI (or a test) inject a fixed roster —
        used by ``ccoral room watch <id>`` (single-tab subset, wired in
        C6) and by the C7 fixture-driven tests. When None we discover
        from ``~/.ccoral/rooms/`` (or ``base`` if supplied for tests).

        ``base`` is the per-room state archive root. None resolves to
        the production default (``~/.ccoral/rooms``); tests pass a
        tmp_path so transcript writers and the cockpit agree on where
        the JSONL files live.

        ``user_name`` colors host lines and is forwarded to the slot-
        classifier; matches room_watch.py's contract.

        ``poll_interval`` controls how often each tailer's loop wakes
        when its transcript is idle. 250ms matches the watch sidecar.
        """
        super().__init__(**kwargs)
        self._explicit_room_ids = room_ids
        self._discovery_base = base
        self.user_name = user_name
        self.poll_interval = poll_interval
        # Resolved at compose() so on_mount can act on the same list.
        self.room_ids: list[str] = []
        # Unified-mode flag (Ctrl+U toggles). Tabs mode is the default.
        self.unified_mode: bool = False
        # Per-room read cursors for the tailers. Bytes already consumed
        # from each transcript so a worker restart resumes cleanly.
        self._cursors: dict[str, int] = {}
        # Carry partial JSONL lines between reads — same byte-buffer
        # discipline room_watch.py uses to avoid mid-line tears.
        self._line_bufs: dict[str, bytes] = {}
        # The most recently active room — the unified-mode dispatcher
        # in C4 targets this when the operator types plain text without
        # a `/room <id>` prefix.
        self.last_active_room: "str | None" = None
        # Per-room activity badge state. Set when a worker writes to a
        # non-active tab; cleared when the operator focuses the tab or
        # hits Ctrl+L. Public so tests can assert without scraping the
        # tab label string.
        self.activity_badges: set[str] = set()

    # ─── compose / lifecycle ───────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """Build the layout once at startup.

        Tabs mode shows one ``TabPane`` per discovered room, each with
        its own ``RichLog``. The unified-log is composed alongside but
        hidden until Ctrl+U toggles in (C4). The prompt docks at the
        bottom; Ctrl+N/P cycling lives in actions below.

        If no rooms are discovered we still compose a single placeholder
        tab so the App has a valid TabbedContent surface — the operator
        sees the empty state rather than a crash.
        """
        if self._explicit_room_ids is not None:
            self.room_ids = list(self._explicit_room_ids)
        else:
            self.room_ids = discover_room_ids(self._discovery_base)

        yield Header(show_clock=True)
        with TabbedContent(id="tabs"):
            if not self.room_ids:
                with TabPane("(no rooms)", id="empty"):
                    yield RichLog(
                        id="log-empty",
                        wrap=True,
                        markup=True,
                        max_lines=200,
                        auto_scroll=True,
                    )
            else:
                for room_id in self.room_ids:
                    pane_id = _pane_id(room_id)
                    with TabPane(self._tab_label(room_id), id=pane_id):
                        yield RichLog(
                            id=_log_id(room_id),
                            wrap=True,
                            markup=True,
                            max_lines=20000,
                            auto_scroll=True,
                        )
        # Unified log lives outside the TabbedContent so toggling mode
        # is just a display flip; we don't reparent widgets.
        yield RichLog(
            id="unified-log",
            wrap=True,
            markup=True,
            max_lines=50000,
            auto_scroll=True,
            classes="hidden",
        )
        yield Input(
            id="prompt",
            placeholder="message  (/room <id> <text> in unified)",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Drop a seed line into each tab and spawn one tailer per room.

        Each tailer runs as a `@work(thread=True, group="tail")` worker
        so the asyncio event loop stays free for input + redraws. The
        worker posts `RoomLine` messages back to the App, which the
        main-thread handler appends to the right RichLog.

        The placeholder tab (no rooms discovered) gets a one-liner
        directing the operator to start a room from another terminal.
        """
        if not self.room_ids:
            try:
                log = self.query_one("#log-empty", RichLog)
            except Exception:
                return
            log.write(
                "  [system]no rooms found under "
                "~/.ccoral/rooms/ — start one with `ccoral room`[/system]"
            )
            return
        for room_id in self.room_ids:
            try:
                log = self.query_one(f"#{_log_id(room_id)}", RichLog)
            except Exception:
                continue
            log.write(f"  [system]watching {room_id}[/system]")
            # Resolve transcript path through the discovery base so tests
            # can run against a tmp_path archive.
            transcript = self._transcript_path_for(room_id)
            if transcript is None:
                log.write(
                    f"  [warn]no state dir for {room_id}; nothing to tail[/warn]"
                )
                continue
            self.tail_room(room_id, str(transcript))

    def _transcript_path_for(self, room_id: str) -> "Path | None":
        """Resolve the transcript.jsonl for a room id under the active
        discovery base. Returns None if the room dir doesn't exist; the
        cockpit treats that as a benign "nothing to tail yet" rather
        than crashing.
        """
        from room import ROOMS_ARCHIVE

        base = self._discovery_base if self._discovery_base is not None else ROOMS_ARCHIVE
        room_dir = base / room_id
        if not room_dir.is_dir():
            return None
        return room_dir / "transcript.jsonl"

    # ─── tab navigation actions ────────────────────────────────────────

    def action_next_tab(self) -> None:
        """Cycle the active tab forward; wraps at the end."""
        self._cycle_tab(+1)

    def action_prev_tab(self) -> None:
        """Cycle the active tab backward; wraps at the start."""
        self._cycle_tab(-1)

    def _cycle_tab(self, direction: int) -> None:
        if not self.room_ids:
            return
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        active = tabs.active
        # Resolve the active pane id back to a room id by stripping the
        # `pane-` prefix used in compose(). If for any reason the id
        # doesn't decode, fall back to the first room.
        try:
            current_idx = self.room_ids.index(_room_id_from_pane(active))
        except (ValueError, KeyError):
            current_idx = 0
        next_idx = (current_idx + direction) % len(self.room_ids)
        tabs.active = _pane_id(self.room_ids[next_idx])

    # ─── tailer worker (one per room) ──────────────────────────────────

    @work(thread=True, exclusive=False, group="tail")
    def tail_room(self, room_id: str, jsonl_path: str) -> None:
        """Follow one room's transcript.jsonl; post a RoomLine per record.

        Polls the file size, reads new bytes since the last cursor, and
        splits on newlines (carrying any partial trailing line into the
        next read so a torn write doesn't get dropped as malformed
        JSON). Each parsed record is delivered to the App's main thread
        via ``post_message`` — the @on(RoomLine) handler then writes to
        the right RichLog.

        Per the Phase 11 hard rules: this worker performs no subprocess
        spawn, no relay-loop call, no run_room call. It strictly reads
        a JSONL file and emits messages.
        """
        path = Path(jsonl_path)
        cursor = self._cursors.setdefault(room_id, 0)
        buf = self._line_bufs.setdefault(room_id, b"")
        while True:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                # Transcript not yet created — wait and retry. A freshly
                # started room writes its first record only after the
                # first turn lands; the watch sidecar handles this the
                # same way (see room_watch.py:158).
                time.sleep(self.poll_interval)
                continue
            if size < cursor:
                # File truncated / rotated (rename / delete). Reset and
                # restart the read from the head — the same forgiving
                # behavior room_watch.py adopts.
                cursor = 0
                buf = b""
            if size > cursor:
                try:
                    with open(path, "rb") as f:
                        f.seek(cursor)
                        chunk = f.read(size - cursor)
                except OSError:
                    time.sleep(self.poll_interval)
                    continue
                cursor = size
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines[-1]
                for raw in lines[:-1]:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # post_message is the documented thread-safe path
                    # for delivering messages from a worker thread.
                    self.post_message(RoomLine(room_id, rec))
                # Persist the cursor + carry-buffer for any external
                # introspection (and so a worker restart resumes
                # cleanly without re-rendering already-seen turns).
                self._cursors[room_id] = cursor
                self._line_bufs[room_id] = buf
            else:
                time.sleep(self.poll_interval)

    # ─── message handlers ──────────────────────────────────────────────

    def on_room_line(self, message: RoomLine) -> None:
        """A tailer delivered a parsed transcript record. Render it to
        the per-room RichLog (always) and to the unified-log (always,
        prefixed with the room id) so toggling between modes never
        loses history.

        Updates ``self.last_active_room`` so unified-mode plain-text
        input (C4) targets the most-recently-active room when no
        explicit `/room <id>` prefix is present.
        """
        room_id = message.room_id
        rec = message.record
        self.last_active_room = room_id
        css = _classify(rec, user_name=self.user_name)
        name = rec.get("name") or "?"
        text = rec.get("text") or ""
        # Per-room log line — same shape as room_watch.py's renderer.
        per_room = (
            f"  [{css}]{_escape_markup(name)}:[/{css}] "
            f"{_escape_markup(text)}"
        )
        try:
            log = self.query_one(f"#{_log_id(room_id)}", RichLog)
        except Exception:
            log = None
        if log is not None:
            log.write(per_room)
        # Unified line — prefixed with the room id so it remains
        # disambiguable when N rooms interleave.
        unified = (
            f"  [system]\\[{_escape_markup(room_id)}][/system] "
            f"[{css}]{_escape_markup(name)}:[/{css}] "
            f"{_escape_markup(text)}"
        )
        try:
            unified_log = self.query_one("#unified-log", RichLog)
        except Exception:
            unified_log = None
        if unified_log is not None:
            unified_log.write(unified)
        # Activity badge — only fires when the room isn't the active
        # tab. Posted as a Message so the badge mutation runs through
        # the same dispatch path as everything else, keeping the test
        # rhythm uniform (Pilot.pause() between act + assert).
        if room_id != self._active_room_id():
            self.post_message(RoomActivity(room_id))

    def on_room_activity(self, message: RoomActivity) -> None:
        """A worker reported activity on a non-active tab. Decorate the
        tab label with a `+` badge if not already present.

        Idempotent — a busy room writing dozens of lines per second
        only ever decorates the label once until the badge is cleared.
        """
        room_id = message.room_id
        if room_id in self.activity_badges:
            return
        self.activity_badges.add(room_id)
        self._refresh_tab_label(room_id)

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Clear the activity badge for the tab the operator just
        focused. Fires for every tab change — clicks, Ctrl+N/P, the
        post-mount initial activation — so the badge contract is
        "visible only while the operator is looking elsewhere".
        """
        try:
            room_id = _room_id_from_pane(event.tab.id or "")
        except Exception:
            return
        if room_id in self.activity_badges:
            self.activity_badges.discard(room_id)
            self._refresh_tab_label(room_id)

    # ─── mode + badge actions ──────────────────────────────────────────

    def action_toggle_unified(self) -> None:
        """C4 lands the real toggle. The binding is wired here so the
        Footer shows the keystroke from the first commit; the body
        becomes a real ContentSwitcher-style flip in C4.
        """
        self.unified_mode = not self.unified_mode

    def action_clear_badges(self) -> None:
        """Ctrl+L — clear every activity badge. Useful after stepping
        away from the cockpit and returning to a wall of `+` markers.
        """
        if not self.activity_badges:
            return
        cleared = list(self.activity_badges)
        self.activity_badges.clear()
        for room_id in cleared:
            self._refresh_tab_label(room_id)

    # ─── helpers ───────────────────────────────────────────────────────

    def _tab_label(self, room_id: str) -> str:
        """Compose the visible tab label.

        Format: ``<id> [+]`` when activity is pending. C5 will extend
        this with ``(stopped)`` / ``× broken`` decorations.
        """
        suffix = " [+]" if room_id in self.activity_badges else ""
        return f"{room_id}{suffix}"

    def _refresh_tab_label(self, room_id: str) -> None:
        """Re-render one tab's label after a badge state change.

        Textual's ``TabPane`` exposes a ``label`` property that
        accepts a string; assignment triggers a re-render. We resolve
        the pane via the canonical ``_pane_id`` and silently no-op
        when the pane isn't present (e.g. during teardown).
        """
        try:
            pane = self.query_one(f"#{_pane_id(room_id)}", TabPane)
        except Exception:
            return
        try:
            pane.label = self._tab_label(room_id)
        except Exception:
            # Older Textual builds may expose the label through a
            # different attribute. Falling back silently keeps the
            # cockpit alive — the badge is a visual hint, not a
            # correctness requirement.
            pass

    def _active_room_id(self) -> "str | None":
        """Resolve the currently focused tab back to a room id.

        Returns None when no TabbedContent exists yet (very early in
        compose) or when the active tab is the empty-roster
        placeholder.
        """
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return None
        active = tabs.active
        if not active or active == "empty":
            return None
        return _room_id_from_pane(active)


# ---------------------------------------------------------------------------
# Pane / log id helpers — kept tiny and pure so C2..C5 (and the C7 tests)
# can compute the right widget id from a room id without re-encoding the
# convention in five places. Room ids are timestamped (e.g. `2025-01-15-
# 1430-blank-leguin`) and Textual's id rules disallow dots / colons but
# allow dashes — so we prepend a stable prefix and pass the room id
# through unchanged.
# ---------------------------------------------------------------------------


def _pane_id(room_id: str) -> str:
    return f"pane-{room_id}"


def _log_id(room_id: str) -> str:
    return f"log-{room_id}"


def _room_id_from_pane(pane_id: str) -> str:
    if pane_id.startswith("pane-"):
        return pane_id[len("pane-"):]
    return pane_id
