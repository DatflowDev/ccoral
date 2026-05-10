#!/usr/bin/env python3
"""Phase 3 — RoomConfig threading + per-room state dir + CLI flag parsing.

Covers the new surface introduced by Phase 3:

  - RoomConfig defaults match the pre-Phase-3 module-level constants.
  - The CLI arg-walker (_parse_room_args + _build_room_config) accepts
    every documented flag and produces a correctly-shaped RoomConfig.
  - The per-room state dir lifecycle: write_initial -> append_turn ->
    update_exit transitions live -> stopped with the right exit_reason.
  - transcript.jsonl is append-only and one JSON object per line.
  - meta.yaml lifecycle for both signal and turn_limit exit reasons.
  - room_ls + _resolve_room_id select the right rooms in a populated
    state dir.

Run standalone: `python3 tests/test_room_config.py`
Run under pytest: `pytest tests/test_room_config.py -v`
"""
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from room import (  # noqa: E402
    RoomConfig,
    RoomState,
    _list_room_dirs,
    _make_room_id,
    _resolve_room_id,
    room_ls,
)


# ---------------------------------------------------------------------------
# CLI loader (the `ccoral` script has no .py extension; load it manually so
# we can drive `_parse_room_args` and `_build_room_config` from pytest)
# ---------------------------------------------------------------------------

