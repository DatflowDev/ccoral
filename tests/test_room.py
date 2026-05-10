#!/usr/bin/env python3
"""Phase 7+12 — room integration tests.

Closes out the room-overhaul plan: end-to-end-ish coverage of the
JSONL/control-FIFO contract that the per-phase suites exercise in
isolation. The real proxies + tmux + Textual cockpit are NOT spun up
here — that surface is covered by `.plan/room-overhaul-runbook.md`,
which is the human runbook for live validation. What we DO exercise:

  * Slot-stamped turn records flowing through `resolve_record_slot`
    and `RoomState.append_turn` end up in `transcript.jsonl` in the
    sender's emission order, with `slot` + `profile` preserved
    (the duplicate-`blank` collision case).
  * `meta.yaml` rolls forward `state=live` then `state=stopped` with
    a clean `exit_reason`.
  * The control FIFO's `say` event lands exactly once in the router
    queue (the same surface a sidecar interjection would use mid-turn).
  * A `--resume` against a state dir surfaces the prior exchange as
    an inject-tail system note (no recap-style chat-message dump),
    which is the structural fix Phase 5 made.

What's deliberately skipped (covered by the runbook):

  * `tmux capture-pane -p` checks for `Read /tmp/...` leaks. Requires a
    live tmux server; the runbook drives this in Sequence A.
  * Real proxy boot on 8090/8091. Covered by Sequence A end-to-end.

Run standalone: ``python3 tests/test_room.py``
Run under pytest: ``pytest tests/test_room.py -v``
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import room
from room import (  # noqa: E402
    ControlFifo,
    RoomConfig,
    RoomState,
    _make_room_id,
    build_prior_exchange_block,
    create_room_profiles,
    resolve_record_slot,
    write_control_event,
)


# ---------------------------------------------------------------------------
# Helpers — mirror the per-phase suites' synthetic-record + isolation pattern
# ---------------------------------------------------------------------------


def _turn_record(slot: int, profile: str, text: str, ts: str) -> dict:
    """Phase-8-shaped turn record. Same shape server.py emits — used by
    test_room_persona_sticky.py for the deterministic ordering assertions.
    """
    return {
        "ts": ts,
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "text": text,
        "lane": "primary",
        "request_id": f"req_{slot}_{ts}",
        "profile": profile,
        "slot": slot,
        "kind": "turn",
        "name": profile.upper() if profile != profile.upper() else profile,
    }


def _wait_for(predicate, *, timeout: float = 2.0, interval: float = 0.02):
    """Block until predicate() is truthy or timeout. Same helper shape as
    test_room_sidecar.py — no async, plays nicely with the FIFO consumer
    thread that ControlFifo.start() spins up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {predicate!r}")


def _drive_turns_into_state(state: RoomState, stream: list) -> list:
    """Take a list of (reader_slot, record) tuples and run them through
    the same `resolve_record_slot` + `state.append_turn` pipe the relay
    loop uses (room.py around line 2089). Returns the resolved
    [(slot, profile, text), ...] list so a test can assert ordering
    independent of what landed in transcript.jsonl on disk.
    """
    resolved = []
    for reader_slot, rec in stream:
        slot, _legacy = resolve_record_slot(rec, reader_slot)
        # Mirror the relay's behaviour: stamp the resolved slot back
        # onto the record before appending, so the on-disk transcript
        # reflects the authoritative slot (not whatever the channel
        # delivered).
        rec_out = dict(rec)
        rec_out["slot"] = slot
        state.append_turn(rec_out)
        resolved.append((slot, rec.get("profile"), rec.get("text")))
    return resolved


# ---------------------------------------------------------------------------
# Test 1 — three-turn exchange end-to-end (duplicate-profile case)
# ---------------------------------------------------------------------------


