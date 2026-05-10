"""
CCORAL Room — Cockpit (orchestrator-side TUI)
=============================================

Owns the orchestrator's terminal: a top region that streams the rolling
transcript, a bottom region that holds an input prompt. No external TUI
dependency — `select.select` on `sys.stdin` plus ANSI escape sequences
for the split. Same minimalism as the relay layer.

Public surface (Phase 1):

    setup_split_screen() / teardown_split_screen() — context-managed
        terminal state (alt screen + scroll region). `with split_screen():`
        is the intended call site.

    render_transcript_line(speaker, text, color) — write a colored,
        soft-wrapped transcript line to the top region. No truncation.

    read_command(timeout=None) — non-blocking poll of the input prompt.
        Returns one of:
            ("say",   text)
            ("inject", target, text)
            ("pause",) / ("resume",)
            ("end-after-turn",)
            ("stop",)
            ("save-now",)
            ("transcript",)
            ("help",)
        or None if no complete line is ready.

    set_user(name) — set the [USER] prefix used by the input dispatcher
        (Phase 3 will plumb a CLI flag through; Phase 1 just needs the
        setter so room.py can configure it).

    enqueue_user_event(event) / drain_user_events() — small in-process
        queue for the relay loop. The dispatcher in room.py enqueues
        events that arrive while a profile is mid-stream and flushes
        them after the relay copy step lands.

The split is implemented with the standard tmux-friendly ANSI sequences:
    \\033[?1049h            enter alternate screen
    \\033[<top>;<bot>r      set scrolling region
    \\033[<row>;1H          cursor positioning
    \\033[?1049l            leave alternate screen

We deliberately do not use curses — the relay loop already shells out to
tmux + subprocess and we don't want to fight curses for terminal control.
"""

import atexit
import os
import select
import shutil
import signal
import sys
import termios
import tty
from contextlib import contextmanager

# Reuse room.py's palette so the cockpit and the (legacy) stdout look match.
Y = "\033[33m"
C = "\033[36m"
W = "\033[1;37m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

# ANSI control
ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
CLEAR_SCREEN = "\033[2J"
CLEAR_LINE = "\033[2K"
SHOW_CURSOR = "\033[?25h"


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_user_name = "CASSIUS"

# Phase 8: per-slot speaker display overrides. The room launcher seeds
# these from --user-1 / --user-2 (Phase 3 wires the flags); the relay
# reads them for transcript prefixes when a turn lands. None means
# "use the slot's profile-derived default" (BLANK / BLANK#1 / etc.,
# computed by the caller and passed in via set_speaker_display).
_speaker_displays: dict = {1: None, 2: None}

# Phase 8: one-shot legacy-record WARN. The relay falls back to the
# reader's slot when a turn record arrives without a stamped `slot`
# field. We emit ONE yellow line per session — Phase 12 verifies the
# shim is unreachable against the in-tree server, so a warning here
# means an external producer (test fixture, third-party proxy, or a
# regression). Set kept at module scope so reload-style cockpit
# re-entry doesn't reset the dedup state mid-session.
_legacy_record_warned: set = set()

# Lines pending dispatch — the relay loop drains these after a turn lands.
_event_queue: list = []

# Input editor state (line buffer for the prompt at the bottom).
_input_buffer = ""

# Terminal geometry — recalculated on SIGWINCH.
_term_rows = 24
_term_cols = 80
_transcript_top = 1   # first row of transcript region
_transcript_bot = 22  # last row of transcript region (input prompt is below)
_input_row = 23       # status line
_prompt_row = 24      # input prompt

# Saved tty state for clean restore.
_saved_tty_attrs = None
_split_active = False

# Saved signal handlers so teardown can put them back.
_saved_sigterm_handler = None
_saved_sigwinch_handler = None

# Ensure the atexit hook is only registered once across setup/teardown cycles
# (e.g. /transcript re-entry calls setup again).
_atexit_registered = False

# Transcript scroll cursor — next free row inside the transcript region.
_transcript_cursor = 1


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------

def set_user(name: str) -> None:
    """Configure the user prefix (e.g. 'CASSIUS' → '[CASSIUS] ...').

    Pre-Phase-8 callers used this to label the host's interjections.
    Still the right setter for that — Phase 8 didn't change the host
    prefix; it added per-slot displays for the two AI speakers.
    """
    global _user_name
    _user_name = name


def get_user() -> str:
    return _user_name


# ---------------------------------------------------------------------------
# Phase 8: per-slot speaker display API
# ---------------------------------------------------------------------------