def _load_ccoral_cli():
    path = Path(__file__).resolve().parents[1] / "ccoral"
    loader = importlib.machinery.SourceFileLoader("ccoral_cli", str(path))
    spec = importlib.util.spec_from_loader("ccoral_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. RoomConfig defaults preserve pre-Phase-3 behavior
# ---------------------------------------------------------------------------

def test_room_config_defaults_match_pre_phase_3_constants():
    """The RoomConfig dataclass replaces the USER_NAME / BASE_PORT /
    TMUX_SESSION / BACKPRESSURE_* module constants. Defaults must equal
    the literal values those constants used to carry, so callers that
    don't pass any flags see identical behavior."""
    cfg = RoomConfig()
    assert cfg.user_name == "CASSIUS"
    assert cfg.base_port == 8090
    assert cfg.tmux_session_prefix == "room"
    assert cfg.backpressure_turns == 2
    assert cfg.backpressure_timeout_s == 60.0
    assert cfg.turn_limit is None
    assert cfg.max_chars_per_turn is None
    assert cfg.seed1 is None
    assert cfg.seed2 is None
    assert cfg.moderator is None
    assert cfg.moderator_cadence == 4
    # session_for derives from the prefix so two rooms with different
    # prefixes can run side by side.
    assert cfg.session_for("blank") == "room-blank"
    cfg2 = RoomConfig(tmux_session_prefix="alt")
    assert cfg2.session_for("blank") == "alt-blank"
    print("test_room_config_defaults_match_pre_phase_3_constants: OK")


# ---------------------------------------------------------------------------
# 2. CLI flag parsing — every Phase 3 flag must round-trip into RoomConfig
# ---------------------------------------------------------------------------

def test_cli_flag_parsing_full_surface():
    """Every Phase 3 flag is accepted by the hand-rolled arg-walker and
    the resulting RoomConfig carries the expected values. Also verifies
    the legacy positional `topic` is captured into topic_parts (the
    cmd_room call site promotes it to seed1 if --seed1 is unset)."""
    cli = _load_ccoral_cli()
    args = [
        "blank", "leguin",
        "--port", "9100",
        "--user", "LO",
        "--turn-limit", "4",
        "--max-chars-per-turn", "500",
        "--backpressure-turns", "3",
        "--backpressure-timeout", "90.0",
        "--seed1", "you start",
        "--seed2", "you start too",
        "--moderator", "vonnegut",
        "--moderator-cadence", "6",
    ]
    parsed = cli._parse_room_args(args)
    assert parsed["profiles"] == ["blank", "leguin"]
    assert parsed["port"] == 9100
    assert parsed["user"] == "LO"
    assert parsed["turn_limit"] == 4
    assert parsed["max_chars_per_turn"] == 500
    assert parsed["backpressure_turns"] == 3
    assert parsed["backpressure_timeout"] == 90.0
    assert parsed["seed1"] == "you start"
    assert parsed["seed2"] == "you start too"
    assert parsed["moderator"] == "vonnegut"
    assert parsed["moderator_cadence"] == 6

    cfg = cli._build_room_config(parsed)
    assert cfg.base_port == 9100
    assert cfg.user_name == "LO"
    assert cfg.turn_limit == 4
    assert cfg.max_chars_per_turn == 500
    assert cfg.backpressure_turns == 3
    assert cfg.backpressure_timeout_s == 90.0
    assert cfg.seed1 == "you start"
    assert cfg.seed2 == "you start too"
    assert cfg.moderator == "vonnegut"
    assert cfg.moderator_cadence == 6

    # Legacy positional topic with no profiles-overflow: should land in
    # topic_parts and (in cmd_room) be promoted to seed1 if --seed1 is
    # unset. Test the parser layer: positional after the second profile
    # is captured.
    parsed2 = cli._parse_room_args(["blank", "leguin", "a topic"])
    assert parsed2["topic_parts"] == ["a topic"]
    assert parsed2["seed1"] is None
    print("test_cli_flag_parsing_full_surface: OK")


def test_cli_port_flag_changes_session_names():
    """A non-default --port must NOT collide with the default 8090 room.
    The base_port reaches start_proxies (CCORAL_PORT env per slot) and
    setup_tmux/relay_loop derive distinct tmux session names from it
    only via tmux_session_prefix — but the per-port distinction means
    two rooms can co-exist as long as tmux_session_prefix differs OR
    profiles differ. Here we check the simpler invariant: --port does
    set base_port on the resolved RoomConfig and leaves the prefix at
    its default (so rooms with different profiles + different ports do
    not collide on either dimension)."""
    cli = _load_ccoral_cli()
    parsed = cli._parse_room_args(["blank", "blank", "--port", "9100"])
    cfg = cli._build_room_config(parsed)
    assert cfg.base_port == 9100
    assert cfg.session_for("blank") == "room-blank"
    # The other-room baseline:
    cfg_default = RoomConfig()
    assert cfg_default.base_port == 8090
    assert cfg.base_port != cfg_default.base_port
    print("test_cli_port_flag_changes_session_names: OK")


# ---------------------------------------------------------------------------
# 3. Per-room state dir lifecycle
# ---------------------------------------------------------------------------

def test_room_state_lifecycle_writes_three_files():
    """write_initial creates config.yaml, meta.yaml, transcript.jsonl.
    meta.yaml has state=live and exit_reason=None. config.yaml carries
    the resolved RoomConfig under `config`."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = RoomConfig(base_port=9100, user_name="LO", turn_limit=3)
        rid = _make_room_id("blank", "leguin")
        ch = {1: (base / "s1.fifo", "fifo"), 2: (base / "s2.fifo", "fifo")}
        st = RoomState(rid, cfg, "blank", "leguin", ch, base_dir=base)
        st.write_initial()

        assert st.config_path.exists()
        assert st.meta_path.exists()
        assert st.transcript_path.exists()

        config_yaml = yaml.safe_load(st.config_path.read_text())
        assert config_yaml["profile1"] == "blank"
        assert config_yaml["profile2"] == "leguin"
        assert config_yaml["config"]["base_port"] == 9100
        assert config_yaml["config"]["user_name"] == "LO"
        assert config_yaml["config"]["turn_limit"] == 3

        meta = yaml.safe_load(st.meta_path.read_text())
        assert meta["state"] == "live"
        assert meta["exit_reason"] is None
        assert meta["ports"] == {"slot1": 9100, "slot2": 9101}
        assert meta["profiles"] == ["blank", "leguin"]
        # Initial transcript is empty (touch only).
        assert st.transcript_path.read_text() == ""
        print("test_room_state_lifecycle_writes_three_files: OK")


def test_meta_state_transitions_live_to_stopped_on_clean_exit():
    """update_exit("clean") flips state from live to stopped and stamps
    exit_reason. Phase 11's room watcher reads `state` to decide which
    rooms to attach to."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        st = RoomState(
            _make_room_id("a", "b"), RoomConfig(),
            "a", "b",
            {1: (base / "x.fifo", "fifo"), 2: (base / "y.fifo", "fifo")},
            base_dir=base,
        )
        st.write_initial()
        meta = yaml.safe_load(st.meta_path.read_text())
        assert meta["state"] == "live"

        st.update_exit("clean")
        meta = yaml.safe_load(st.meta_path.read_text())
        assert meta["state"] == "stopped"
        assert meta["exit_reason"] == "clean"
        assert meta.get("ended") is not None
        print("test_meta_state_transitions_live_to_stopped_on_clean_exit: OK")


