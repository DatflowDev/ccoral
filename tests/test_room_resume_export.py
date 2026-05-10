#!/usr/bin/env python3
"""Phase 5 — Resume & Archive UX.

Covers the new surface introduced by Phase 5:

  - build_prior_exchange_block renders an operator-scope system note
    (per INJECT-FRAMING.md), not a chat-message dump.
  - --resume-tail bounds the embedded history (default 30, override
    via RoomConfig.resume_tail).
  - create_room_profiles appends prior_exchange to the inject AFTER
    the addendum (order: base inject -> addendum -> prior exchange).
  - The legacy "Continue the conversation from where you left off"
    chat-message dump path is gone.
  - export_conversation(format="jsonl") writes one JSON record per
    line, verbatim.
  - export_conversation(format="html") writes a single-file standalone
    with inline CSS and the cockpit palette colors.
  - delete_room(yes=True) removes the dir; without yes, the prompt
    callable gates deletion.
  - rename_room moves the dir and updates meta.yaml's room_id field.

Run standalone: `python3 tests/test_room_resume_export.py`
Run under pytest: `pytest tests/test_room_resume_export.py -v`
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from room import (  # noqa: E402
    DEFAULT_ROOM_ADDENDUM,
    RoomConfig,
    RoomState,
    _make_room_id,
    build_prior_exchange_block,
    create_room_profiles,
    delete_room,
    export_conversation,
    rename_room,
)


# ---------------------------------------------------------------------------
# CLI loader (the `ccoral` script has no .py extension)
# ---------------------------------------------------------------------------

def _load_ccoral_cli():
    path = Path(__file__).resolve().parents[1] / "ccoral"
    loader = importlib.machinery.SourceFileLoader("ccoral_cli_p5", str(path))
    spec = importlib.util.spec_from_loader("ccoral_cli_p5", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _seed_room(base: Path, room_id: str, profile1: str, profile2: str,
               records: list, *, exit_reason: str = "clean") -> Path:
    """Write a minimal per-room state dir directly so tests don't have
    to spin up a full proxy/tmux/cockpit. Mirrors what RoomState +
    relay_loop would produce.
    """
    cfg = RoomConfig()
    ch = {1: (base / "s1.fifo", "fifo"), 2: (base / "s2.fifo", "fifo")}
    st = RoomState(room_id, cfg, profile1, profile2, ch, base_dir=base)
    st.write_initial()
    for r in records:
        st.append_turn(r)
    st.update_exit(exit_reason)
    return st.dir


# ---------------------------------------------------------------------------
# 1. Resume = inject system note, NOT chat-message dump
# ---------------------------------------------------------------------------

def test_resume_builds_system_block_in_inject_not_chat_dump(tmp_path, monkeypatch):
    """create_room_profiles, when called with prior_exchange, appends
    that text to the inject AFTER the addendum. The block carries the
    Phase 5 header phrase. The chat-message dump path is gone (no
    pane send happens — verified at the relay_loop layer separately
    via grep)."""
    # Redirect TEMP_PROFILES_DIR + ROOM_DIR to a sandbox so we don't
    # smash the operator's real ~/.ccoral.
    import room as room_mod
    monkeypatch.setattr(room_mod, "TEMP_PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(room_mod, "ROOM_DIR", tmp_path / "tmp")

    prior_block = build_prior_exchange_block([
        {"name": "BLANK", "text": "first thought", "kind": "turn"},
        {"name": "LEGUIN", "text": "second thought", "kind": "turn"},
    ], tail=10)

    # Sanity: the rendered block carries the operator-scope header
    # and the speaker prefixes the addendum trains the model to read.
    assert "## Prior exchange (resumed by host)" in prior_block
    assert "Continue naturally from where you left off" in prior_block
    assert "[BLANK] first thought" in prior_block
    assert "[LEGUIN] second thought" in prior_block

    temp_names = create_room_profiles(
        "blank", "blank", user_name="LO", prior_exchange=prior_block,
    )
    assert temp_names == {"blank": "blank-room"}

    yaml_path = tmp_path / "profiles" / "blank-room.yaml"
    assert yaml_path.exists()
    profile_data = yaml.safe_load(yaml_path.read_text())
    inject = profile_data["inject"]

    # Order: base inject (or empty) -> addendum -> prior exchange.
    # The prior block must come AFTER the addendum's ## header.
    addendum_marker = "Room context (operator-set)"
    prior_marker = "## Prior exchange (resumed by host)"
    assert addendum_marker in inject
    assert prior_marker in inject
    assert inject.index(addendum_marker) < inject.index(prior_marker), \
        "prior exchange block must be appended AFTER the room addendum"

    # And the chat-message dump phrase must NOT be in the inject — it's
    # not how Phase 5 frames it.
    assert "Continue the conversation from where you left off" not in inject
    print("test_resume_builds_system_block_in_inject_not_chat_dump: OK")


# ---------------------------------------------------------------------------
# 2. --resume-tail bounds the embedded history
# ---------------------------------------------------------------------------

def test_resume_tail_honored_default_and_override():
    """Default (30) trims a 100-record stream to the last 30. Custom
    tail=5 trims to 5. tail=0 falls through (no trim)."""
    records = [
        {"name": f"S{i % 2 + 1}", "text": f"line {i}", "kind": "turn"}
        for i in range(100)
    ]

    block_default = build_prior_exchange_block(records, tail=30)
    # Header lines + blank + 30 body lines = 34 lines total.
    body_lines = [
        L for L in block_default.splitlines() if L.startswith("[S")
    ]
    assert len(body_lines) == 30, f"expected 30 tail lines, got {len(body_lines)}"
    assert "line 70" in block_default  # first kept
    assert "line 99" in block_default  # last
    assert "line 69" not in block_default  # dropped

    block_tight = build_prior_exchange_block(records, tail=5)
    body_lines = [
        L for L in block_tight.splitlines() if L.startswith("[S")
    ]
    assert len(body_lines) == 5
    assert "line 95" in block_tight
    assert "line 94" not in block_tight

    # tail=0 means no trim (caller wants everything).
    block_all = build_prior_exchange_block(records, tail=0)
    body_lines = [
        L for L in block_all.splitlines() if L.startswith("[S")
    ]
    assert len(body_lines) == 100

    # And the CLI surfaces it via --resume-tail.
    cli = _load_ccoral_cli()
    parsed = cli._parse_room_args([
        "blank", "leguin", "--resume-tail", "12",
    ])
    assert parsed["resume_tail"] == 12
    cfg = cli._build_room_config(parsed)
    assert cfg.resume_tail == 12
    # Default RoomConfig still 30.
    assert RoomConfig().resume_tail == 30
    print("test_resume_tail_honored_default_and_override: OK")


# ---------------------------------------------------------------------------
# 3. JSONL export shape — one record per line, verbatim
# ---------------------------------------------------------------------------

def test_jsonl_export_emits_one_record_per_line(tmp_path, monkeypatch):
    """export_conversation(format="jsonl") writes the records straight
    through with no kind filter and no shape mutation."""
    # Sandbox the rooms archive root so _resolve_room_id finds our
    # seeded room and nothing else.
    import room as room_mod
    monkeypatch.setattr(room_mod, "ROOMS_ARCHIVE", tmp_path / "rooms")

    base = tmp_path / "rooms"
    base.mkdir()
    rid = _make_room_id("blank", "leguin")
    records = [
        {"name": "CASSIUS", "text": "kick off", "kind": "relay-meta",
         "envelope_kind": "interject"},
        {"name": "BLANK", "text": "alpha", "slot": 1, "kind": "turn"},
        {"name": "LEGUIN", "text": "beta", "slot": 2, "kind": "turn"},
    ]
    _seed_room(base, rid, "blank", "leguin", records)

    out_path = tmp_path / "out.jsonl"
    result = export_conversation(rid, output=str(out_path), format="jsonl")
    assert result == out_path
    lines = out_path.read_text().splitlines()
    assert len(lines) == 3, f"expected 3 lines (no kind filter), got {len(lines)}"
    parsed = [json.loads(L) for L in lines]
    # relay-meta record must survive — jsonl is verbatim.
    assert parsed[0]["kind"] == "relay-meta"
    assert parsed[0]["text"] == "kick off"
    assert parsed[1]["text"] == "alpha"
    assert parsed[2]["text"] == "beta"
    print("test_jsonl_export_emits_one_record_per_line: OK")


# ---------------------------------------------------------------------------
# 4. HTML export — single file, inline CSS, palette colors present
# ---------------------------------------------------------------------------

def test_html_export_single_file_with_palette(tmp_path, monkeypatch):
    """export_conversation(format="html") writes a self-contained html
    file with inline CSS, the cockpit palette colors, and one .turn
    block per non-meta record."""
    import room as room_mod
    monkeypatch.setattr(room_mod, "ROOMS_ARCHIVE", tmp_path / "rooms")

    base = tmp_path / "rooms"
    base.mkdir()
    rid = _make_room_id("blank", "leguin")
    records = [
        {"name": "CASSIUS", "text": "host seed", "kind": "relay-meta"},
        {"name": "BLANK", "text": "first <turn> & body",
         "slot": 1, "kind": "turn"},
        {"name": "LEGUIN", "text": "second turn",
         "slot": 2, "kind": "turn"},
    ]
    _seed_room(base, rid, "blank", "leguin", records)

    out_path = tmp_path / "out.html"
    result = export_conversation(rid, output=str(out_path), format="html")
    assert result == out_path
    html = out_path.read_text()

    # Self-contained: no external <link> or <script src> tags.
    assert "<link" not in html
    assert "<script" not in html

    # Inline CSS present and the palette colors are baked in.
    assert "<style>" in html
    assert "#d7c842" in html  # speaker-1 yellow
    assert "#42c8d7" in html  # speaker-2 cyan

    # relay-meta filtered out.
    assert "host seed" not in html

    # Two turn blocks, one per slot, in order.
    assert "speaker-1" in html
    assert "speaker-2" in html
    assert html.index("speaker-1") < html.index("speaker-2")

    # HTML escaping happened on the body.
    assert "first &lt;turn&gt; &amp; body" in html
    assert "<turn>" not in html.replace("<title>", "").replace(
        "<turn>".replace("<", "&lt;"), ""
    )  # the literal angle brackets in the body are escaped

    # Parseable by the stdlib html.parser as a smoke check.
    from html.parser import HTMLParser

    class _Smoke(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = 0
            self.error = None

        def handle_starttag(self, tag, attrs):
            self.tags += 1

        def error(self, message):  # noqa: A003
            self.error = message

    p = _Smoke()
    p.feed(html)
    assert p.tags > 5
    print("test_html_export_single_file_with_palette: OK")


# ---------------------------------------------------------------------------
# 5. delete_room — --yes deletes silently; no-yes prompt gates removal
# ---------------------------------------------------------------------------

def test_delete_room_with_yes_removes_dir_silently(tmp_path):
    """delete_room(yes=True) removes the dir without invoking the
    prompt. delete_room(yes=False, prompt=lambda _: 'n') refuses
    cleanly. delete_room(yes=False, prompt=lambda _: 'y') deletes."""
    import io

    base = tmp_path / "rooms"
    base.mkdir()
    rid_a = _make_room_id("blank", "leguin")
    _seed_room(base, rid_a, "blank", "leguin", [
        {"name": "BLANK", "text": "x", "slot": 1, "kind": "turn"},
    ])
    rid_b = "2026-05-10_999999_blank-blank"
    _seed_room(base, rid_b, "blank", "blank", [
        {"name": "BLANK", "text": "y", "slot": 1, "kind": "turn"},
    ])

    target_a = base / rid_a
    target_b = base / rid_b
    assert target_a.exists()
    assert target_b.exists()

    # --yes: silent delete.
    out = io.StringIO()
    ok = delete_room(rid_a, yes=True, base=base, stream=out)
    assert ok is True
    assert not target_a.exists()
    assert "deleted" in out.getvalue()

    # No --yes, refusal: dir survives.
    out = io.StringIO()
    ok = delete_room(rid_b, yes=False, base=base, stream=out,
                     prompt=lambda msg: "n")
    assert ok is False
    assert target_b.exists()
    assert "aborted" in out.getvalue()

    # No --yes, accept: dir gone.
    out = io.StringIO()
    ok = delete_room(rid_b, yes=False, base=base, stream=out,
                     prompt=lambda msg: "y")
    assert ok is True
    assert not target_b.exists()

    # Miss: returns False without raising.
    out = io.StringIO()
    ok = delete_room("does-not-exist", yes=True, base=base, stream=out)
    assert ok is False
    assert "not found" in out.getvalue()
    print("test_delete_room_with_yes_removes_dir_silently: OK")


# ---------------------------------------------------------------------------
# 6. rename_room — moves dir + rewrites meta.yaml room_id field
# ---------------------------------------------------------------------------

def test_rename_room_moves_dir_and_updates_meta(tmp_path):
    """rename_room moves the source dir to <new_id> under the same
    base, and the meta.yaml room_id field reflects the new id."""
    import io

    base = tmp_path / "rooms"
    base.mkdir()
    rid = "2026-05-10_120000_blank-leguin"
    _seed_room(base, rid, "blank", "leguin", [
        {"name": "BLANK", "text": "stuff", "slot": 1, "kind": "turn"},
    ])

    src = base / rid
    new_id = "archived/coral-thread"
    dst = base / new_id

    out = io.StringIO()
    ok = rename_room(rid, new_id, base=base, stream=out)
    assert ok is True
    assert not src.exists()
    assert dst.exists()
    assert (dst / "meta.yaml").exists()
    assert (dst / "transcript.jsonl").exists()

    meta = yaml.safe_load((dst / "meta.yaml").read_text())
    assert meta["room_id"] == new_id, \
        f"expected room_id to be {new_id}, got {meta.get('room_id')}"

    # Target-exists collision is reported, not clobbered.
    rid2 = "2026-05-10_130000_blank-leguin"
    _seed_room(base, rid2, "blank", "leguin", [
        {"name": "BLANK", "text": "z", "slot": 1, "kind": "turn"},
    ])
    out = io.StringIO()
    ok = rename_room(rid2, new_id, base=base, stream=out)
    assert ok is False
    assert "exists" in out.getvalue()
    # The collision target survived unmoved.
    assert (base / rid2).exists()
    print("test_rename_room_moves_dir_and_updates_meta: OK")


# ---------------------------------------------------------------------------
# 7. CLI surface — every Phase 5 flag/subverb parses
# ---------------------------------------------------------------------------

def test_cli_phase5_surface_parses():
    """--resume-tail, --format md|jsonl|html, --yes, delete, rename
    all reach the parsed dict in the right shape."""
    cli = _load_ccoral_cli()

    parsed = cli._parse_room_args([
        "--export", "last", "--format", "jsonl", "--output", "x.jsonl",
    ])
    assert parsed["export"] == "last"
    assert parsed["export_format"] == "jsonl"
    assert parsed["export_output"] == "x.jsonl"

    parsed = cli._parse_room_args([
        "--export", "last", "--format", "html",
    ])
    assert parsed["export_format"] == "html"

    parsed = cli._parse_room_args(["delete", "last", "--yes"])
    assert parsed["delete_id"] == "last"
    assert parsed["yes"] is True

    parsed = cli._parse_room_args(["delete", "abc"])
    assert parsed["delete_id"] == "abc"
    assert parsed["yes"] is False

    parsed = cli._parse_room_args([
        "rename", "last", "archived/quiet-talk",
    ])
    assert parsed["rename_from"] == "last"
    assert parsed["rename_to"] == "archived/quiet-talk"

    # --resume-tail honored at the parser layer.
    parsed = cli._parse_room_args([
        "blank", "leguin", "--resume-tail", "5",
    ])
    assert parsed["resume_tail"] == 5
    print("test_cli_phase5_surface_parses: OK")


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def main():
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


if __name__ == "__main__":
    main()
