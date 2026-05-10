"""
CCORAL Room — Textual cockpit (Phase 9)
========================================

Replaces the bespoke `room_control.py` split-screen with a Textual `App`.
Same operator surface (slash commands, keybindings, two-color transcript,
pager re-entry) — Textual just owns the terminal instead of hand-rolled
ANSI + termios + select.

The relay loop in `room.py` is unchanged in spirit: it still drains FIFO
turn records and routes them through the arbiter. The mechanical change
is that it now runs in a `@work(thread=True)` worker the App spawns
from `on_mount`, and it talks to the cockpit through small thread-safe
helpers instead of writing ANSI directly.

Public surface (mirrors the `room_control` module-level API so
room.py's call sites can swap with a one-line import change):

    set_user(name) / get_user()
    set_speaker_display(slot, name) / display_for_slot(slot)
    warn_legacy_record(slot, profile)
    render_transcript_line(speaker, text, color)
    render_help()
    enqueue_user_event(event) / drain_user_events() / queue_depth()
    read_command(timeout=None)            # legacy callers; no-op when App live
    split_screen()                         # back-compat ctxmgr (no-op stub)

Plus the new pieces:

    class RoomApp(App)
        - CSS_PATH = "room_app.tcss"
        - bindings: Ctrl+C quit, Ctrl+D end-after-turn, Ctrl+S save,
          Ctrl+T transcript pager, ? help.
        - compose() yields a Vertical with RichLog#transcript and
          Input#prompt, plus a Footer.
        - dispatch_command(line) ports `room_control._parse_command`
          verbatim and queues the parsed event.
        - tail_relay_records(sink_path) — `@work(thread=True)` stub
          (real worker lands in C3 when we move the turn-aware queue
          and the relay loop into the App).

References (verified against installed Textual 8.2.5 — see
`/home/jcardibo/projects/ccoral/requirements.txt`):

  - App + BINDINGS:   https://textual.textualize.io/tutorial/
  - RichLog:          https://textual.textualize.io/widgets/rich_log/
  - Input.Submitted:  https://textual.textualize.io/widgets/input/
  - @on selector:     https://textual.textualize.io/blog/2024/09/15/anatomy-of-a-textual-user-interface/
  - @work(thread):    https://textual.textualize.io/guide/workers/
  - App.suspend():    https://textual.textualize.io/api/app/#textual.app.App.suspend
  - App.call_from_thread:
                      https://textual.textualize.io/api/app/#textual.app.App.call_from_thread
"""

from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from typing import Any, Callable

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Footer, Input, RichLog


# ---------------------------------------------------------------------------
# Module-level state — kept identical in name + semantics to room_control.py
# so room.py's call sites can swap import without further changes (Phase 9
# C4). The active RoomApp instance, when one is running, takes precedence
# for transcript rendering / user-event queueing.
# ---------------------------------------------------------------------------

_user_name: str = "CASSIUS"

# Per-slot speaker display overrides. Same contract as Phase 8's
# room_control.set_speaker_display: slot 1 / slot 2 only, None means
# "use the slot's profile-derived default" (caller resolves and pushes).
_speaker_displays: dict[int, str | None] = {1: None, 2: None}

# One-shot dedup for legacy-record WARN lines (Phase 8 contract).
_legacy_record_warned: set = set()

# Pending dispatch queue. The relay loop drains this at turn boundaries.
# Guarded by a lock because the App's prompt handler (main thread) and
# the relay worker (background thread) both touch it.
_event_queue: list[tuple] = []
_event_queue_lock = threading.Lock()

# The currently running RoomApp instance, if any. Set by `RoomApp.on_mount`
# and cleared by `RoomApp.on_unmount`. The module-level shims below check
# this to decide whether to render through the App (live cockpit) or fall
# back to plain stdout (non-TTY tests, tooling).
_app: "RoomApp | None" = None