def set_speaker_display(slot: int, name: str) -> None:
    """Set the cockpit display label for slot 1 or slot 2.

    The launcher resolves the right name once at startup using these
    rules (cockpit display defaults are the launcher's job — this
    setter just stores whatever it's handed):

      - --user-1 LO --user-2 OPS         → "LO" / "OPS"
      - distinct profiles, no overrides  → "BLANK" / "LEGUIN"
      - same profile, no overrides       → "BLANK#1" / "BLANK#2"

    Slot 1/2 only — anything else is a bug in the caller.
    """
    if slot not in (1, 2):
        raise ValueError(f"set_speaker_display: slot must be 1 or 2, got {slot!r}")
    _speaker_displays[slot] = name


def display_for_slot(slot: int) -> str | None:
    """Return the configured display for `slot`, or None if unset.

    The relay sources slot displays from `slot_meta` (which the
    launcher populates from the same rules above). This getter exists
    so cockpit code (status line, /help footer) can render the live
    label without re-deriving from the profile name.
    """
    if slot not in (1, 2):
        return None
    return _speaker_displays.get(slot)


def warn_legacy_record(slot: int, profile: str | None) -> None:
    """One-shot yellow WARN that a turn record arrived without a
    stamped `slot` field (Phase 8 contract violation by the producer).

    Dedupes per (slot, profile) tuple so a noisy producer doesn't
    spam the cockpit. Phase 12's verification week should never see
    this line in real sessions — it fires only against legacy proxies
    or hand-crafted test fixtures.
    """
    key = (slot, profile)
    if key in _legacy_record_warned:
        return
    _legacy_record_warned.add(key)
    render_transcript_line(
        "ROOM",
        f"WARN: legacy turn record (slot={slot}, profile={profile!r})",
        Y,
    )


# ---------------------------------------------------------------------------
# Event queue (cross-thread-safe enough for the single-threaded relay loop)
# ---------------------------------------------------------------------------

def enqueue_user_event(event: tuple) -> None:
    """Queue a user event for later flushing by the relay loop."""
    _event_queue.append(event)


def drain_user_events() -> list:
    """Pop and return all queued events. Order preserved."""
    global _event_queue
    out = _event_queue
    _event_queue = []
    return out


def queue_depth() -> int:
    return len(_event_queue)


# ---------------------------------------------------------------------------
# Geometry / terminal helpers
# ---------------------------------------------------------------------------

def _refresh_geometry() -> None:
    global _term_rows, _term_cols, _transcript_top, _transcript_bot, _input_row, _prompt_row
    size = shutil.get_terminal_size(fallback=(80, 24))
    _term_cols = max(20, size.columns)
    _term_rows = max(8, size.lines)
    _transcript_top = 1
    # Reserve two rows at the bottom: a thin status row and the prompt row.
    _transcript_bot = max(_transcript_top + 2, _term_rows - 2)
    _input_row = _term_rows - 1
    _prompt_row = _term_rows


def _set_scroll_region(top: int, bot: int) -> None:
    sys.stdout.write(f"\033[{top};{bot}r")


def _clear_scroll_region() -> None:
    sys.stdout.write("\033[r")


def _move_cursor(row: int, col: int = 1) -> None:
    sys.stdout.write(f"\033[{row};{col}H")


def _redraw_status() -> None:
    _move_cursor(_input_row, 1)
    sys.stdout.write(CLEAR_LINE)
    label = f"{DIM}room ─ /help for commands ─ user: {_user_name}{NC}"
    sys.stdout.write(label)


def _redraw_prompt() -> None:
    _move_cursor(_prompt_row, 1)
    sys.stdout.write(CLEAR_LINE)
    sys.stdout.write(f"{BOLD}> {NC}{_input_buffer}")


def _on_winch(signum, frame) -> None:
    if not _split_active:
        return
    _refresh_geometry()
    _set_scroll_region(_transcript_top, _transcript_bot)
    _redraw_status()
    _redraw_prompt()
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Split-screen lifecycle
# ---------------------------------------------------------------------------

def _on_sigterm(signum, frame) -> None:
    """SIGTERM handler — restore the terminal, then re-raise the default.

    Without this, `kill <pid>` leaves the user inside the alt screen with
    cbreak mode and a narrow scroll region. After teardown we reinstall
    the default disposition and re-send SIGTERM to ourselves so the
    process actually exits.
    """
    try:
        teardown_split_screen()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)


