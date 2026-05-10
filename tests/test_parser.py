#!/usr/bin/env python3
"""Smoke tests for the CC 2.1.138 parser refresh."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import parse_system_prompt, apply_profile, rebuild_system_prompt
from server import model_tier

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
    test_model_tier()
    print("\nAll tests passed.")