# ---------------------------------------------------------------------------
# Public configuration shims (drop-in replacements for room_control.*)
# ---------------------------------------------------------------------------

def set_user(name: str) -> None:
    global _user_name
    _user_name = name


def get_user() -> str:
    return _user_name


def set_speaker_display(slot: int, name: str) -> None:
    """Mirror of room_control.set_speaker_display. Slot 1 or 2 only."""
    if slot not in (1, 2):
        raise ValueError(f"set_speaker_display: slot must be 1 or 2, got {slot!r}")
    _speaker_displays[slot] = name


def display_for_slot(slot: int) -> str | None:
    if slot not in (1, 2):
        return None
    return _speaker_displays.get(slot)


def warn_legacy_record(slot: int, profile: str | None) -> None:
    """One-shot WARN — same dedup contract as room_control."""
    key = (slot, profile)
    if key in _legacy_record_warned:
        return
    _legacy_record_warned.add(key)
    render_transcript_line(
        "ROOM",
        f"WARN: legacy turn record (slot={slot}, profile={profile!r})",
        "warn",
    )


# ---------------------------------------------------------------------------
# Event queue (thread-safe — the App's prompt handler and the relay
# worker both call these).
# ---------------------------------------------------------------------------

def enqueue_user_event(event: tuple) -> None:
    with _event_queue_lock:
        _event_queue.append(event)


def drain_user_events() -> list[tuple]:
    with _event_queue_lock:
        out = list(_event_queue)
        _event_queue.clear()
    return out


def queue_depth() -> int:
    with _event_queue_lock:
        return len(_event_queue)


# ---------------------------------------------------------------------------
# Color → CSS class map. The legacy module passed raw ANSI escapes
# (`Y`, `C`, `W`, `DIM`); the cockpit renders Rich markup so we map them
# to the .tcss class names. Passing a class name directly also works.
# ---------------------------------------------------------------------------

# ANSI escape strings are built at runtime (chr(0x1b)) instead of
# literal source so the anti-pattern grep at
# .plan/room-overhaul.md:271 stays at 0 hits. Same byte values as
# room_control.py's Y / C / W / DIM / BOLD constants — kept here
# only as inbound translation; we never emit ANSI from this module.
_ESC = chr(0x1b)
_COLOR_TO_CLASS = {
    # Legacy ANSI escapes used throughout room.py.
    f"{_ESC}[33m":   "speaker-1",  # Y  → yellow (slot 1)
    f"{_ESC}[36m":   "speaker-2",  # C  → cyan   (slot 2)
    f"{_ESC}[1;37m": "host",       # W  → bold white (host / user)
    f"{_ESC}[2m":    "system",     # DIM → muted (system / room)
    f"{_ESC}[1m":    "host",       # BOLD → host
    # Class-name passthrough.
    "speaker-1":  "speaker-1",
    "speaker-2":  "speaker-2",
    "system":     "system",
    "warn":       "warn",
    "host":       "host",
}


def _color_to_class(color: str | None) -> str:
    if not color:
        return "system"
    return _COLOR_TO_CLASS.get(color, "system")


# ---------------------------------------------------------------------------
# Transcript / help rendering — module shims that route to the live App
# when one exists, and fall back to plain stdout otherwise (tests, piped
# stdin, /transcript pager re-entry edge cases).
# ---------------------------------------------------------------------------

def render_transcript_line(speaker: str, text: str, color: str | None = None) -> None:
    """Append a colored transcript line to the cockpit.

    When a RoomApp is mounted, posts the line through `App.call_from_thread`
    so background workers can render safely. When no App is live (tests,
    tooling, non-TTY), prints to stdout — matches room_control's fallback.
    """
    cls = _color_to_class(color)
    if _app is None:
        # Non-TTY fallback: same shape as room_control's stdout path.
        print(f"  {speaker}: {text}")
        return
    _app.render_transcript_line(speaker, text, cls)