def setup_split_screen() -> None:
    """Enter alt screen, set scroll region, put terminal in cbreak mode.

    Safe to call after a teardown (e.g. `/transcript` re-entry). The
    teardown_split_screen counterpart is idempotent — the `_split_active`
    flag plus a singleton atexit hook guarantees we never double-restore.
    """
    global _saved_tty_attrs, _split_active, _transcript_cursor
    global _saved_sigterm_handler, _saved_sigwinch_handler, _atexit_registered

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # No TTY → fall back to plain stdout. setup is a no-op so the
        # relay loop still runs (useful in tests / piped invocations).
        _split_active = False
        return

    _refresh_geometry()
    try:
        _saved_tty_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    except (termios.error, OSError):
        _saved_tty_attrs = None

    sys.stdout.write(ALT_SCREEN_ON)
    sys.stdout.write(CLEAR_SCREEN)
    _set_scroll_region(_transcript_top, _transcript_bot)
    _move_cursor(_transcript_top, 1)
    _transcript_cursor = _transcript_top

    _redraw_status()
    _redraw_prompt()
    sys.stdout.flush()

    try:
        _saved_sigwinch_handler = signal.signal(signal.SIGWINCH, _on_winch)
    except (ValueError, OSError):
        _saved_sigwinch_handler = None

    # Install SIGTERM handler so `kill <pid>` doesn't strand the user in
    # the alt screen. Saved so teardown can put the prior one back.
    try:
        _saved_sigterm_handler = signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        _saved_sigterm_handler = None

    # atexit is the belt to the SIGTERM/finally suspenders — if anything
    # bypasses normal control flow (subprocess crash, os._exit, etc.) the
    # interpreter shutdown still puts the terminal back. Register once.
    if not _atexit_registered:
        atexit.register(teardown_split_screen)
        _atexit_registered = True

    _split_active = True


def teardown_split_screen() -> None:
    """Restore terminal: clear region, leave alt screen, restore tty.

    Idempotent — the `_split_active` flag short-circuits repeat calls so
    the SIGINT `finally`, the SIGTERM handler, and the atexit hook can
    all fire without double-restoring (which would clobber the user's
    real terminal state with stale saved values).
    """
    global _split_active, _saved_sigterm_handler, _saved_sigwinch_handler

    if not _split_active:
        return
    # Flip the flag first so any re-entrant call (atexit while we're
    # mid-teardown) becomes a no-op.
    _split_active = False

    try:
        _clear_scroll_region()
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(ALT_SCREEN_OFF)
        sys.stdout.flush()
    except Exception:
        pass

    if _saved_tty_attrs is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _saved_tty_attrs)
        except (termios.error, OSError):
            pass

    # Restore the prior signal handlers (or fall back to defaults).
    try:
        signal.signal(
            signal.SIGWINCH,
            _saved_sigwinch_handler if _saved_sigwinch_handler is not None
            else signal.SIG_DFL,
        )
    except (ValueError, OSError):
        pass
    _saved_sigwinch_handler = None

    try:
        signal.signal(
            signal.SIGTERM,
            _saved_sigterm_handler if _saved_sigterm_handler is not None
            else signal.SIG_DFL,
        )
    except (ValueError, OSError):
        pass
    _saved_sigterm_handler = None


@contextmanager
def split_screen():
    """Context manager wrapping setup/teardown. Restores on SIGINT too."""
    setup_split_screen()
    try:
        yield
    finally:
        teardown_split_screen()


# ---------------------------------------------------------------------------
# Transcript rendering
# ---------------------------------------------------------------------------