def test_three_turn_blank_blank_exchange_preserves_order_and_slots(tmp_path):
    """Spin a stub `room blank blank`: synthesize three turn records
    flowing slot1 -> slot2 -> slot1, push them through the relay's
    resolution + append pipe, then assert:

      * transcript.jsonl has exactly 3 turn records
      * the records' (slot, profile, text) triples match the sender
        order (no swap, no duplicate-profile collision)
      * meta.yaml moves from state=live to state=stopped + exit_reason="clean"

    This is the structural Phase-7+12 acceptance for the duplicate-
    profile case the per-phase persona-sticky suite asserts on a
    deeper helper. Here we drive the full state-dir round-trip.
    """
    cfg = RoomConfig()
    room_id = _make_room_id("blank", "blank")
    channels = {
        1: (tmp_path / "slot1_blank.fifo", "fifo"),
        2: (tmp_path / "slot2_blank.fifo", "fifo"),
    }
    state = RoomState(
        room_id, cfg, "blank", "blank", channels, base_dir=tmp_path,
    )
    state.write_initial()

    # meta.yaml is live straight after write_initial.
    live_meta = yaml.safe_load(state.meta_path.read_text())
    assert live_meta["state"] == "live"
    assert live_meta["exit_reason"] is None
    assert live_meta["profiles"] == ["blank", "blank"]

    stream = [
        (1, _turn_record(1, "blank", "side A opens",  "2026-05-10T00:00:00.000Z")),
        (2, _turn_record(2, "blank", "side B replies","2026-05-10T00:00:01.000Z")),
        (1, _turn_record(1, "blank", "side A wraps",  "2026-05-10T00:00:02.000Z")),
    ]
    resolved = _drive_turns_into_state(state, stream)

    # In-memory ordering matches the sender's emission order.
    assert resolved == [
        (1, "blank", "side A opens"),
        (2, "blank", "side B replies"),
        (1, "blank", "side A wraps"),
    ]

    state.update_exit("clean")

    # On-disk transcript has exactly the three records, in order.
    lines = [
        json.loads(l) for l in state.transcript_path.read_text().splitlines()
        if l.strip()
    ]
    assert len(lines) == 3
    assert [(r["slot"], r["profile"], r["text"]) for r in lines] == [
        (1, "blank", "side A opens"),
        (2, "blank", "side B replies"),
        (1, "blank", "side A wraps"),
    ]

    # meta.yaml stamped clean exit.
    final_meta = yaml.safe_load(state.meta_path.read_text())
    assert final_meta["state"] == "stopped"
    assert final_meta["exit_reason"] == "clean"
    assert final_meta["ended"]

    # No `Read /tmp/...` leak in the transcript text — covered live by
    # Sequence B in the runbook for the full pane-capture path; here we
    # verify the on-disk record is clean of the canonical leak shape.
    full_text = "\n".join(r["text"] for r in lines)
    assert "Read /tmp/" not in full_text


# ---------------------------------------------------------------------------
# Test 2 — control-FIFO interjection lands exactly once
# ---------------------------------------------------------------------------


def test_control_fifo_interjection_lands_exactly_once(tmp_path, monkeypatch):
    """Sidecar / cockpit-side interjection: an operator types `say hello`
    during turn 2. The control FIFO must deliver the event exactly once
    to the router (same surface room_app._push_input wires).

    This mirrors the contract the runbook's Sequence E exercises through
    the live HTTP POST /say path — here we drive the FIFO directly,
    same way Phase 6's tests do, but with the room id derived through
    the same _make_room_id helper run_room uses so we know the
    integration point isn't drifting.
    """
    monkeypatch.setattr(room, "ROOM_DIR", tmp_path)

    events: list = []

    def router(ev):
        events.append(ev)

    room_id = _make_room_id("blank", "blank")
    consumer = ControlFifo(room_id, router=router)
    consumer.start()
    try:
        # Tick so the consumer thread opens the FIFO for read.
        time.sleep(0.05)
        write_control_event(room_id, {"kind": "say", "text": "hello"})
        _wait_for(lambda: len(events) >= 1)
        # Settle so any duplicate would surface.
        time.sleep(0.1)
    finally:
        consumer.stop()

    assert events == [("say", "hello")], f"expected exactly one say event, got {events!r}"