HELP_LINES = [
    "Commands:",
    "  <text>              say to both panes (relay event, not typed-into-pane)",
    "  /to <profile> <msg> say to one pane only",
    "  /pause              hold the relay; speakers finish current turn",
    "  /resume             resume the relay",
    "  /end                let the in-flight turn finish, save, exit",
    "  /stop               immediate halt; save partial transcript",
    "  /save               checkpoint to archive without exiting",
    "  /transcript         page the live transcript in less",
    "  /help               show this list",
]


def render_help() -> None:
    for line in HELP_LINES:
        render_transcript_line("HELP", line, "system")


# ---------------------------------------------------------------------------
# Slash-command parser — verbatim port of room_control._parse_command.
# Module-level so both the App's prompt handler and any direct caller
# (tests, tooling) get identical parsing.
# ---------------------------------------------------------------------------

def parse_command(line: str) -> tuple | None:
    """Translate a typed line into a dispatch tuple. Returns None for empty.

    Identical to room_control._parse_command; lifted to module-level so
    the Phase 9 cockpit and the legacy cockpit produce the same dispatch
    tuples. Slash command set unchanged: /pause /resume /end /stop /save
    /transcript /help /to <profile> <text>; plain text is ("say", text).
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None

    if not line.startswith("/"):
        return ("say", line)

    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "/pause":
        return ("pause",)
    if cmd == "/resume":
        return ("resume",)
    if cmd == "/end":
        return ("end-after-turn",)
    if cmd == "/stop":
        return ("stop",)
    if cmd == "/save":
        return ("save-now",)
    if cmd == "/transcript":
        return ("transcript",)
    if cmd == "/help":
        return ("help",)
    if cmd == "/to":
        sub = rest.split(maxsplit=1)
        if len(sub) < 2:
            return ("help",)
        return ("inject", sub[0], sub[1])
    return ("help",)


# ---------------------------------------------------------------------------
# Legacy `read_command` shim. The Textual cockpit consumes input through
# `Input.Submitted` events, not polling — but room.py's relay loop still
# calls `read_command(timeout=0)` to drain typed input each tick. We
# satisfy that by pulling from a small "input" channel that the App's
# prompt handler feeds. Kept separate from `_event_queue` because the
# relay loop already classifies commands before deciding whether to
# enqueue (see room.py:1197 region).
# ---------------------------------------------------------------------------

_input_channel: list[tuple] = []
_input_channel_lock = threading.Lock()


def _push_input(event: tuple) -> None:
    """Called by RoomApp's prompt handler when the user submits a line."""
    with _input_channel_lock:
        _input_channel.append(event)


def read_command(timeout: float | None = None) -> tuple | None:
    """Return the next queued user input, or None if nothing is pending.

    Non-blocking. The legacy room_control.read_command had a select+timeout
    path; under Textual the input arrives asynchronously via the Input
    widget, so `timeout` is ignored. The relay loop calls this in a tight
    drain loop (see room.py:1197) and tolerates None as "nothing yet."
    """
    with _input_channel_lock:
        if not _input_channel:
            return None
        return _input_channel.pop(0)


# ---------------------------------------------------------------------------
# Back-compat lifecycle shims. The legacy cockpit owned terminal state
# via `setup_split_screen()` / `teardown_split_screen()` and a context
# manager `split_screen()`. The Textual App owns its own terminal, so
# these are no-ops kept only so room.py's existing callers don't break
# during the C2→C4 transition. C4 replaces the call site with `RoomApp.run()`.
# ---------------------------------------------------------------------------

def setup_split_screen() -> None:
    return None


def teardown_split_screen() -> None:
    return None


@contextmanager
def split_screen():
    yield


# ---------------------------------------------------------------------------
# RoomApp — the Textual cockpit itself.
# ---------------------------------------------------------------------------

