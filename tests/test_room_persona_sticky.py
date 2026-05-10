#!/usr/bin/env python3
"""Phase 8 — persona stickiness regression suite.

These tests prove the structural fix for the duplicate-profile collision
and near-simultaneous re-attribution race documented in
.plan/room-overhaul.md (Phase 0b ground truth):

  1. Every turn record carries an explicit `slot` (server-side stamp).
  2. The orchestrator attributes turns by `record["slot"]` — never by
     filesystem ordering, sink-path identity, or "which file changed."
  3. The duplicate-profile case (`profile1 == profile2 == "blank"`)
     resolves cleanly because the slot prefix is independent of the
     profile name.

Run standalone: `python3 tests/test_room_persona_sticky.py`
Run under pytest: `pytest tests/test_room_persona_sticky.py -v`
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from room import resolve_record_slot, _setup_turn_channel, ROOM_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(slot, profile, ts, text="hello there from the room"):
    """Build a Phase-8-shaped turn record. Text is long enough to clear
    the server's `_should_emit_turn_record` 20-char gate so the same
    fixture shape works in stricter integration tests later."""
    return {
        "ts": ts,
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "text": text,
        "lane": "primary",
        "request_id": f"req_{slot}_{ts}",
        "profile": profile,
        "slot": slot,
    }


def _attribute(records_with_reader_slot):
    """Resolve every record to its (slot, profile, text) triple using
    the same code path the relay uses. Returns a list — order preserved.

    Each input element is (reader_slot, record_dict). `reader_slot` is
    the FIFO/JSONL channel the bytes came in on; `record_dict` is the
    payload the proxy emitted. The resolver should always trust the
    record's `slot` field when present.
    """
    out = []
    for reader_slot, rec in records_with_reader_slot:
        slot, _legacy = resolve_record_slot(rec, reader_slot)
        out.append((slot, rec.get("profile"), rec.get("text")))
    return out


# ---------------------------------------------------------------------------
# Tests — direct on the slot-resolution helper
# ---------------------------------------------------------------------------

def test_slot_from_record_is_authoritative():
    """When the record carries `slot=2`, attribution is 2 even if the
    record arrived on the slot-1 reader. (The reader-slot fallback
    must NEVER override an explicit slot field.)"""
    rec = _make_record(slot=2, profile="leguin", ts="2026-05-10T00:00:01.000Z")
    slot, legacy = resolve_record_slot(rec, reader_slot=1)
    assert slot == 2, f"expected slot=2 from record, got {slot}"
    assert legacy is False, "record had explicit slot — should not be legacy"
    print("test_slot_from_record_is_authoritative: OK")


def test_legacy_record_falls_back_to_reader_slot():
    """A record with no `slot` field falls back to the reader's slot
    and flags the legacy path. This is the Phase 8 backwards-compat
    shim — Phase 12 verifies it's unreachable in real sessions."""
    rec = {
        "ts": "2026-05-10T00:00:00.000Z",
        "text": "legacy producer",
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
    }
    slot, legacy = resolve_record_slot(rec, reader_slot=2)
    assert slot == 2
    assert legacy is True
    print("test_legacy_record_falls_back_to_reader_slot: OK")


def test_malformed_slot_falls_back():
    """A record whose `slot` isn't 1 or 2 (string, None, 7, ...) falls
    back to the reader slot. The relay never wedges on bad input."""
    for bad in (None, "1", "2", 0, 3, [1], {"x": 1}):
        rec = {"slot": bad, "text": "x"}
        slot, legacy = resolve_record_slot(rec, reader_slot=1)
        assert slot == 1, f"bad={bad!r} should fall back to reader slot 1, got {slot}"
        assert legacy is True
    print("test_malformed_slot_falls_back: OK")


# ---------------------------------------------------------------------------
# Tests — synthetic JSONL streams (the deliberate races)
# ---------------------------------------------------------------------------

def test_deterministic_ordering_distinct_profiles():
    """Slot 1 (BLANK) speaks, then slot 2 (LEGUIN), three exchanges.
    Every turn must attribute to the right slot AND profile."""
    stream = [
        (1, _make_record(1, "blank",  "2026-05-10T00:00:00.000Z", "blank one")),
        (2, _make_record(2, "leguin", "2026-05-10T00:00:01.000Z", "leguin one")),
        (1, _make_record(1, "blank",  "2026-05-10T00:00:02.000Z", "blank two")),
        (2, _make_record(2, "leguin", "2026-05-10T00:00:03.000Z", "leguin two")),
        (1, _make_record(1, "blank",  "2026-05-10T00:00:04.000Z", "blank three")),
        (2, _make_record(2, "leguin", "2026-05-10T00:00:05.000Z", "leguin three")),
    ]
    attribution = _attribute(stream)
    expected = [
        (1, "blank",  "blank one"),
        (2, "leguin", "leguin one"),
        (1, "blank",  "blank two"),
        (2, "leguin", "leguin two"),
        (1, "blank",  "blank three"),
        (2, "leguin", "leguin three"),
    ]
    assert attribution == expected, f"mismatch: got {attribution!r}"
    print("test_deterministic_ordering_distinct_profiles: OK")


