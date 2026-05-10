#!/usr/bin/env python3
"""Smoke tests for the CC 2.1.138 parser refresh."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import parse_system_prompt, apply_profile, rebuild_system_prompt
from server import model_tier, strip_message_tags
from refusal import detect_refusal, all_refusals, REFUSAL_PATTERNS

FIXTURES = Path(__file__).parent / "fixtures"


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
    """Verify <system-reminder> stripping walks both user and assistant text,
    skips tool_use blocks, and leaves thinking-block signatures intact."""
    body = {
        "messages": [
            # User text (string content) — must strip
            {"role": "user", "content": "hello <system-reminder>nag</system-reminder> world"},
            # Assistant text block — must strip (this is the Phase 2 fix)
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok <system-reminder>echoed</system-reminder> done"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
            # User tool_result — must strip
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "stdout <system-reminder>nag2</system-reminder> end",
                    }
                ],
            },
            # Assistant thinking — must NOT strip. Anthropic protocol
            # requires thinking blocks be replayed unchanged during tool-use
            # multi-turn flows; modifying invalidates the signature, dropping
            # breaks tool_use→tool_result continuity. Reminder text inside
            # thinking is a real leak path but the safe fix point is the
            # conversation summarizer (Phase 5), not the request-side strip.
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "internal <system-reminder>preserved</system-reminder> note",
                        "signature": "abc123",
                    },
                    {"type": "text", "text": "visible answer"},
                ],
            },
            # Assistant redacted_thinking — must NOT strip. Encrypted content
            # in `data` field, same protocol constraint. Per Anthropic docs:
            # "Filtering on block.type == 'thinking' alone silently drops
            # redacted_thinking blocks and breaks the multi-turn protocol."
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
    assert count == 3, f"expected 3 strips (user-string, assistant-text, tool_result), got {count}"

    msgs = body["messages"]
    # User string
    assert "<system-reminder>" not in msgs[0]["content"], "user string not stripped"
    # Assistant text block
    assert "<system-reminder>" not in msgs[1]["content"][0]["text"], (
        "assistant text not stripped (Phase 2 leak)"
    )
    # tool_use block untouched
    assert msgs[1]["content"][1]["type"] == "tool_use", "tool_use mangled"
    # tool_result
    assert "<system-reminder>" not in msgs[2]["content"][0]["content"], (
        "tool_result not stripped"
    )
    # Thinking preserved (protocol requires unchanged replay)
    assert "<system-reminder>" in msgs[3]["content"][0]["thinking"], (
        "thinking block was modified — would invalidate signature, break replay"
    )
    assert msgs[3]["content"][0]["signature"] == "abc123", "signature mutated"
    # redacted_thinking preserved (same protocol constraint)
    assert msgs[4]["content"][0]["type"] == "redacted_thinking", (
        "redacted_thinking block was dropped — would break multi-turn protocol"
    )
    assert msgs[4]["content"][0]["data"] == "Eo8FCkYICRgCKkBopaque", (
        "redacted_thinking data field was modified"
    )
    print("test_strip_message_tags_cross_role: OK")


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


if __name__ == "__main__":
    test_main_fixture()
    test_subagent_fixture()
    test_apply_profile_main()
    test_apply_profile_subagent_fixture()
    test_strip_message_tags_cross_role()
    test_refusal_detection()
    test_model_tier()
    print("\nAll tests passed.")
