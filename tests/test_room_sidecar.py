"""Phase 6 — sidecar (watch + serve) + control FIFO tests.

Five (six) tests covering the Phase 6 contract:

1. ``ControlFifo`` routes ``{"kind": "say", "text": ...}`` lines to its
   router as ``("say", text)`` tuples.
2. ``ControlFifo`` routes ``{"kind": "inject", "target": ..., "text": ...}``
   to the router as ``("inject", target, text)``.
3. ``WatchApp`` (Textual ``run_test`` + Pilot) tails ``transcript.jsonl``
   and renders new lines into its RichLog.
4. ``room_serve`` renders existing transcript records into the GET /
   HTML response.
5. ``room_serve`` POST /say writes a control event that the orchestrator's
   ControlFifo consumer routes through the configured router.
6. ``room_serve`` SSE stream pushes a record after a new line lands in
   the transcript file (the "no refresh required" promise).

Pattern reference: tests/test_room_app.py (Pilot driver), aiohttp
TestServer / TestClient for the server.

Run standalone: ``python3 tests/test_room_sidecar.py``
Run under pytest: ``pytest tests/test_room_sidecar.py -v``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import room
import room_serve
import room_watch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for(predicate, *, timeout: float = 2.0, interval: float = 0.02):
    """Block (with sleep, not async) until predicate() is truthy or timeout.

    Raises AssertionError if the timeout fires. Used for FIFO round-trip
    tests where we need to give the consumer thread a tick to drain.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {predicate!r}")


def _isolate_room_dir(monkeypatch=None):
    """Return a fresh temp dir to use as ROOM_DIR for one test, restoring
    the original on teardown via the returned cleanup callable.
    """
    tmp = tempfile.mkdtemp(prefix="ccoral-sidecar-test-")
    original = room.ROOM_DIR
    room.ROOM_DIR = Path(tmp)

    def cleanup():
        room.ROOM_DIR = original
        # Best-effort dir cleanup — leave on failure for post-mortem.
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    return Path(tmp), cleanup


# ---------------------------------------------------------------------------
# Test 1 — ControlFifo routes "say"
# ---------------------------------------------------------------------------


def test_control_fifo_routes_say_event():
    """Producer writes {"kind": "say", "text": "hello"}; consumer routes
    it to the router as ("say", "hello"). Same surface the relay loop's
    read_command drain consumes.
    """
    _, cleanup = _isolate_room_dir()
    try:
        events: list = []

        def router(ev):
            events.append(ev)

        c = room.ControlFifo("test-say-route", router=router)
        c.start()
        try:
            # Give the consumer thread a tick to open the FIFO for read.
            time.sleep(0.05)
            room.write_control_event(
                "test-say-route", {"kind": "say", "text": "hello"},
            )
            _wait_for(lambda: len(events) >= 1)
        finally:
            c.stop()

        assert events == [("say", "hello")], f"got {events!r}"
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# Test 2 — ControlFifo routes "inject" with target
# ---------------------------------------------------------------------------


def test_control_fifo_routes_inject_event():
    """Producer writes inject; consumer routes to (\"inject\", target, text).

    Empty / missing target is dropped silently — the relay loop's
    _inject_to handler would otherwise route to a bare "" pane name.
    """
    _, cleanup = _isolate_room_dir()
    try:
        events: list = []

        def router(ev):
            events.append(ev)

        c = room.ControlFifo("test-inject-route", router=router)
        c.start()
        try:
            time.sleep(0.05)
            # Valid inject lands.
            room.write_control_event(
                "test-inject-route",
                {"kind": "inject", "target": "leguin", "text": "world"},
            )
            # Invalid inject (no target) dropped.
            room.write_control_event(
                "test-inject-route",
                {"kind": "inject", "text": "no target"},
            )
            # Unknown kind dropped (forward-compat).
            room.write_control_event(
                "test-inject-route",
                {"kind": "vibe", "text": "future"},
            )
            _wait_for(lambda: len(events) >= 1)
            # Sleep a tick more so the dropped lines get a chance to
            # be parsed (and dropped) before we assert the count.
            time.sleep(0.1)
        finally:
            c.stop()

        assert events == [("inject", "leguin", "world")], f"got {events!r}"
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# Test 3 — WatchApp tails appended JSONL lines (Pilot)
# ---------------------------------------------------------------------------


def test_watch_app_tails_appended_lines():
    """Append a turn record to transcript.jsonl while WatchApp is mounted;
    assert the RichLog picks it up.
    """
    async def scenario(transcript: Path):
        from textual.widgets import RichLog
        app = room_watch.WatchApp(
            transcript_path=transcript,
            room_id="2026-05-10_test",
            user_name="CASSIUS",
            poll_interval=0.05,
        )
        async with app.run_test() as pilot:
            # Let on_mount fire + tailer thread spin up.
            await pilot.pause()
            # Append a record — the tailer's next tick should catch it.
            with open(transcript, "a") as f:
                f.write(json.dumps(
                    {"name": "BLANK", "text": "hi from test",
                     "slot": 1, "kind": "turn"},
                ) + "\n")
            # Pump the App's event loop a few times so call_from_thread
            # delivers the write.
            for _ in range(20):
                await pilot.pause()
                log = app.query_one("#transcript", RichLog)
                # The mount line ("watching ...") + at least one rendered
                # transcript line means the tailer did its job.
                rendered = "\n".join(str(s) for s in log.lines)
                if "hi from test" in rendered:
                    break
                await asyncio.sleep(0.05)
            log = app.query_one("#transcript", RichLog)
            rendered = "\n".join(str(s) for s in log.lines)
            assert "hi from test" in rendered, (
                f"transcript did not pick up the appended line; got "
                f"{rendered!r}"
            )
            app.exit(return_code=0)

    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "transcript.jsonl"
        transcript.touch()
        asyncio.run(scenario(transcript))


