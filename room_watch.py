"""
CCORAL Room — Watch sidecar (Phase 6)
======================================

Read-only color transcript that follows ``transcript.jsonl`` for one room
with ``tail -f`` semantics. A small Textual ``App`` — single ``RichLog``
that grows as new turns land in the per-room state dir.

Entry point ``ccoral room watch <id|last>`` (wired from the ccoral CLI's
``cmd_room`` dispatcher). Read-only by design: the watch sidecar does
NOT write to the control FIFO. ``serve`` (the next commit) is the writer.

Pattern reference:
  - room_app.py — Textual cockpit shell, RichLog usage, @work(thread=True)
    background tailer.
  - room_picker.py — small focused App; same compose / BINDINGS shape.

After Phase 11 lands, ``ccoral room watch <id>`` becomes an alias for
``ccoral rooms <id>`` filtered to a single room. Phase 6 ships the
standalone ``watch`` first so operators can read a room from a second
terminal today, before the multi-room TUI exists.

References (verified against installed textual==8.2.5):
  - App + BINDINGS:    https://textual.textualize.io/tutorial/
  - RichLog:           https://textual.textualize.io/widgets/rich_log/
  - @work(thread):     https://textual.textualize.io/guide/workers/
  - App.call_from_thread:
                       https://textual.textualize.io/api/app/#textual.app.App.call_from_thread
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, RichLog


# ---------------------------------------------------------------------------
# Slot-color resolution.
#
# room_app.tcss assigns slot 1 -> yellow, slot 2 -> cyan. The transcript.jsonl
# records carry an explicit `slot` field (Phase 8) so we can color each line
# without re-deriving from the speaker name. SYSTEM / ROOM / host lines fall
# back to muted / bold-white.
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
# WatchApp — single RichLog tailing transcript.jsonl.
# ---------------------------------------------------------------------------


class WatchApp(App):
    """Read-only follower for one room's transcript.jsonl.

    Re-uses room_app.tcss so the palette matches the cockpit. The CSS is
    deliberately the same file rather than a duplicate — operators with
    cockpit muscle memory should see the same yellow / cyan / muted colors
    in watch mode.
    """

    CSS_PATH = "room_app.tcss"
    TITLE = "ccoral room watch"

    BINDINGS = [
        # priority=True so q exits even if some future widget ate the key.
        Binding("q,ctrl+c", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        transcript_path: Path,
        *,
        room_id: str = "",
        user_name: str = "CASSIUS",
        poll_interval: float = 0.25,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.transcript_path = Path(transcript_path)
        self.room_id = room_id
        self.user_name = user_name
        self.poll_interval = poll_interval
        # Bytes already rendered. Persists across re-opens (the file might
        # be rotated mid-watch by `room rename`; the next read just starts
        # fresh from offset 0 then).
        self._cursor = 0
        # Carry partial lines between reads — a line written in two
        # syscalls would otherwise be split and dropped as malformed JSON.
        self._buf = b""

    # ─── lifecycle ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield RichLog(
                id="transcript",
                wrap=True,
                markup=True,
                max_lines=20000,
                auto_scroll=True,
            )
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#transcript", RichLog)
        label = self.room_id or self.transcript_path.parent.name
        log.write(f"  [system]watching {label} ({self.transcript_path})[/system]")
        # Spawn the tailer as a thread worker so the asyncio event loop
        # stays free for input + redraws (per plan line: "DO NOT block
        # the Textual event loop in room_watch.py").
        self.tail_loop()

    # ─── tail worker ───────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="watch-tail")
    def tail_loop(self) -> None:
        """Poll transcript.jsonl. Each new complete line gets parsed and
        rendered via call_from_thread so the RichLog mutation happens on
        the App's main thread.

        We tolerate the file disappearing (rename / delete) by simply
        idling until it reappears — operators sometimes rename rooms
        out from under a watcher and the friendlier behavior is to keep
        the App alive rather than crash.
        """
        while not self._is_exited():
            try:
                size = self.transcript_path.stat().st_size
            except FileNotFoundError:
                time.sleep(self.poll_interval)
                continue
            if size < self._cursor:
                # File truncated / rotated — start over.
                self._cursor = 0
                self._buf = b""
            if size > self._cursor:
                try:
                    with open(self.transcript_path, "rb") as f:
                        f.seek(self._cursor)
                        chunk = f.read(size - self._cursor)
                except OSError:
                    time.sleep(self.poll_interval)
                    continue
                self._cursor = size
                self._buf += chunk
                self._drain_buffer()
            else:
                time.sleep(self.poll_interval)

    def _is_exited(self) -> bool:
        # Textual's worker cancels via raise on shutdown, but checking
        # _running gives us a graceful exit on quit too.
        try:
            return self._running is False  # type: ignore[attr-defined]
        except AttributeError:
            return False

    def _drain_buffer(self) -> None:
        """Split self._buf on newlines, dispatch complete lines, keep tail."""
        lines = self._buf.split(b"\n")
        self._buf = lines[-1]
        for raw in lines[:-1]:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            self._render_line(line)

    def _render_line(self, line: str) -> None:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return
        name = rec.get("name") or "?"
        text = rec.get("text") or ""
        css = _classify(rec, user_name=self.user_name)
        markup = (
            f"  [{css}]{_escape_markup(name)}:[/{css}] "
            f"{_escape_markup(text)}"
        )
        try:
            self.call_from_thread(self._append, markup)
        except RuntimeError:
            # Same-thread call — happens when the App has already shut
            # down and the worker is winding down. Drop the line.
            pass

    def _append(self, markup: str) -> None:
        try:
            log = self.query_one("#transcript", RichLog)
        except Exception:
            return
        log.write(markup)


# ---------------------------------------------------------------------------
# Public entry point — the ccoral CLI calls this.
# ---------------------------------------------------------------------------


def run_watch(room_id: str, *, user_name: str = "CASSIUS") -> int:
    """Resolve `<id|last>` and run the WatchApp synchronously.

    Returns the App's exit code (0 on clean q / Ctrl+C, 1 on resolution
    failure). Errors print to stderr in the same shape as room_show.
    """
    # Late import to avoid pulling room.py's heavy dep graph (yaml, profile
    # loading, etc.) when this module is import-checked in isolation.
    from room import _resolve_room_id

    target = _resolve_room_id(room_id)
    if target is None:
        print(f"room not found: {room_id}", file=sys.stderr)
        return 1
    transcript = target / "transcript.jsonl"
    if not transcript.exists():
        # Touch it so the tailer has something to stat — a freshly-started
        # room writes its first record only after the first turn lands.
        transcript.touch()

    app = WatchApp(
        transcript_path=transcript,
        room_id=target.name,
        user_name=user_name,
    )
    app.run()
    return 0
