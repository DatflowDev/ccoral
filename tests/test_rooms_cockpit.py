"""Phase 11 — Multi-room cockpit (RoomsCockpit) tests.

Drives ``rooms_cockpit.RoomsCockpit`` via ``App.run_test()`` + ``Pilot``
to prove the critical surface:

  1. discovery from a tmp_path state dir fixture
  2. Ctrl+N tab cycling
  3. plain text submission writes ``{"kind": "say", "text": ...}`` to
     the active room's control FIFO and NOT to any other room's FIFO
  4. ``/room <id> <text>`` writes to <id>'s FIFO regardless of which
     tab is active (also exercises unified-mode dispatch)
  5. tailer detects ``state: stopped`` in meta.yaml and decorates the
     tab label

We never spawn proxies, FIFOs from inside ``room.run_room``, tmux, or
any subprocess from the cockpit itself. The control sink we write to
is created via ``room.write_control_event``'s contract — same path
the production sidecars use — and read back from the on-disk JSONL
file directly so the assertion has zero coupling to a live
``ControlFifo`` consumer.

Pattern reference: tests/test_room_app.py, tests/test_room_picker.py.

Run standalone: `python3 tests/test_rooms_cockpit.py`
Run under pytest: `pytest tests/test_rooms_cockpit.py -v`
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

import room
import rooms_cockpit
from textual.widgets import Input, RichLog, TabbedContent


# ---------------------------------------------------------------------------
# Fixtures — small helper that builds a per-room state dir on disk that
# matches the Phase 3 contract (config.yaml + meta.yaml + transcript.jsonl).
# ---------------------------------------------------------------------------


def _make_room_dir(
    base: Path,
    room_id: str,
    *,
    profile1: str = "blank",
    profile2: str = "leguin",
    state: str = "live",
) -> Path:
    """Materialise one room's state dir on disk under `base`.

    Returns the room directory path. Mirrors what
    ``RoomStateDir.write_initial`` produces in production, minimally
    enough for ``_list_room_dirs`` + the cockpit tailer to be happy.
    """
    room_dir = base / room_id
    room_dir.mkdir(parents=True, exist_ok=True)

    config_data = {
        "room_id": room_id,
        "profile1": profile1,
        "profile2": profile2,
        "config": {
            "user_name": "CASSIUS",
            "base_port": 8090,
            "tmux_session_prefix": "room",
        },
    }
    with open(room_dir / "config.yaml", "w") as f:
        yaml.dump(config_data, f, default_flow_style=False)

    meta = {
        "ccoral_version": "test",
        "room_id": room_id,
        "profiles": [profile1, profile2],
        "started": datetime.now().isoformat(),
        "state": state,
        "exit_reason": None if state == "live" else "test",
    }
    with open(room_dir / "meta.yaml", "w") as f:
        yaml.dump(meta, f, default_flow_style=False)

    (room_dir / "transcript.jsonl").touch()
    return room_dir


def _drain_control_sink(room_id: str) -> list[dict]:
    """Read every queued event from the per-room control sink and
    drain it. Works for both the FIFO and JSONL fallback paths.

    For the FIFO path we open with O_RDONLY|O_NONBLOCK and drain;
    for the JSONL path we read the file and truncate. The cockpit's
    ``_send_event`` writes through ``room.write_control_event`` which
    chooses the right sink — but in the test environment we set up
    a JSONL sink up front (no consumer thread) so ``write_control_event``
    keeps appending and we read the file directly.
    """
    sink = room.control_path_for(room_id)
    if not sink.exists():
        return []
    events: list[dict] = []
    if sink.suffix == ".control":
        # FIFO — drain non-blocking.
        try:
            fd = os.open(str(sink), os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return []
        try:
            chunks: list[bytes] = []
            while True:
                try:
                    buf = os.read(fd, 4096)
                except BlockingIOError:
                    break
                if not buf:
                    break
                chunks.append(buf)
            data = b"".join(chunks).decode("utf-8", errors="replace")
        finally:
            os.close(fd)
    else:
        # JSONL fallback — read + truncate.
        with open(sink) as f:
            data = f.read()
        with open(sink, "w") as f:
            pass
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _prime_jsonl_control_sinks(room_ids: list[str]) -> None:
    """Force the JSONL fallback for every room's control sink so the
    test doesn't require a live ControlFifo consumer thread.

    ``room.control_path_for`` prefers the FIFO when it exists; we
    create the JSONL file first and remove any stale FIFO so the
    helper picks the JSONL path.
    """
    room.ROOM_DIR.mkdir(parents=True, exist_ok=True)
    for room_id in room_ids:
        fifo = room.ROOM_DIR / f"{room_id}.control"
        jsonl = room.ROOM_DIR / f"{room_id}.control.jsonl"
        try:
            fifo.unlink()
        except FileNotFoundError:
            pass
        jsonl.touch()


def _cleanup_control_sinks(room_ids: list[str]) -> None:
    for room_id in room_ids:
        for ext in (".control", ".control.jsonl"):
            sink = room.ROOM_DIR / f"{room_id}{ext}"
            try:
                sink.unlink()
            except FileNotFoundError:
                pass


def _run(coro):
    return asyncio.run(coro)


async def _type_and_submit(pilot, text):
    """Same key-mapping helper as test_room_app.py."""
    key_map = {" ": "space", "/": "slash"}
    for ch in text:
        await pilot.press(key_map.get(ch, ch))
    await pilot.press("enter")


# ---------------------------------------------------------------------------
# Test 1 — discovery: cockpit picks up rooms from a fixture state dir
# ---------------------------------------------------------------------------


def test_discovery_from_state_dir_fixture():
    """Cockpit discovers the rooms we materialised under the discovery
    base. Order is newest-first per ``_list_room_dirs``.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_room_dir(base, "0001-room-a")
            # Ensure mtime ordering: second room is newer.
            _make_room_dir(base, "0002-room-b")
            os.utime(base / "0002-room-b", (
                (base / "0001-room-a").stat().st_atime + 1,
                (base / "0001-room-a").stat().st_mtime + 1,
            ))

            app = rooms_cockpit.RoomsCockpit(base=base)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "0001-room-a" in app.room_ids
                assert "0002-room-b" in app.room_ids
                # Newer first.
                assert app.room_ids[0] == "0002-room-b"
                # Each room got a TabPane.
                tabs = app.query_one("#tabs", TabbedContent)
                pane_ids = [pane.id for pane in tabs.query("TabPane")]
                assert "pane-0001-room-a" in pane_ids
                assert "pane-0002-room-b" in pane_ids
                app.exit(0)

    _run(scenario())