def test_meta_exit_reason_matches_signal_vs_turn_limit():
    """Different exit causes stamp different exit_reason values. Both
    paths must round-trip through update_exit unchanged so Phase 11
    can distinguish them in the watcher UI."""
    with tempfile.TemporaryDirectory() as td:
        for reason in ("signal", "turn_limit", "error", "clean"):
            base = Path(td) / reason
            base.mkdir()
            st = RoomState(
                _make_room_id("a", "b"), RoomConfig(),
                "a", "b",
                {1: (base / "x.fifo", "fifo"), 2: (base / "y.fifo", "fifo")},
                base_dir=base,
            )
            st.write_initial()
            st.update_exit(reason)
            meta = yaml.safe_load(st.meta_path.read_text())
            assert meta["state"] == "stopped"
            assert meta["exit_reason"] == reason, \
                f"expected {reason}, got {meta['exit_reason']}"
        print("test_meta_exit_reason_matches_signal_vs_turn_limit: OK")


# ---------------------------------------------------------------------------
# 4. Transcript append-only invariant
# ---------------------------------------------------------------------------

def test_transcript_jsonl_is_append_only_and_well_formed():
    """append_turn writes one JSON object per line and never truncates
    the file. Also: re-opening for append after update_exit still
    works — there is no implicit close on the lifecycle transition."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        st = RoomState(
            _make_room_id("blank", "leguin"), RoomConfig(),
            "blank", "leguin",
            {1: (base / "x.fifo", "fifo"), 2: (base / "y.fifo", "fifo")},
            base_dir=base,
        )
        st.write_initial()
        records = [
            {"name": "BLANK", "text": "first", "slot": 1, "kind": "turn"},
            {"name": "LEGUIN", "text": "second", "slot": 2, "kind": "turn"},
            {"name": "BLANK", "text": "third with unicode — dash",
             "slot": 1, "kind": "turn"},
            {"name": "SYSTEM", "text": "backpressure", "kind": "relay-meta"},
            {"name": "LEGUIN", "text": "fourth", "slot": 2, "kind": "turn"},
        ]
        for r in records:
            st.append_turn(r)

        lines = st.transcript_path.read_text().splitlines()
        assert len(lines) == len(records), \
            f"expected {len(records)} lines, got {len(lines)}"
        round_tripped = [json.loads(L) for L in lines]
        assert round_tripped == records

        # Append-only after exit transition.
        st.update_exit("clean")
        st.append_turn({"name": "POST", "text": "after exit",
                        "kind": "relay-meta"})
        lines2 = st.transcript_path.read_text().splitlines()
        assert len(lines2) == len(records) + 1
        # And the originals are still there byte-for-byte.
        assert lines2[: len(records)] == lines
        print("test_transcript_jsonl_is_append_only_and_well_formed: OK")


# ---------------------------------------------------------------------------
# 5. Seed1/seed2 placement — the parser layer (relay_loop wiring is the
#    integration concern; this confirms the CLI hands seeds through to
#    the right RoomConfig fields, which is what relay_loop reads).
# ---------------------------------------------------------------------------

def test_seed_flags_reach_config_fields():
    """--seed1 reaches config.seed1; --seed2 reaches config.seed2.
    relay_loop's _relay_runner sends each to the corresponding pane
    (tested by inspection — there is no live tmux in CI). When --seed1
    is unset and a positional topic is given, cmd_room (not the parser)
    promotes the topic to seed1; that promotion lives in cmd_room and
    is exercised by the smoke run on the workstation, not pytest."""
    cli = _load_ccoral_cli()
    parsed = cli._parse_room_args([
        "blank", "leguin", "--seed1", "hi one", "--seed2", "hi two",
    ])
    cfg = cli._build_room_config(parsed)
    assert cfg.seed1 == "hi one"
    assert cfg.seed2 == "hi two"

    # Asymmetric: only seed2 set; seed1 stays None so relay_loop falls
    # back to the legacy `topic` parameter (or leaves slot 1 cold).
    parsed2 = cli._parse_room_args(["blank", "leguin", "--seed2", "B only"])
    cfg2 = cli._build_room_config(parsed2)
    assert cfg2.seed1 is None
    assert cfg2.seed2 == "B only"
    print("test_seed_flags_reach_config_fields: OK")


# ---------------------------------------------------------------------------
# 6. room_ls + _resolve_room_id
# ---------------------------------------------------------------------------

def test_room_ls_lists_rooms_newest_first_with_state():
    """A populated rooms dir lists every entry, sorted newest first,
    with the right state column. _resolve_room_id("last") returns the
    same dir that appears at the top of the listing."""
    import time as _time
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # Two rooms; second is created later so it should sort first.
        r1 = RoomState(
            "2026-05-10_010000_blank-leguin", RoomConfig(),
            "blank", "leguin",
            {1: (base / "a.fifo", "fifo"), 2: (base / "b.fifo", "fifo")},
            base_dir=base,
        )
        r1.write_initial()
        r1.append_turn({"name": "BLANK", "text": "first turn",
                        "slot": 1, "kind": "turn"})
        r1.update_exit("clean")
        # Force mtime ordering by sleeping the clock forward; tempfs
        # mtime resolution can be coarse on some hosts.
        _time.sleep(0.05)
        r2 = RoomState(
            "2026-05-10_020000_blank-blank", RoomConfig(base_port=9100),
            "blank", "blank",
            {1: (base / "c.fifo", "fifo"), 2: (base / "d.fifo", "fifo")},
            base_dir=base,
        )
        r2.write_initial()
        # r2 stays live (no update_exit) — the listing should reflect that.

        dirs = _list_room_dirs(base=base)
        assert [d.name for d in dirs] == [
            "2026-05-10_020000_blank-blank",
            "2026-05-10_010000_blank-leguin",
        ], f"got {[d.name for d in dirs]}"

        out = io.StringIO()
        room_ls(base=base, stream=out)
        text = out.getvalue()
        assert "2026-05-10_020000_blank-blank" in text
        assert "2026-05-10_010000_blank-leguin" in text
        # The live room (r2) has no exit; state should be "live".
        # The stopped room (r1) shows "stopped".
        # We don't assert position because tabular formatting may shift
        # widths; instead grep the state column tokens are both present.
        assert "live" in text
        assert "stopped" in text

        # _resolve_room_id("last") -> the newer room.
        last = _resolve_room_id("last", base=base)
        assert last is not None and last.name == "2026-05-10_020000_blank-blank"
        # Prefix match works.
        prefix = _resolve_room_id("2026-05-10_010000", base=base)
        assert prefix is not None and prefix.name == \
            "2026-05-10_010000_blank-leguin"
        # Miss returns None.
        assert _resolve_room_id("does-not-exist", base=base) is None
        print("test_room_ls_lists_rooms_newest_first_with_state: OK")


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_room_config_defaults_match_pre_phase_3_constants,
        test_cli_flag_parsing_full_surface,
        test_cli_port_flag_changes_session_names,
        test_room_state_lifecycle_writes_three_files,
        test_meta_state_transitions_live_to_stopped_on_clean_exit,
        test_meta_exit_reason_matches_signal_vs_turn_limit,
        test_transcript_jsonl_is_append_only_and_well_formed,
        test_seed_flags_reach_config_fields,
        test_room_ls_lists_rooms_newest_first_with_state,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"{t.__name__}: FAIL - {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