def test_near_simultaneous_emissions_distinct_profiles():
    """Both slots fire turns with timestamps within 1ms of each other.
    Even if the records arrive interleaved on a single tick, slot
    identity must hold — there is no mtime tiebreak in the resolver."""
    same_ts = "2026-05-10T00:00:00.000Z"
    stream = [
        (2, _make_record(2, "leguin", same_ts, "leguin says hi")),
        (1, _make_record(1, "blank",  same_ts, "blank says hi")),
        (1, _make_record(1, "blank",  same_ts, "blank says again")),
        (2, _make_record(2, "leguin", same_ts, "leguin says again")),
    ]
    attribution = _attribute(stream)
    # Order of arrival is preserved; what we care about is that NONE
    # of the slot-2 turns ended up tagged as slot 1, and vice versa.
    for slot, profile, _ in attribution:
        if profile == "blank":
            assert slot == 1, f"blank turn attributed to slot {slot}, expected 1"
        elif profile == "leguin":
            assert slot == 2, f"leguin turn attributed to slot {slot}, expected 2"
    print("test_near_simultaneous_emissions_distinct_profiles: OK")


def test_out_of_order_timestamps():
    """Slot 2 emits a record whose `ts` is BEFORE slot 1's prior
    record. The resolver does not look at `ts` for attribution —
    slot identity is in the `slot` field — so out-of-order arrival
    must not flip attribution."""
    stream = [
        (1, _make_record(1, "blank",  "2026-05-10T00:00:05.000Z", "blank-late")),
        (2, _make_record(2, "leguin", "2026-05-10T00:00:01.000Z", "leguin-early")),
        (1, _make_record(1, "blank",  "2026-05-10T00:00:02.000Z", "blank-earlier")),
    ]
    attribution = _attribute(stream)
    assert attribution == [
        (1, "blank",  "blank-late"),
        (2, "leguin", "leguin-early"),
        (1, "blank",  "blank-earlier"),
    ], f"out-of-order attribution drifted: {attribution!r}"
    print("test_out_of_order_timestamps: OK")


def test_duplicate_profile_collision():
    """The deterministic Phase 0b finding: `ccoral room blank blank`
    used to collide because both proxies wrote to the same sink path
    and the relay attributed by mtime. With Phase 8 in place, the
    `slot` field disambiguates — slot 1 stays slot 1 even when both
    sides report `profile: "blank"`."""
    stream = [
        (1, _make_record(1, "blank", "2026-05-10T00:00:00.000Z", "side A intro")),
        (2, _make_record(2, "blank", "2026-05-10T00:00:00.500Z", "side B intro")),
        (1, _make_record(1, "blank", "2026-05-10T00:00:01.000Z", "side A reply")),
        (2, _make_record(2, "blank", "2026-05-10T00:00:01.500Z", "side B reply")),
        # The classic race: simultaneous end-of-turn from both sides,
        # delivered in REVERSE order to the resolver.
        (2, _make_record(2, "blank", "2026-05-10T00:00:02.000Z", "side B cap")),
        (1, _make_record(1, "blank", "2026-05-10T00:00:02.000Z", "side A cap")),
    ]
    attribution = _attribute(stream)
    a_lines = [text for slot, _, text in attribution if slot == 1]
    b_lines = [text for slot, _, text in attribution if slot == 2]
    assert a_lines == ["side A intro", "side A reply", "side A cap"], \
        f"slot-1 attribution wrong: {a_lines!r}"
    assert b_lines == ["side B intro", "side B reply", "side B cap"], \
        f"slot-2 attribution wrong: {b_lines!r}"
    print("test_duplicate_profile_collision: OK")


def test_duplicate_profile_with_reader_misroute_is_corrected():
    """Hard case: even if the reader-slot delivery were somehow wrong
    (a deliberate test stress; should never happen in production
    because each FIFO is per-slot), the record's `slot` field
    overrides the reader slot. This is the Phase 8 contract: the
    record is the source of truth, NOT the channel that delivered it."""
    # Reader says slot 1 delivered the bytes, but the record claims
    # slot 2. The record wins.
    rec = _make_record(slot=2, profile="blank", ts="2026-05-10T00:00:00.000Z",
                       text="actually slot two even though channel said one")
    slot, legacy = resolve_record_slot(rec, reader_slot=1)
    assert slot == 2
    assert legacy is False
    print("test_duplicate_profile_with_reader_misroute_is_corrected: OK")


# ---------------------------------------------------------------------------
# Tests — slot-prefixed sink paths (the path-collision fix)
# ---------------------------------------------------------------------------

def test_slot_prefixed_channel_paths_distinct_for_same_profile():
    """The Phase 0b deterministic bug: `room blank blank` made BOTH
    proxies write to `blank_response.txt`. After Phase 8, each slot
    gets a unique `slot{N}_<base>` path that cannot collide."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    p1, kind1 = _setup_turn_channel(1, "blank")
    p2, kind2 = _setup_turn_channel(2, "blank")
    try:
        assert p1 != p2, f"slot 1 and slot 2 both got the same path {p1}"
        assert "slot1_blank" in p1.name
        assert "slot2_blank" in p2.name
        assert kind1 in ("fifo", "jsonl")
        assert kind2 in ("fifo", "jsonl")
    finally:
        for p in (p1, p2):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    print("test_slot_prefixed_channel_paths_distinct_for_same_profile: OK")


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_slot_from_record_is_authoritative,
        test_legacy_record_falls_back_to_reader_slot,
        test_malformed_slot_falls_back,
        test_deterministic_ordering_distinct_profiles,
        test_near_simultaneous_emissions_distinct_profiles,
        test_out_of_order_timestamps,
        test_duplicate_profile_collision,
        test_duplicate_profile_with_reader_misroute_is_corrected,
        test_slot_prefixed_channel_paths_distinct_for_same_profile,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"{t.__name__}: FAIL — {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