# ---------------------------------------------------------------------------
# Test 4 — room_serve renders existing transcript HTML
# ---------------------------------------------------------------------------


def test_serve_renders_transcript_html():
    """Server's GET / returns 200 and includes the existing transcript
    records in the prerendered backlog (so a fresh page load shows
    history before the SSE stream starts).
    """
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario(transcript: Path):
        state = room_serve.ServerState(
            room_id="2026-05-10_test",
            transcript_path=transcript,
            user_name="CASSIUS",
            port=8095,
        )
        app = room_serve.make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/")
            assert resp.status == 200
            body = await resp.text()
            assert "BLANK" in body, body[:500]
            assert "hello world" in body
            # Slot-1 record gets the speaker-1 css class — palette mirror.
            assert "speaker-1" in body
            # Loopback bind acknowledged in the header.
            assert "8095" in body

    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "transcript.jsonl"
        transcript.write_text(json.dumps({
            "name": "BLANK", "text": "hello world",
            "slot": 1, "kind": "turn",
        }) + "\n")
        asyncio.run(scenario(transcript))


# ---------------------------------------------------------------------------
# Test 5 — POST /say writes to control FIFO
# ---------------------------------------------------------------------------


def test_serve_post_say_writes_to_control_fifo():
    """End-to-end: a ControlFifo is consuming /tmp/ccoral-room/<id>.control;
    POST /say {"text": "..."} → router gets ("say", "...").

    This proves the wire from the browser-side form through the aiohttp
    handler through write_control_event into the orchestrator-side
    consumer is intact — i.e. the FIFO contract Phase 11 will reuse.
    """
    from aiohttp.test_utils import TestClient, TestServer

    _, cleanup = _isolate_room_dir()
    try:
        events: list = []

        def router(ev):
            events.append(ev)

        c = room.ControlFifo("test-serve-say", router=router)
        c.start()
        try:
            time.sleep(0.05)

            async def scenario():
                with tempfile.TemporaryDirectory() as tmp:
                    transcript = Path(tmp) / "transcript.jsonl"
                    transcript.touch()
                    state = room_serve.ServerState(
                        room_id="test-serve-say",
                        transcript_path=transcript,
                        user_name="CASSIUS",
                        port=8095,
                    )
                    app = room_serve.make_app(state)
                    async with TestClient(TestServer(app)) as client:
                        resp = await client.post(
                            "/say", json={"text": "hi from browser"},
                        )
                        assert resp.status == 200, await resp.text()
                        payload = await resp.json()
                        assert payload == {"ok": True}

            asyncio.run(scenario())
            _wait_for(lambda: len(events) >= 1)
        finally:
            c.stop()

        assert events == [("say", "hi from browser")], f"got {events!r}"
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# Test 6 — SSE stream pushes new turn records as they land
# ---------------------------------------------------------------------------


def test_serve_sse_pushes_new_records():
    """Open an EventSource-style read against /sse; append a new line to
    the transcript file; assert the data: payload carrying that record
    arrives on the stream.

    aiohttp's TestClient gives us a chunked reader on the response
    object; we read a couple of chunks with a wait_for so a stuck
    test doesn't hang the suite.
    """
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario(transcript: Path):
        state = room_serve.ServerState(
            room_id="test-sse",
            transcript_path=transcript,
            user_name="CASSIUS",
            port=8095,
        )
        app = room_serve.make_app(state)
        async with TestClient(TestServer(app)) as client:
            # Open the SSE stream as a long-lived response.
            resp = await client.get("/sse")
            assert resp.status == 200
            # Append a record; the asyncio tailer's next 0.25s tick
            # should pick it up and fan out to our subscriber.
            with open(transcript, "a") as f:
                f.write(json.dumps({
                    "name": "BLANK", "text": "sse-pushed",
                    "slot": 2, "kind": "turn",
                }) + "\n")

            # Read until we see our payload, with a 3s overall budget.
            buffer = b""
            try:
                async with asyncio.timeout(3.0):
                    while b"sse-pushed" not in buffer:
                        chunk = await resp.content.read(256)
                        if not chunk:
                            break
                        buffer += chunk
            except (asyncio.TimeoutError, AttributeError):
                # Python 3.10 doesn't have asyncio.timeout; fall back
                # to wait_for with manual chunked reads.
                pass

            if b"sse-pushed" not in buffer:
                # Fallback path: explicit wait_for-based read loop for
                # Python versions without asyncio.timeout.
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and b"sse-pushed" not in buffer:
                    try:
                        chunk = await asyncio.wait_for(
                            resp.content.read(256), timeout=0.5,
                        )
                    except asyncio.TimeoutError:
                        chunk = b""
                    if chunk:
                        buffer += chunk

            assert b"sse-pushed" in buffer, (
                f"SSE stream did not deliver the appended record; "
                f"buffer={buffer!r}"
            )
            resp.close()

    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "transcript.jsonl"
        transcript.touch()
        asyncio.run(scenario(transcript))


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def main():
    tests = [
        test_control_fifo_routes_say_event,
        test_control_fifo_routes_inject_event,
        test_watch_app_tails_appended_lines,
        test_serve_renders_transcript_html,
        test_serve_post_say_writes_to_control_fifo,
        test_serve_sse_pushes_new_records,
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
            import traceback
            traceback.print_exc()
            print(f"{t.__name__}: ERROR — {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