# ---------------------------------------------------------------------------
# Test 3 — resume surfaces prior exchange as inject system note (no recap)
# ---------------------------------------------------------------------------


def test_resume_block_is_system_note_not_chat_dump(tmp_path, monkeypatch):
    """Phase 5 structural fix. After exit, `--resume <id> "continue"`
    must NOT replay the prior exchange as a chat-message dump in pane 1.
    Instead, `build_prior_exchange_block` renders an operator-scope
    block that `create_room_profiles` appends to the inject AFTER the
    addendum. The resumed Claude reads its voice anchor first, then the
    history, and never sees a "Continue the conversation from where you
    left off" chat-message preamble.

    We assert two structural properties:

      1. The rendered block carries the operator-scope header phrase
         (`## Prior exchange (resumed by host)`) and the `[NAME] body`
         line shape — the Phase 4 addendum already trains the model
         to read those prefixes.
      2. The rebuilt temp profile's inject ends with that block — i.e.
         it lives in the inject, not in the chat history. The legacy
         "Continue the conversation from where you left off" string
         appears nowhere in the rebuilt inject.
    """
    monkeypatch.setattr(room, "TEMP_PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(room, "ROOM_DIR", tmp_path / "tmp")

    prior_messages = [
        {"name": "BLANK", "text": "first thought from side A", "kind": "turn"},
        {"name": "BLANK", "text": "second thought from side B", "kind": "turn"},
        {"name": "BLANK", "text": "wrap-up from side A",        "kind": "turn"},
    ]
    block = build_prior_exchange_block(prior_messages, tail=30)

    # Operator-scope header + the `Continue naturally` framing
    # (positive instruction, no compliance-forcing suffix).
    assert "## Prior exchange (resumed by host)" in block
    assert "Continue naturally from where you left off" in block
    # Speaker prefixes match the addendum-trained `[NAME]` shape.
    assert "[BLANK] first thought from side A" in block
    assert "[BLANK] wrap-up from side A" in block

    # Build the temp profile with the resume block plumbed through.
    temp_names = create_room_profiles(
        "blank", "blank", user_name="LO", prior_exchange=block,
    )
    assert temp_names == {"blank": "blank-room"}

    inject = yaml.safe_load(
        (tmp_path / "profiles" / "blank-room.yaml").read_text(),
    )["inject"]

    # The block lives at the END of the inject (after the addendum).
    assert inject.rstrip().endswith(block.rstrip()), (
        "prior-exchange block must be the inject tail (operator-scope), "
        "not a chat-message dump"
    )
    # The legacy chat-dump preamble is gone.
    assert "Continue the conversation from where you left off" not in inject


# ---------------------------------------------------------------------------
# Test 4 — slot identity survives an out-of-order arrival on the wire
# ---------------------------------------------------------------------------


def test_out_of_order_arrival_does_not_swap_attribution(tmp_path):
    """Hard case from the Phase 0b ground-truth: two slots fire turns
    with timestamps that arrive interleaved on the resolver. The
    resolver looks at the record's `slot` field, never the `ts` field
    or which channel "changed first". Even when slot 2's record carries
    an EARLIER ts than slot 1's prior record, attribution must follow
    the slot field.

    This is the regression the Phase 8 envelope landed; we re-assert it
    here through the full state-dir round-trip so the Phase 7+12 sign-
    off has the structural guarantee on disk.
    """
    cfg = RoomConfig()
    room_id = _make_room_id("blank", "leguin")
    channels = {
        1: (tmp_path / "slot1_blank.fifo", "fifo"),
        2: (tmp_path / "slot2_leguin.fifo", "fifo"),
    }
    state = RoomState(
        room_id, cfg, "blank", "leguin", channels, base_dir=tmp_path,
    )
    state.write_initial()

    # Slot 1 emits a late ts; slot 2 then a much earlier ts; slot 1
    # again with one in between. None of this should flip attribution.
    stream = [
        (1, _turn_record(1, "blank",  "blank-late",     "2026-05-10T00:00:05.000Z")),
        (2, _turn_record(2, "leguin", "leguin-early",   "2026-05-10T00:00:01.000Z")),
        (1, _turn_record(1, "blank",  "blank-earlier",  "2026-05-10T00:00:02.000Z")),
    ]
    _drive_turns_into_state(state, stream)
    state.update_exit("clean")

    lines = [
        json.loads(l) for l in state.transcript_path.read_text().splitlines()
        if l.strip()
    ]
    # Order on disk = order resolved = order emitted (resolver does not
    # reorder by ts). What we care about: every slot-1 line has profile
    # "blank", every slot-2 line has profile "leguin".
    for r in lines:
        if r["slot"] == 1:
            assert r["profile"] == "blank", r
        elif r["slot"] == 2:
            assert r["profile"] == "leguin", r
        else:
            pytest.fail(f"unexpected slot in record: {r!r}")
    assert [r["text"] for r in lines] == [
        "blank-late",
        "leguin-early",
        "blank-earlier",
    ]


# ---------------------------------------------------------------------------
# Test 5 — live tmux + proxy path (skipped, runbook owns it)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=(
    "requires live tmux + proxies (8090/8091) + a real Anthropic backend; "
    "covered end-to-end by .plan/room-overhaul-runbook.md sequences A/B/E. "
    "The structural pieces (slot resolution, transcript append, control FIFO, "
    "resume inject) are exercised by tests 1-4 above and the per-phase suites."
))
def test_live_tmux_and_proxy_session():
    """Placeholder for the full live-session test. The runbook drives:

      * `ccoral room blank blank "say A then B"` in a real terminal
      * `tmux capture-pane -p` checks for `Read /tmp/...` leaks
      * `cat ~/.ccoral/rooms/<id>/transcript.jsonl | jq` for slot/profile fields

    Marked skip rather than xfail because the live infra isn't a "bug"
    — it's an environmental dependency the test runner doesn't have.
    """
    pass


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def main():
    import shutil

    tests = [
        test_three_turn_blank_blank_exchange_preserves_order_and_slots,
        test_control_fifo_interjection_lands_exactly_once,
        test_resume_block_is_system_note_not_chat_dump,
        test_out_of_order_arrival_does_not_swap_attribution,
    ]

    failed = 0
    for t in tests:
        # Each test gets its own scratch dir; the pytest harness gives
        # this for free, but standalone we have to roll it.
        tmp = Path(tempfile.mkdtemp(prefix="ccoral-room-int-"))

        class _Mp:
            """Minimal monkeypatch shim — only setattr is used."""

            def __init__(self):
                self._undo: list = []

            def setattr(self, target, name, value):
                old = getattr(target, name)
                self._undo.append((target, name, old))
                setattr(target, name, value)

            def undo(self):
                for target, name, old in reversed(self._undo):
                    setattr(target, name, old)

        mp = _Mp()
        try:
            sig = t.__code__.co_varnames[: t.__code__.co_argcount]
            kwargs = {}
            if "tmp_path" in sig:
                kwargs["tmp_path"] = tmp
            if "monkeypatch" in sig:
                kwargs["monkeypatch"] = mp
            t(**kwargs)
            print(f"{t.__name__}: OK")
        except AssertionError as e:
            failed += 1
            print(f"{t.__name__}: FAIL — {e}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"{t.__name__}: ERROR — {type(e).__name__}: {e}")
        finally:
            mp.undo()
            shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print("\nAll tests passed (live tmux/proxy test skipped — see runbook).")


if __name__ == "__main__":
    main()