def _soft_wrap(text: str, width: int, indent: str = "") -> list:
    """Word-wrap text to width. Preserves explicit newlines as paragraph
    breaks. No truncation — long words are hard-broken to fit width.
    """
    out = []
    width = max(20, width)
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        words = paragraph.split(" ")
        line = ""
        for word in words:
            # Hard-break absurdly long words.
            while len(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            candidate = word if not line else line + " " + word
            if len(candidate) > width:
                out.append(line)
                line = word
            else:
                line = candidate
        if line:
            out.append(line)
    if indent:
        out = [out[0]] + [indent + l for l in out[1:]] if out else out
    return out


def render_transcript_line(speaker: str, text: str, color: str) -> None:
    """Append a colored transcript line to the top region. Soft-wrapped.

    Outside split mode, falls back to plain stdout (so the relay loop
    still produces readable output when run without a TTY).
    """
    global _transcript_cursor

    if not _split_active:
        # Non-TTY fallback. No truncation (Phase 1 explicitly removes
        # the 200-char cap from the legacy stdout cockpit).
        print(f"  {color}{speaker}:{NC} {text}")
        sys.stdout.flush()
        return

    label = f"{color}{speaker}:{NC} "
    visible_label_len = len(speaker) + 2  # "<speaker>: "
    indent = " " * visible_label_len
    width = max(20, _term_cols - 2)  # 2-col left gutter for breathing room

    # Wrap the body to (width - visible_label_len) for the first line so
    # the header fits, then to (width - visible_label_len) for continuations.
    body_width = max(20, width - visible_label_len)
    wrapped = _soft_wrap(text, body_width)

    # Save cursor + input state, jump into the scroll region, write.
    sys.stdout.write("\0337")  # save cursor (DECSC)
    try:
        # Jump to the bottom of the scroll region so the next \n scrolls.
        _move_cursor(_transcript_bot, 1)
        if not wrapped:
            wrapped = [""]
        first = True
        for line in wrapped:
            if first:
                sys.stdout.write("\n  " + label + line)
                first = False
            else:
                sys.stdout.write("\n  " + indent + line)
        sys.stdout.write(NC)
    finally:
        sys.stdout.write("\0338")  # restore cursor (DECRC)
        # Re-paint status + prompt in case scroll bumped them.
        _redraw_status()
        _redraw_prompt()
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Input prompt
# ---------------------------------------------------------------------------

def _parse_command(line: str):
    """Translate a typed line into a dispatch tuple. Returns None for empty."""
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
        # /to <profile> <message>
        sub = rest.split(maxsplit=1)
        if len(sub) < 2:
            return ("help",)  # malformed; surface help
        return ("inject", sub[0], sub[1])

    # Unknown slash command — surface help instead of swallowing.
    return ("help",)


def read_command(timeout: float | None = None):
    """Non-blocking input read. Returns a dispatch tuple, or None if no
    complete line is ready within `timeout` seconds.

    Behavior:
      - In split mode, runs a tiny line editor on the bottom prompt row
        (Backspace, Ctrl-C, Enter). Each character read is echoed to the
        prompt; Enter returns the parsed command and clears the buffer.
      - Outside split mode, falls back to a select+readline so non-TTY
        callers still work.
    """
    global _input_buffer

    if not sys.stdin.isatty():
        # Piped stdin — readline blocks, so probe with select first.
        ready, _, _ = select.select([sys.stdin], [], [], timeout or 0)
        if not ready:
            return None
        line = sys.stdin.readline()
        if not line:
            return None
        return _parse_command(line)

    if not _split_active:
        # TTY but no split — same select+readline path.
        ready, _, _ = select.select([sys.stdin], [], [], timeout or 0)
        if not ready:
            return None
        line = sys.stdin.readline()
        if not line:
            return None
        return _parse_command(line)

    # Split-mode line editor.
    deadline_zero = (timeout or 0)
    ready, _, _ = select.select([sys.stdin], [], [], deadline_zero)
    if not ready:
        return None

    # Read everything currently buffered (cbreak mode → 1 char at a time
    # from the kernel, but we may have several queued).
    try:
        chunk = os.read(sys.stdin.fileno(), 1024).decode(errors="replace")
    except (OSError, BlockingIOError):
        return None
    if not chunk:
        return None

    result = None
    for ch in chunk:
        if ch in ("\r", "\n"):
            line = _input_buffer
            _input_buffer = ""
            _redraw_prompt()
            sys.stdout.flush()
            parsed = _parse_command(line)
            if parsed is not None:
                # If multiple commands arrived in one chunk, queue the
                # rest and surface the first now.
                if result is None:
                    result = parsed
                else:
                    enqueue_user_event(parsed)
        elif ch in ("\x7f", "\b"):
            if _input_buffer:
                _input_buffer = _input_buffer[:-1]
            _redraw_prompt()
            sys.stdout.flush()
        elif ch == "\x03":
            # Ctrl-C inside the prompt → treat as /stop, not a SIGINT.
            _input_buffer = ""
            _redraw_prompt()
            sys.stdout.flush()
            if result is None:
                result = ("stop",)
        elif ch == "\x04":
            # Ctrl-D on empty buffer → /end; otherwise ignore.
            if not _input_buffer and result is None:
                result = ("end-after-turn",)
        elif ord(ch) < 0x20:
            # Other control chars — swallow.
            continue
        else:
            _input_buffer += ch
            # Incremental redraw of the prompt.
            _redraw_prompt()
            sys.stdout.flush()

    return result


# ---------------------------------------------------------------------------
# Convenience: help text rendered into the transcript
# ---------------------------------------------------------------------------

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
        render_transcript_line("HELP", line, DIM)
