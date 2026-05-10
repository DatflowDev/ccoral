#!/usr/bin/env python3
"""Smoke tests for the CC 2.1.138 parser refresh."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import parse_system_prompt, apply_profile, rebuild_system_prompt
from server import model_tier, strip_message_tags
from refusal import detect_refusal, all_refusals, REFUSAL_PATTERNS
from reminders import classify_reminder
from tool_scrub import scrub_tool_descriptions
from lanes import detect_lane, LANE_FINGERPRINTS

FIXTURES = Path(__file__).parent / "fixtures"

# Reusable fixture strings — bare literals to keep test readable. The
# `<system-reminder>` opener/closer are spelled out in code, not inlined
# elsewhere, so the test stays robust to harness display quirks.
_OPEN = "<system-reminder>"
_CLOSE = "</system-reminder>"
_NAG = (
    _OPEN
    + "The task tools haven't been used recently. If you're working on a "
      "multi-file change, consider using them."
    + _CLOSE
)
_DEFERRED = (
    _OPEN
    + "The following deferred tools are now available via ToolSearch: "
      "NotebookEdit, MultiEdit, ..."
    + _CLOSE
)
_HOOK = (
    _OPEN
    + "SessionStart hook additional context: # [project] recent context, "
      "2026-05-09 ..."
    + _CLOSE
)


def assert_in(name, blocks, where="parse"):
    found = {s.name for b in blocks for s in b.sections}
    assert name in found, f"[{where}] expected section '{name}' in {sorted(found)}"


def assert_not_in(name, blocks, where="parse"):
    found = {s.name for b in blocks for s in b.sections}
    assert name not in found, f"[{where}] section '{name}' should be absent"


def test_main_fixture():
    data = json.loads((FIXTURES / "main-2.1.138.json").read_text())
    blocks = parse_system_prompt(data["system"])
    for name in ("identity", "executing_actions", "system_reminder", "environment", "git_commit"):
        assert_in(name, blocks, "main")
    print("test_main_fixture: OK")


def test_subagent_fixture():
    data = json.loads((FIXTURES / "subagent-2.1.138.json").read_text())
    blocks = parse_system_prompt(data["system"])
    assert_in("agent_thread_notes", blocks, "subagent")
    assert_not_in("harness", blocks, "subagent")
    assert_not_in("git_commit", blocks, "subagent")
    print("test_subagent_fixture: OK")


def test_apply_profile_main():
    data = json.loads((FIXTURES / "main-2.1.138.json").read_text())
    blocks = parse_system_prompt(data["system"])
    profile = {
        "inject": "You are Vonnegut. Stay in character.",
        "preserve": ["environment", "current_date", "claude_md", "harness"],
        "minimal": False,
    }
    blocks = apply_profile(blocks, profile)
    out = rebuild_system_prompt(blocks)
    text = "\n".join(b.get("text", "") for b in out)
    assert "You are Vonnegut" in text, "inject text missing"
    assert "# Executing actions with care" not in text, "executing_actions not stripped"
    assert "IMPORTANT: Assist with authorized" not in text, "security_policy not stripped"
    print("test_apply_profile_main: OK")


def test_apply_profile_subagent_fixture():
    """When apply_to_subagents=true, server.py dispatch routes subagent calls
    through modify_request_body → apply_profile, the same pipeline used for
    the main worker. Verify the building block: apply_profile on the subagent
    fixture replaces identity with inject and strips agent_thread_notes."""
    data = json.loads((FIXTURES / "subagent-2.1.138.json").read_text())
    blocks = parse_system_prompt(data["system"])
    profile = {
        "inject": "You are Vonnegut. Stay in character.",
        "preserve": ["environment", "current_date", "claude_md"],
    }
    blocks = apply_profile(blocks, profile)
    out = rebuild_system_prompt(blocks)
    text = "\n".join(b.get("text", "") for b in out)
    # Inject replaced identity
    assert "You are Vonnegut" in text, "inject text missing"
    # Original subagent identity opener replaced
    assert "You are an agent for Claude Code" not in text, (
        "default subagent identity not stripped"
    )
    # Agent-thread notes section stripped (cwd-reset/no-emoji guidance)
    assert "Agent threads always have their cwd reset" not in text, (
        "agent_thread_notes not stripped"
    )
    # Environment section survived (it's in preserve)
    assert "Working directory" in text, "environment section was incorrectly stripped"
    print("test_apply_profile_subagent_fixture: OK")


def test_strip_message_tags_cross_role():
    """Smart-strip: nag reminders are removed; functional reminders (deferred
    tools, skills, MCP, hook outputs, IDE context) are preserved across all
    roles and text-bearing block types. Thinking and redacted_thinking blocks
    are protocol-protected and never touched. tool_use blocks untouched.

    Fixture mirrors categories from a captured Opus 4.7 dump (raw-eni-
    executor-claude-opu.json: 32 reminders, 9 nags, 23 functional)."""
    body = {
        "messages": [
            # 0: User string with one nag and one hook. Only nag stripped.
            {"role": "user", "content": f"hello {_NAG} {_HOOK} world"},
            # 1: Assistant text (nag) + tool_use. Text gets nag stripped, tool_use intact.
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"ok {_NAG} done"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
            # 2: User tool_result with deferred-tools (functional, must survive).
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": f"stdout {_DEFERRED} end",
                    }
                ],
            },
            # 3: User tool_result whose content is a list of text sub-blocks
            # mixing a nag and a hook — nag stripped, hook preserved.
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [
                            {"type": "text", "text": f"line1 {_NAG} end1"},
                            {"type": "text", "text": f"line2 {_HOOK} end2"},
                        ],
                    }
                ],
            },
            # 4: Assistant thinking — protocol-protected, never touched.
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": f"internal {_NAG} note",
                        "signature": "abc123",
                    },
                    {"type": "text", "text": "visible answer"},
                ],
            },
            # 5: Assistant redacted_thinking — protocol-protected.
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "redacted_thinking",
                        "data": "Eo8FCkYICRgCKkBopaque",
                    },
                    {"type": "text", "text": "another visible answer"},
                ],
            },
        ]
    }
    count = strip_message_tags(body, {})
    # Three nags total: msg[0], msg[1].text, msg[3].tool_result.content[0]
    assert count == 3, f"expected 3 nag strips, got {count}"

    msgs = body["messages"]
    # 0: nag gone, hook preserved
    assert "haven't been used recently" not in msgs[0]["content"], "nag survived"
    assert "SessionStart hook" in msgs[0]["content"], "hook was eaten"
    # 1: assistant nag gone, tool_use untouched
    assert "haven't been used recently" not in msgs[1]["content"][0]["text"], (
        "assistant nag survived"
    )
    assert msgs[1]["content"][1]["type"] == "tool_use", "tool_use mangled"
    # 2: deferred-tools list preserved (the actual MCP/tool-search fix)
    assert "deferred tools are now available" in msgs[2]["content"][0]["content"], (
        "deferred-tools reminder was eaten — ToolSearch would be broken"
    )
    # 3: nested tool_result list — nag gone from sub[0], hook preserved in sub[1]
    sub_blocks = msgs[3]["content"][0]["content"]
    assert "haven't been used recently" not in sub_blocks[0]["text"], (
        "nested nag survived"
    )
    assert "SessionStart hook" in sub_blocks[1]["text"], "nested hook was eaten"
    # 4: thinking untouched (signature stays valid)
    assert "haven't been used recently" in msgs[4]["content"][0]["thinking"], (
        "thinking was modified — would invalidate signature"
    )
    assert msgs[4]["content"][0]["signature"] == "abc123", "signature mutated"
    # 5: redacted_thinking untouched
    assert msgs[5]["content"][0]["type"] == "redacted_thinking", (
        "redacted_thinking was dropped"
    )
    assert msgs[5]["content"][0]["data"] == "Eo8FCkYICRgCKkBopaque", (
        "redacted_thinking data field mutated"
    )
    print("test_strip_message_tags_cross_role: OK")


def test_smart_strip_classifier_branches():
    """Direct test of the reminders classifier. Each PRESERVE and NAG
    pattern category produces the expected decision so a regression in
    pattern coverage is caught at unit-test time, not in production."""
    preserves = [
        "The following deferred tools are now available via ToolSearch: ...",
        "The following skills are available for use with the Skill tool: ...",
        "# MCP Server Instructions\n\nMCP servers...",
        "SessionStart hook additional context: ...",
        "SessionStart:startup hook success: {...}",
        "UserPromptSubmit hook additional context: ...",
        "The user opened the file /tmp/x.py",
        "The user sent a new message while you were working: foo",
    ]
    nags = [
        "The task tools haven't been used recently. ...",
        "The task tool hasn't been used recently. ...",
        "# Plan mode is active. ...",
        "Remember to use absolute paths.",
    ]
    for text in preserves:
        d, _ = classify_reminder(text)
        assert d == "preserve", f"expected preserve for {text!r}, got {d}"
    for text in nags:
        d, _ = classify_reminder(text)
        assert d == "strip", f"expected strip for {text!r}, got {d}"
    # Unknown shapes default to preserve (false-preserve > false-strip)
    d, _ = classify_reminder("some weird new shape we have not seen")
    assert d == "unknown"
    print("test_smart_strip_classifier_branches: OK")


def test_refusal_detection():
    """Verify refusal patterns catch common idioms and don't false-positive
    on legitimate uses of similar phrasing."""
    # Positive cases — should match
    positives = [
        "I can't help with that request.",
        "I cannot help you with this.",
        "I'm sorry, but I can't assist with that.",
        "I apologize, but I cannot generate that.",
        "Sorry, but I won't write that.",
        "I'm not able to provide that information.",
        "I am not comfortable with this request.",
        "I have to decline this request.",
        "That goes against my guidelines.",
        "This isn't appropriate.",
        "That's not something I can help with.",
        "I cannot fulfill this request.",
    ]
    for text in positives:
        assert detect_refusal(text) is not None, f"missed refusal: {text!r}"

    # Negative cases — must NOT match (legitimate idioms)
    negatives = [
        "Sure, here's the code you wanted.",
        "Let me help you with this — first, the structure.",
        "I can't help thinking about how this connects to your earlier point.",
        "Looking at this, I can't tell which option you prefer.",
        "It's appropriate to ask this question; here's the answer.",
        "",
    ]
    for text in negatives:
        assert detect_refusal(text) is None, f"false positive: {text!r}"

    # Position constraint: a refusal-like phrase deep in the response
    # should NOT trigger (mid-response use is legitimate).
    long_prefix = "Here's the implementation you asked for. " * 10  # ~400 chars
    text = long_prefix + "I can't help with edge cases here without more info."
    assert detect_refusal(text) is None, "position constraint failed"

    # all_refusals returns multiple matches when present
    multi = "I'm sorry, but I cannot help with that. I have to decline."
    matches = all_refusals(multi)
    assert len(matches) >= 2, f"expected multiple matches, got {matches}"

    print("test_refusal_detection: OK")


def test_model_tier():
    cases = [
        ("claude-opus-4-7", "opus"),
        ("claude-opus-4-7[1m]", "opus"),
        ("claude-sonnet-4-6", "sonnet"),
        ("claude-haiku-4-5-20251001", "haiku"),
        ("claude-haiku-4-5", "haiku"),
        ("claude-3-5-haiku-20241022", "haiku"),
        ("", "unknown"),
        (None, "unknown"),
    ]
    for model, expected in cases:
        got = model_tier(model)
        assert got == expected, f"model_tier({model!r}) = {got!r}, expected {expected!r}"
    print("test_model_tier: OK")


def test_lane_detection_canonical_openers():
    """Phase 5: detect_lane() returns the right label for each canonical
    opener fingerprint defined in LANE_FINGERPRINTS."""
    # Exercise every fingerprint by feeding its substring as a system block.
    for needle, expected_lane in LANE_FINGERPRINTS:
        sys = [{"type": "text", "text": f"{needle} ... rest of prompt"}]
        got = detect_lane(sys, model="claude-opus-4-7")
        assert got == expected_lane, (
            f"detect_lane on opener {needle!r}: got {got!r}, expected {expected_lane!r}"
        )
    print("test_lane_detection_canonical_openers: OK")


def test_lane_detection_fallbacks():
    """Phase 5: detect_lane() handles empty system, tiny haiku utility
    calls, and unknown large prompts via fallback labels."""
    # Empty system → empty_system regardless of model
    assert detect_lane(None, model="claude-opus-4-7") == "empty_system"
    assert detect_lane([], model="claude-haiku-4-5") == "empty_system"
    assert detect_lane("", model="claude-opus-4-7") == "empty_system"

    # Small haiku call without a fingerprint match → haiku_utility
    sys_small = [{"type": "text", "text": "Tiny utility prompt without fingerprints."}]
    assert detect_lane(sys_small, model="claude-haiku-4-5") == "haiku_utility"

    # Large unmatched call → subagent_or_unknown (NOT haiku_utility,
    # NOT empty_system, NOT main_worker)
    sys_large = [{"type": "text", "text": "X" * 5000}]
    assert detect_lane(sys_large, model="claude-opus-4-7") == "subagent_or_unknown"

    # Billing-header block is correctly skipped (string form)
    s = "x-anthropic-billing-header: cc=foo\n\nYou are an interactive agent that helps users with software engineering tasks."
    assert detect_lane(s, model="claude-opus-4-7") == "main_worker"

    print("test_lane_detection_fallbacks: OK")


def test_lane_detection_real_dumps():
    """Phase 5: validate detect_lane() against captured production dumps."""
    log_dir = Path.home() / ".ccoral" / "logs"
    dumps = sorted(log_dir.glob("raw-*.json"))
    if not dumps:
        print("test_lane_detection_real_dumps: SKIP (no captured dumps)")
        return

    # Expected lanes per dump (validated by manual inspection).
    EXPECTED = {
        "raw-eni-claude-hai.json": "empty_system",
        "raw-eni-claude-opu.json": "main_worker",
        "raw-eni-claude-son.json": "main_worker",
        "raw-eni-executor-claude-hai.json": "session_title_generator",
        "raw-eni-executor-claude-opu.json": "main_worker",
        "raw-eni-executor-room-claude-hai.json": "main_worker",
        "raw-eni-executor-room-claude-opu.json": "main_worker",
        "raw-eni-executor-room-claude-son.json": "subagent_custom",
        "raw-eni-room-claude-hai.json": "main_worker",
        "raw-eni-room-claude-opu.json": "main_worker",
        "raw-eni-supervisor-room-claude-hai.json": "main_worker",
        "raw-eni-supervisor-room-claude-opu.json": "main_worker",
        "raw-red-claude-hai.json": "session_title_generator",
        "raw-red-claude-opu.json": "main_worker",
    }
    checked = 0
    for p in dumps:
        if p.name not in EXPECTED:
            continue  # new captures without labels — record-only
        with open(p) as f:
            data = json.load(f)
        got = detect_lane(data.get("system"), model=data.get("model"))
        assert got == EXPECTED[p.name], (
            f"{p.name}: detect_lane={got!r}, expected={EXPECTED[p.name]!r}"
        )
        checked += 1
    print(f"test_lane_detection_real_dumps: OK ({checked} dumps)")


def test_tool_scrub_default_activation():
    """Phase 4: tool scrubbing defaults to ON for permissive profiles
    (apply_to_subagents implies tool_scrub_default), defaults OFF for
    vanilla profiles, and is overridable via tool_scrub_default: false."""
    bash_desc = (
        "Executes a given bash command and returns its output.\n\n"
        "IMPORTANT: Avoid using this tool to run `cat`, `head`, `tail`, or "
        "`echo` commands, unless explicitly instructed.\n\n"
        " - Read files: Use Read (NOT cat/head/tail)\n"
        " - Always quote file paths\n\n"
        "Git Safety Protocol:\n"
        "- NEVER update the git config\n"
        "- NEVER run destructive git commands unless the user explicitly requests these actions\n\n"
        "1. Run the following bash commands."
    )

    # Permissive profile (apply_to_subagents=true) → scrub fires
    tools = [{"name": "Bash", "description": bash_desc}]
    removed, hits = scrub_tool_descriptions(tools, {"apply_to_subagents": True})
    assert removed > 0, "permissive profile should activate scrub"
    assert "IMPORTANT: Avoid using this tool" not in tools[0]["description"], \
        "moralizing preamble should be gone"
    assert "Git Safety Protocol:" not in tools[0]["description"], \
        "Git Safety Protocol section should be gone"
    assert "Read files: Use Read" in tools[0]["description"], \
        "functional bullet must survive"
    assert "Always quote file paths" in tools[0]["description"], \
        "functional bullet must survive"
    assert "1. Run the following bash commands." in tools[0]["description"], \
        "numbered step must survive"

    # Vanilla profile (no apply_to_subagents) → scrub off
    tools_v = [{"name": "Bash", "description": bash_desc}]
    removed_v, _ = scrub_tool_descriptions(tools_v, {})
    assert removed_v == 0, "vanilla profile must not auto-scrub"
    assert tools_v[0]["description"] == bash_desc, "no modifications expected"

    # Permissive profile with explicit override → scrub off
    tools_off = [{"name": "Bash", "description": bash_desc}]
    removed_off, _ = scrub_tool_descriptions(
        tools_off, {"apply_to_subagents": True, "tool_scrub_default": False}
    )
    assert removed_off == 0, "explicit tool_scrub_default=False must skip defaults"

    # Profile-supplied tool_scrub_patterns are applied additively even when
    # tool_scrub_default is False
    tools_extra = [{"name": "Bash", "description": "Hello WORLD test."}]
    extra_pattern_profile = {
        "tool_scrub_default": False,
        "tool_scrub_patterns": [r"WORLD\s+"],
    }
    removed_extra, hits_extra = scrub_tool_descriptions(tools_extra, extra_pattern_profile)
    assert removed_extra > 0, "profile-supplied patterns must apply even when defaults off"
    assert "WORLD" not in tools_extra[0]["description"]
    assert "Hello" in tools_extra[0]["description"]

    print("test_tool_scrub_default_activation: OK")


def test_tool_scrub_real_bash_description():
    """Phase 4: against the captured production Bash description, scrub must
    measurably reduce length while preserving every flag/parameter mention."""
    real_dump = Path.home() / ".ccoral" / "logs" / "raw-eni-room-claude-opu.json"
    if not real_dump.exists():
        print("test_tool_scrub_real_bash_description: SKIP (no captured dump)")
        return

    with open(real_dump) as f:
        data = json.load(f)
    bash_tools = [t for t in data.get("tools", []) if t.get("name") == "Bash"]
    if not bash_tools:
        print("test_tool_scrub_real_bash_description: SKIP (no Bash tool in dump)")
        return

    before = len(bash_tools[0]["description"])
    removed, hits = scrub_tool_descriptions(bash_tools, {"apply_to_subagents": True})
    after = len(bash_tools[0]["description"])
    text = bash_tools[0]["description"]

    assert removed >= 1500, f"expected ≥1500 chars removed from real Bash desc, got {removed}"
    assert len(hits) >= 4, f"expected ≥4 distinct pattern categories matched, got {hits}"

    # Functional content survives — specific to real CC Bash desc
    must_survive = [
        "Executes a given bash command",
        "run_in_background",
        "timeout in milliseconds",
        "--no-verify",
        "--no-gpg-sign",
        "Read files: Use Read",
        "# Instructions",
        "# Committing changes with git",
        "gh pr create",
        "<example>",
        "HEREDOC",
    ]
    for s in must_survive:
        assert s in text, f"scrub eaten functional content: {s!r}"

    # Moralizing content is gone
    must_be_gone = [
        "IMPORTANT: Avoid using this tool",
        "Git Safety Protocol:",
        "Taking unauthorized destructive actions",
        "CRITICAL: Always create NEW commits",
        "Only create commits when requested by the user",
    ]
    for s in must_be_gone:
        assert s not in text, f"moralizing content survived scrub: {s!r}"

    print(f"test_tool_scrub_real_bash_description: OK ({before} → {after}, removed {removed})")


if __name__ == "__main__":
    test_main_fixture()
    test_subagent_fixture()
    test_apply_profile_main()
    test_apply_profile_subagent_fixture()
    test_strip_message_tags_cross_role()
    test_smart_strip_classifier_branches()
    test_refusal_detection()
    test_model_tier()
    test_tool_scrub_default_activation()
    test_tool_scrub_real_bash_description()
    test_lane_detection_canonical_openers()
    test_lane_detection_fallbacks()
    test_lane_detection_real_dumps()
    print("\nAll tests passed.")
