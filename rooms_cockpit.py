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

from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Footer,
    Header,
    Input,
    RichLog,
    TabbedContent,
    TabPane,
)


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
        **kwargs: Any,
    ) -> None:
        """Construct the cockpit.

        ``room_ids`` lets the CLI (or a test) inject a fixed roster —
        used by ``ccoral room watch <id>`` (single-tab subset, wired in
        C6) and by the C7 fixture-driven tests. When None we discover
        from ``~/.ccoral/rooms/`` (or ``base`` if supplied for tests).
        """
        super().__init__(**kwargs)
        self._explicit_room_ids = room_ids
        self._discovery_base = base
        # Resolved at compose() so on_mount can act on the same list.
        self.room_ids: list[str] = []
        # Unified-mode flag (Ctrl+U toggles). Tabs mode is the default.
        self.unified_mode: bool = False

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
        """Drop a seed line into each tab so the operator sees the
        cockpit came up cleanly even before any turn lands.

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

    def action_toggle_unified(self) -> None:
        """C4 lands the real toggle. The binding is wired here so the
        Footer shows the keystroke from the first commit; the body
        becomes a real ContentSwitcher-style flip in C4.
        """
        self.unified_mode = not self.unified_mode

    def action_clear_badges(self) -> None:
        """C3 lands the activity-badge clear. Stub here so the binding
        is reachable from the first commit and the Footer label is
        accurate.
        """
        return

    # ─── helpers ───────────────────────────────────────────────────────

    def _tab_label(self, room_id: str) -> str:
        """Default tab label is the room id. C3 decorates with `+`
        on activity, C5 decorates with `(stopped)` / `× broken`.
        """
        return room_id


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