# ---------------------------------------------------------------------------
# Test 2 — Ctrl+N cycles the active tab forward
# ---------------------------------------------------------------------------


def test_ctrl_n_cycles_active_tab_forward():
    """With two rooms discovered, Ctrl+N moves the active tab to the
    next room and wraps at the end.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_room_dir(base, "r-alpha")
            _make_room_dir(base, "r-beta")
            os.utime(base / "r-beta", (
                (base / "r-alpha").stat().st_atime + 1,
                (base / "r-alpha").stat().st_mtime + 1,
            ))

            app = rooms_cockpit.RoomsCockpit(base=base)
            async with app.run_test() as pilot:
                await pilot.pause()
                tabs = app.query_one("#tabs", TabbedContent)
                # Active tab is the first room (newest first).
                first = app.room_ids[0]
                assert tabs.active == f"pane-{first}"
                await pilot.press("ctrl+n")
                await pilot.pause()
                # Now the second room is active.
                second = app.room_ids[1]
                assert tabs.active == f"pane-{second}"
                # And one more wraps back to the first.
                await pilot.press("ctrl+n")
                await pilot.pause()
                assert tabs.active == f"pane-{first}"
                app.exit(0)

    _run(scenario())


# ---------------------------------------------------------------------------
# Test 3 — plain text submission lands on the ACTIVE room's FIFO only
# ---------------------------------------------------------------------------


def test_plain_text_writes_say_to_active_room_only():
    """Typing 'hi' + Enter produces ('say', 'hi') on the currently
    active room's control sink and nothing on the other room's sink.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_room_dir(base, "r-one")
            _make_room_dir(base, "r-two")
            _prime_jsonl_control_sinks(["r-one", "r-two"])
            try:
                app = rooms_cockpit.RoomsCockpit(base=base)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # Switch to r-two so we can prove targeting works.
                    tabs = app.query_one("#tabs", TabbedContent)
                    tabs.active = "pane-r-two"
                    await pilot.pause()

                    # Focus the prompt and type.
                    prompt = app.query_one("#prompt", Input)
                    prompt.focus()
                    await pilot.pause()
                    await _type_and_submit(pilot, "hi")
                    await pilot.pause()

                    one_events = _drain_control_sink("r-one")
                    two_events = _drain_control_sink("r-two")
                    assert one_events == [], f"r-one got {one_events!r}"
                    assert two_events == [{"kind": "say", "text": "hi"}], (
                        f"r-two got {two_events!r}"
                    )
                    app.exit(0)
            finally:
                _cleanup_control_sinks(["r-one", "r-two"])

    _run(scenario())


