"""Phase 9 — Textual cockpit smoke tests.

Drives `RoomApp` via `App.run_test()` + `Pilot.press(...)` to prove the
critical interaction surface: typing into the prompt produces a parsed
event on the input channel, and the standard slash commands round-trip
through `dispatch_command` correctly.

We do NOT spawn proxies, FIFOs, tmux, or any subprocess here — the test
exercises the cockpit shell only. The relay loop, channel readers, and
arbiter live in room.py and have their own (synthetic-stream) test
coverage in tests/test_room_persona_sticky.py.

Pattern reference: tests/test_room_persona_sticky.py.

Run standalone: `python3 tests/test_room_app.py`
Run under pytest: `pytest tests/test_room_app.py -v`
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import room_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_input_channel():
    """Pop everything currently in room_app's input channel.

    The cockpit pushes parsed user events to a module-level channel
    that room.py's relay loop drains via `read_command`. In the test
    we drain it directly to inspect what the prompt produced.
    """
    out = []
    while True:
        ev = room_app.read_command()
        if ev is None:
            return out
        out.append(ev)


def _reset_module_state():
    """Clear room_app's process-wide state between tests so one test's
    queued events don't leak into the next one.
    """
    _drain_input_channel()
    room_app.drain_user_events()


# ---------------------------------------------------------------------------
# parse_command (pure-function tests — no App needed)
# ---------------------------------------------------------------------------

def test_parse_plain_text_is_say():
    assert room_app.parse_command("hi there") == ("say", "hi there")


def test_parse_slash_commands_roundtrip():
    cases = {
        "/pause":            ("pause",),
        "/resume":           ("resume",),
        "/end":              ("end-after-turn",),
        "/stop":             ("stop",),
        "/save":             ("save-now",),
        "/transcript":       ("transcript",),
        "/help":             ("help",),
        "/to leguin hello":  ("inject", "leguin", "hello"),
    }
    for line, expected in cases.items():
        assert room_app.parse_command(line) == expected, f"failed for {line!r}"


def test_parse_empty_returns_none():
    assert room_app.parse_command("") is None
    assert room_app.parse_command("   ") is None
    assert room_app.parse_command("\n") is None


def test_parse_to_without_message_falls_back_to_help():
    # Malformed `/to <profile>` (no message body) surfaces help instead
    # of silently swallowing — same contract as room_control._parse_command.
    assert room_app.parse_command("/to leguin") == ("help",)


def test_parse_unknown_slash_surfaces_help():
    assert room_app.parse_command("/nope") == ("help",)


# ---------------------------------------------------------------------------
# Pilot tests (drive the App via App.run_test())
# ---------------------------------------------------------------------------

def _run_pilot(coro):
    """Run a Pilot coroutine to completion. Pytest's async support is
    not assumed here — we drive the event loop manually so the test
    works under both `python3 tests/test_room_app.py` and `pytest`.
    """
    return asyncio.run(coro)


async def _type_and_submit(pilot, text):
    """Type each character into the focused Input, then press enter.

    Pilot.press uses key names; printable chars work as their literal
    name (single character). Spaces are "space"; we map a tiny set
    here because the test inputs are constrained ASCII.
    """
    key_map = {" ": "space", "/": "slash"}
    for ch in text:
        await pilot.press(key_map.get(ch, ch))
    await pilot.press("enter")


def test_typing_plain_text_lands_on_input_channel():
    """Typing 'hi' + Enter produces a ('say', 'hi') event on the
    input channel that the relay loop would read via read_command."""
    _reset_module_state()

    async def scenario():
        app = room_app.RoomApp()
        async with app.run_test() as pilot:
            await _type_and_submit(pilot, "hi")
            # Let the App process the Submitted message before reading.
            await pilot.pause()
            events = _drain_input_channel()
            assert events == [("say", "hi")], f"got {events!r}"
            app.exit(return_code=0)

    _run_pilot(scenario())


def test_slash_pause_lands_as_pause_event():
    """Typing '/pause' + Enter produces a ('pause',) event."""
    _reset_module_state()

    async def scenario():
        app = room_app.RoomApp()
        async with app.run_test() as pilot:
            await _type_and_submit(pilot, "/pause")
            await pilot.pause()
            events = _drain_input_channel()
            assert events == [("pause",)], f"got {events!r}"
            app.exit(return_code=0)

    _run_pilot(scenario())


def test_render_transcript_line_writes_to_richlog():
    """RoomApp.render_transcript_line lands a Rich-markup line in the
    transcript widget. We inspect the RichLog's line buffer rather
    than scraping the rendered output — same widget API the cockpit
    consumes, no terminal coupling.
    """
    _reset_module_state()

    async def scenario():
        from textual.widgets import RichLog
        app = room_app.RoomApp()
        async with app.run_test() as pilot:
            app.render_transcript_line("CASSIUS", "hello world", "host")
            await pilot.pause()
            log = app.query_one("#transcript", RichLog)
            # RichLog stores pending writes in `lines`; one write → one entry.
            assert len(log.lines) >= 1, f"transcript empty after write: {log.lines!r}"
            app.exit(return_code=0)

    _run_pilot(scenario())


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_parse_plain_text_is_say,
        test_parse_slash_commands_roundtrip,
        test_parse_empty_returns_none,
        test_parse_to_without_message_falls_back_to_help,
        test_parse_unknown_slash_surfaces_help,
        test_typing_plain_text_lands_on_input_channel,
        test_slash_pause_lands_as_pause_event,
        test_render_transcript_line_writes_to_richlog,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"{t.__name__}: OK")
        except AssertionError as e:
            failed += 1
            print(f"{t.__name__}: FAIL — {e}")
        except Exception as e:
            failed += 1
            print(f"{t.__name__}: ERROR — {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