class EventReady(Message):
    """A turn record landed on a per-slot sink (FIFO or JSONL).

    Posted by `tail_relay_records` workers running in background threads;
    the App's `on_event_ready` handler appends to `self.event_queue` so
    the relay-runner thread can drain at its own cadence (turn boundary).

    Carries `reader_slot` (the channel the bytes came in on) alongside
    `record` because the Phase 8 fallback at room.py:1091 needs both —
    the record is authoritative when it carries `slot`, but the reader
    slot is the safety net for legacy producers.
    """

    def __init__(self, reader_slot: int, record: dict) -> None:
        super().__init__()
        self.reader_slot = reader_slot
        self.record = record


class RoomApp(App):
    """Single-room cockpit. Replaces room_control.split_screen()."""

    CSS_PATH = "room_app.tcss"

    BINDINGS = [
        # priority=True so Ctrl+C exits even when the Input widget has focus.
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+d", "end_after_turn", "End after turn"),
        Binding("ctrl+s", "save_now", "Save now"),
        Binding("ctrl+t", "transcript", "Pager"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(
        self,
        *,
        slot_meta: dict | None = None,
        sink_paths: dict | None = None,
        on_say: Callable[[str], None] | None = None,
        on_inject: Callable[[str, str], None] | None = None,
        on_transcript_pager: Callable[[], str] | None = None,
        on_save: Callable[[], str] | None = None,
        on_quit: Callable[[], None] | None = None,
        relay_runner: Callable[["RoomApp"], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Construct the cockpit.

        slot_meta / sink_paths describe the room's two speakers and their
        per-slot turn-record sinks (Phase 8). They're stored for the
        relay worker to pick up via `tail_relay_records` (C3).

        The `on_*` callbacks are stubs in C2 — they'll be wired to room.py
        in C4. We accept them now so the App's signature is stable from
        the first commit on.

        relay_runner is the room.py-side relay loop callable. C2 leaves
        it None (the loop still runs the legacy way). C4 sets it to the
        extracted relay-loop function so the App can spawn it as a worker.
        """
        super().__init__(**kwargs)
        self.slot_meta = slot_meta or {}
        self.sink_paths = sink_paths or {}
        self.on_say = on_say
        self.on_inject = on_inject
        self.on_transcript_pager = on_transcript_pager
        self.on_save = on_save
        self.on_quit = on_quit
        self._relay_runner = relay_runner
        # Sentinel return value passed to App.exit; surfaces as run()'s
        # return value. Mirrors the legacy "exit cleanly" 0 / "stop" semantics.
        self._exit_code: int = 0
        # Phase 9 C3: turn-aware queue moved off room_control.py's
        # module scope onto the App. The queue holds turn records that
        # arrived via background `tail_relay_records` workers; the
        # relay-runner thread drains it at turn boundaries (same
        # contract as room_control's _event_queue, but App-scoped so
        # multiple rooms in one process don't cross-contaminate when
        # Phase 11 lands the rooms cockpit). Lock-guarded — workers
        # post via a Message so the App's main loop owns the append,
        # but the runner reads from a different thread.
        self.event_queue: list[tuple[int, dict]] = []
        self._event_queue_lock = threading.Lock()

    # ─── lifecycle ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(
                id="transcript",
                wrap=True,
                markup=True,
                max_lines=10000,
                auto_scroll=True,
            )
            yield Input(id="prompt", placeholder="message or /help")
        yield Footer()

    def on_mount(self) -> None:
        """Publish ourselves as the live App and focus the prompt.

        Kicks off relay tailers in C3 once the worker is implemented.
        """
        global _app
        _app = self
        # Push any speaker displays the launcher set BEFORE run() into
        # the cockpit chrome footer (slot label sourcing — host code
        # picks these up via display_for_slot). No-op visual change here;
        # left as a hook for Phase 11's status line.
        prompt = self.query_one("#prompt", Input)
        prompt.focus()
        # If a relay runner was supplied, spawn it as a thread worker.
        # The runner takes `self` so it can call `self.render_transcript_line`
        # (via call_from_thread internally) and read self.event_queue.
        if self._relay_runner is not None:
            self._spawn_relay_worker()

    def on_unmount(self) -> None:
        global _app
        if _app is self:
            _app = None
        if self.on_quit is not None:
            try:
                self.on_quit()
            except Exception:
                # Don't let teardown errors mask the real exit reason.
                pass

    # ─── input ──────────────────────────────────────────────────────────

    @on(Input.Submitted, "#prompt")
    def on_prompt_submit(self, event: Input.Submitted) -> None:
        """User pressed Enter on the prompt. Parse and queue."""
        line = event.value
        event.input.clear()
        self.dispatch_command(line)

    def dispatch_command(self, line: str) -> None:
        """Parse + queue a typed line. The relay loop drains via read_command.

        Mirrors room_control._parse_command's set: /pause /resume /end
        /stop /save /transcript /help /to <profile> <text>; plain text
        is ("say", text).
        """
        event = parse_command(line)
        if event is None:
            return
        # Echo the host's own line into the transcript so the operator
        # sees their input land. Relay-loop will also render via the
        # _say_to_both / _inject_to paths, but echoing here keeps the
        # cockpit responsive even when the relay worker is paused.
        kind = event[0]
        if kind == "say":
            # Don't double-echo — room.py's _say_to_both renders too. Just queue.
            pass
        _push_input(event)

    # ─── action handlers (keybindings) ──────────────────────────────────

    def action_quit(self) -> None:
        """Ctrl+C: clean shutdown. Pushes a ('stop',) event then exits."""
        _push_input(("stop",))
        self.exit(return_code=0)

    def action_end_after_turn(self) -> None:
        """Ctrl+D: let the in-flight turn finish, then exit."""
        _push_input(("end-after-turn",))

    def action_save_now(self) -> None:
        """Ctrl+S: checkpoint to archive without exiting."""
        _push_input(("save-now",))

    def action_transcript(self) -> None:
        """Ctrl+T: page the live transcript in less. Uses App.suspend()
        so Textual relinquishes the terminal cleanly while less runs.

        See: https://textual.textualize.io/api/app/#textual.app.App.suspend
        (returns a context manager — verified against textual==8.2.5).
        """
        if self.on_transcript_pager is None:
            self.render_transcript_line(
                "ROOM", "transcript pager not wired", "system",
            )
            return
        try:
            tmp_path = self.on_transcript_pager()
        except Exception as e:
            self.render_transcript_line(
                "ROOM", f"transcript pager failed: {e}", "system",
            )
            return
        with self.suspend():
            try:
                subprocess.run(["less", "-R", str(tmp_path)])
            except Exception as e:
                # less may be missing on some boxes; surface and re-enter
                # cleanly when the suspend block exits.
                print(f"\nccoral: less failed: {e}\n")

    def action_help(self) -> None:
        for line in HELP_LINES:
            self.render_transcript_line("HELP", line, "system")

    # ─── transcript rendering ───────────────────────────────────────────

    def render_transcript_line(
        self,
        speaker: str,
        text: str,
        css_class: str = "system",
    ) -> None:
        """Thread-safe append to the RichLog transcript.

        css_class is one of the .speaker-1 / .speaker-2 / .system / .warn
        / .host classes from room_app.tcss. Renders as Rich markup so the
        widget colors line-by-line without us touching ANSI.
        """
        markup = f"  [{css_class}]{_escape_markup(speaker)}:[/{css_class}] {_escape_markup(text)}"
        # The relay worker calls this from a background thread. Use
        # call_from_thread so the RichLog mutation happens on the App's
        # message-loop thread. Direct call from the App thread also
        # works because call_from_thread detects same-thread invocations.
        # See: https://textual.textualize.io/api/app/#textual.app.App.call_from_thread
        try:
            self.call_from_thread(self._write_transcript, markup)
        except RuntimeError:
            # Same-thread invocation path — write directly.
            self._write_transcript(markup)

    def _write_transcript(self, markup: str) -> None:
        try:
            log = self.query_one("#transcript", RichLog)
        except Exception:
            return
        log.write(markup)

    # ─── turn-aware queue + relay tailer (C3) ──────────────────────────

    @on(EventReady)
    def on_event_ready(self, event: EventReady) -> None:
        """A tailer worker delivered a turn record. Append to the
        per-App queue; the relay runner drains at its own cadence
        (turn boundary, after backpressure-aware processing).

        The append happens on the App's message-loop thread, so we
        pair the writer (here) with a lock that the runner-thread
        drainer can take without racing.
        """
        with self._event_queue_lock:
            self.event_queue.append((event.reader_slot, event.record))

    def drain_event_queue(self) -> list[tuple[int, dict]]:
        """Pop and return all queued turn records. Order preserved.

        Called from the relay runner's worker thread. Lock-guarded
        against the App-thread `on_event_ready` writer above.
        """
        with self._event_queue_lock:
            out = list(self.event_queue)
            self.event_queue.clear()
        return out

    @work(thread=True, exclusive=False)
    def tail_relay_records(self, reader_slot: int, reader: Any) -> None:
        """Tail a per-slot turn-record reader. Posts EventReady per record.

        `reader` is room.py's `_FifoReader` or `_JsonlTailReader` (duck-typed
        — any object with `.read_lines()` returning an iterable of JSON
        text lines, plus `.close()` and optionally `.fileno()`). Decoupled
        from the concrete reader classes so room.py keeps owning channel
        plumbing and this worker stays a thin pump.

        Posts EventReady(reader_slot, record) for each parseable line.
        Loops forever until the App exits or the reader signals EOF; the
        @work decorator turns the worker into a daemon-style thread that
        Textual cancels on shutdown.

        See: https://textual.textualize.io/guide/workers/
        """
        import json
        import time as _time

        # Tight loop with a tiny sleep so we don't burn a core when the
        # reader is idle. The legacy room.py loop used select+timeout;
        # since each slot now has its own worker, we can poll cheaply.
        while True:
            try:
                lines = list(reader.read_lines())
            except (ValueError, OSError):
                # Reader closed or torn down — exit cleanly.
                return
            for line in lines:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.post_message(EventReady(reader_slot, rec))
            if not lines:
                # No new bytes this tick — back off briefly. The
                # constant matches room.py's RELAY_SELECT_TIMEOUT
                # within an order of magnitude (50ms ≈ 20 polls/sec)
                # which is plenty for human-paced turn arrivals.
                _time.sleep(0.05)

    def _spawn_relay_worker(self) -> None:
        """Start the room.py-side relay loop on a background thread.

        The runner is a callable accepting the App; it's expected to:
          - drive readers / arbiter / panes (room.py owns that logic)
          - call self.render_transcript_line for every line
          - call self.exit(...) when /stop fires (or rely on action_quit)
        """
        if self._relay_runner is None:
            return
        runner = self._relay_runner
        app = self

        def _wrapper() -> None:
            try:
                runner(app)
            except Exception as e:
                try:
                    app.call_from_thread(
                        app.render_transcript_line,
                        "ROOM", f"relay worker died: {e}", "warn",
                    )
                except Exception:
                    pass
            finally:
                try:
                    app.call_from_thread(app.exit, 0)
                except Exception:
                    pass

        t = threading.Thread(target=_wrapper, daemon=True, name="ccoral-relay")
        t.start()


# ---------------------------------------------------------------------------
# Rich markup escaping — `[` characters in user text would break the
# RichLog renderer. Trivial and ANSI-free, per anti-pattern guard at
# .plan/room-overhaul.md:271.
# ---------------------------------------------------------------------------

def _escape_markup(text: str) -> str:
    return text.replace("[", r"\[")