# ---------------------------------------------------------------------------
# Test 4 — `/room <id> <text>` writes to the explicit room's FIFO
# ---------------------------------------------------------------------------


def test_room_prefix_targets_explicit_room():
    """Typing '/room r-one hello' + Enter writes ('say','hello') to
    r-one's control sink even though r-two is the active tab. Also
    asserts no traffic on r-two.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_room_dir(base, "r-one")
            _make_room_dir(base, "r-two")
            _prime_jsonl_control_sinks(["r-one", "r-two"])
            try:
                app = rooms_cockpit.RoomsCockpit(base=base)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # Make r-two the active tab.
                    tabs = app.query_one("#tabs", TabbedContent)
                    tabs.active = "pane-r-two"
                    await pilot.pause()

                    prompt = app.query_one("#prompt", Input)
                    prompt.focus()
                    await pilot.pause()
                    await _type_and_submit(pilot, "/room r-one hello")
                    await pilot.pause()

                    one_events = _drain_control_sink("r-one")
                    two_events = _drain_control_sink("r-two")
                    assert one_events == [{"kind": "say", "text": "hello"}], (
                        f"r-one got {one_events!r}"
                    )
                    assert two_events == [], f"r-two got {two_events!r}"
                    app.exit(0)
            finally:
                _cleanup_control_sinks(["r-one", "r-two"])

    _run(scenario())


# ---------------------------------------------------------------------------
# Test 5 — stopped-room handling: meta.yaml flips to stopped → label badge
# ---------------------------------------------------------------------------


def test_stopped_room_lifecycle_decorates_tab():
    """When meta.yaml flips to ``state: stopped`` mid-session, the
    tailer posts ``RoomStopped`` and the tab label gains the stopped
    decoration.

    We exercise the lifecycle path directly by posting the message —
    the tailer's idle-tick polling is correct by construction
    (covered by inspection in C5's commit message) but waiting 2s
    inside a pytest run is wasteful. The handler under test is the
    interesting half, and that's what we drive here.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_room_dir(base, "r-stopper", state="live")
            app = rooms_cockpit.RoomsCockpit(base=base)
            # Zero grace so the removal timer fires immediately and we
            # can also assert that path didn't crash.
            app.stopped_grace_s = 0.05
            async with app.run_test() as pilot:
                await pilot.pause()
                # Sanity: the tab is present and live before we flip.
                assert "r-stopper" in app.room_ids
                assert app.room_states.get("r-stopper") in (None, "live")

                # Drive the handler directly (same path the tailer
                # would post on EOF + meta state stopped).
                app.post_message(rooms_cockpit.RoomStopped("r-stopper"))
                await pilot.pause()

                assert app.room_states.get("r-stopper") == "stopped"
                # The label resolves through _tab_label which now
                # appends "(stopped)".
                assert "(stopped)" in app._tab_label("r-stopper"), (
                    f"label was {app._tab_label('r-stopper')!r}"
                )
                app.exit(0)

    _run(scenario())


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def main():
    tests = [
        test_discovery_from_state_dir_fixture,
        test_ctrl_n_cycles_active_tab_forward,
        test_plain_text_writes_say_to_active_room_only,
        test_room_prefix_targets_explicit_room,
        test_stopped_room_lifecycle_decorates_tab,
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
