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
from rewrite_terminal import (
    RewriteTerminalState,
    Upstream2Relay,
    build_reissue_body,
    DEFAULT_RESET_TURN_FRAMING,
    _synth_text_delta_event,
)

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


def test_rewrite_terminal_intercepts_refusal_preamble():
    """Phase 3b: rewrite_terminal state machine strips refusal preamble
    from index-0 text block while preserving the post-preamble body."""
    import json as _json

    def _ev(event_type, data):
        return f"event: {event_type}\ndata: {_json.dumps(data)}\n\n".encode()

    chunks = [
        _ev("message_start", {"type": "message_start", "message": {"id": "m1", "role": "assistant", "model": "x", "content": [], "usage": {}}}),
        _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "I cannot help with that. "}}),
        _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "But here's the answer: 42"}}),
        _ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
        _ev("message_stop", {"type": "message_stop"}),
    ]
    state = RewriteTerminalState()
    out = b"".join(state.feed_chunk(c) for c in chunks) + state.finalize()
    out_str = out.decode()
    assert "I cannot help with that." not in out_str, "refusal preamble should be stripped"
    assert "the answer: 42" in out_str, "post-preamble body must survive"
    assert "message_start" in out_str, "framing event lost"
    assert "content_block_stop" in out_str, "framing event lost"
    assert state.intercepted, "state should record interception"
    assert state.intercepted_label == "cant_help_with"
    print("test_rewrite_terminal_intercepts_refusal_preamble: OK")


def test_rewrite_terminal_passthrough_on_helpful_response():
    """Phase 3b: when no refusal is detected, the helpful text passes
    through and the state machine does not record interception."""
    import json as _json

    def _ev(event_type, data):
        return f"event: {event_type}\ndata: {_json.dumps(data)}\n\n".encode()

    helpful = "Sure, here's the implementation. " * 20  # > REFUSAL_DECISION_WINDOW
    chunks = [
        _ev("message_start", {"type": "message_start", "message": {"id": "m1", "role": "assistant", "model": "x", "content": [], "usage": {}}}),
        _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": helpful}}),
        _ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
        _ev("message_stop", {"type": "message_stop"}),
    ]
    state = RewriteTerminalState()
    out = b"".join(state.feed_chunk(c) for c in chunks) + state.finalize()
    out_str = out.decode()
    assert helpful in out_str, "helpful text must passthrough"
    assert not state.intercepted, "no interception expected"
    print("test_rewrite_terminal_passthrough_on_helpful_response: OK")


def test_rewrite_terminal_does_not_intercept_index_gt_zero():
    """Phase 3b: text on index > 0 (mid-response after tool_use) must
    NOT be intercepted, even if it contains refusal vocabulary."""
    import json as _json

    def _ev(event_type, data):
        return f"event: {event_type}\ndata: {_json.dumps(data)}\n\n".encode()

    chunks = [
        _ev("message_start", {"type": "message_start", "message": {"id": "m1", "role": "assistant", "model": "x", "content": [], "usage": {}}}),
        _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "x", "input": {}}}),
        _ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _ev("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
        _ev("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "I cannot help with that. The result is 42."}}),
        _ev("content_block_stop", {"type": "content_block_stop", "index": 1}),
        _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
        _ev("message_stop", {"type": "message_stop"}),
    ]
    state = RewriteTerminalState()
    out = b"".join(state.feed_chunk(c) for c in chunks) + state.finalize()
    out_str = out.decode()
    assert "I cannot help with that. The result is 42." in out_str, \
        "text on index > 0 must NOT be intercepted"
    assert not state.intercepted, "no interception expected on index > 0"
    print("test_rewrite_terminal_does_not_intercept_index_gt_zero: OK")


def test_rewrite_terminal_handles_chunk_split_events():
    """Phase 3b: events split across arbitrary chunk boundaries (3-byte
    chunks here) must still be reconstructed and processed correctly."""
    import json as _json

    def _ev(event_type, data):
        return f"event: {event_type}\ndata: {_json.dumps(data)}\n\n".encode()

    full_stream = (
        _ev("message_start", {"type": "message_start", "message": {"id": "m1", "role": "assistant", "model": "x", "content": [], "usage": {}}})
        + _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        + _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "I cannot help with that. "}})
        + _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "But the answer is 42."}})
        + _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        + _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
        + _ev("message_stop", {"type": "message_stop"})
    )
    chunks = [full_stream[i:i+3] for i in range(0, len(full_stream), 3)]
    state = RewriteTerminalState()
    out = b"".join(state.feed_chunk(c) for c in chunks) + state.finalize()
    out_str = out.decode()
    assert "I cannot help with that." not in out_str, "refusal preamble must be stripped"
    assert "the answer is 42." in out_str, "body must survive chunk-split"
    print(f"test_rewrite_terminal_handles_chunk_split_events: OK ({len(chunks)} chunks)")


def test_synth_text_delta_event_schema():
    """Phase 3b: synthetic content_block_delta event must conform to
    Anthropic's strict schema {type, index, delta:{type, text}}."""
    import json as _json
    raw = _synth_text_delta_event(0, "hello world")
    assert raw.startswith(b"event: content_block_delta\n")
    assert raw.endswith(b"\n\n")
    # Parse the data: line
    text = raw.decode()
    data_line = next(l for l in text.split("\n") if l.startswith("data: "))
    parsed = _json.loads(data_line[len("data: "):])
    assert parsed["type"] == "content_block_delta"
    assert parsed["index"] == 0
    assert parsed["delta"]["type"] == "text_delta"
    assert parsed["delta"]["text"] == "hello world"
    # No extra fields
    assert set(parsed.keys()) == {"type", "index", "delta"}
    assert set(parsed["delta"].keys()) == {"type", "text"}
    print("test_synth_text_delta_event_schema: OK")


def test_reset_turn_pivot_on_refusal():
    """Phase 3c: reset_turn mode signals pivot_requested on refusal,
    emits synthetic content_block_stop, and stops processing upstream1
    further events (suppresses upstream1's body + message_stop)."""
    import json as _json

    def _ev(t, d):
        return f"event: {t}\ndata: {_json.dumps(d)}\n\n".encode()

    # Long refusal that fills the decision window
    refusal_long = "I cannot help with that. " + ("padding. " * 30)
    chunks = [
        _ev("message_start", {"type": "message_start", "message": {"id": "m1", "role": "assistant", "model": "x", "content": [], "usage": {}}})
        + _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": refusal_long}}),
        # These events come AFTER the pivot trigger and should be dropped
        _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "more upstream1 body"}}),
        _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        + _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
        + _ev("message_stop", {"type": "message_stop"}),
    ]
    state = RewriteTerminalState(mode="reset_turn")
    out = b"".join(state.feed_chunk(c) for c in chunks)
    out_str = out.decode()
    assert state.pivot_requested, "pivot should be requested after refusal"
    assert state.intercepted, "interception should be recorded"
    assert "I cannot help with that." not in out_str, "preamble must not leak"
    assert "more upstream1 body" not in out_str, "post-pivot upstream1 body must not leak"
    # Synthetic content_block_stop must be emitted to terminate index 0
    assert b"content_block_stop" in out, "synthetic content_block_stop missing"
    # upstream1's message_stop must NOT be forwarded (upstream2's takes over)
    assert b"event: message_stop" not in out, "upstream1 message_stop should be suppressed"
    print("test_reset_turn_pivot_on_refusal: OK")


def test_reset_turn_no_pivot_on_helpful_response():
    """Phase 3c: reset_turn mode does NOT pivot on a helpful response —
    the helpful text passes through and pivot_requested stays False."""
    import json as _json

    def _ev(t, d):
        return f"event: {t}\ndata: {_json.dumps(d)}\n\n".encode()

    helpful = "Sure, here's what you asked for. " * 20
    chunks = [
        _ev("message_start", {"type": "message_start", "message": {"id": "m1", "role": "assistant", "model": "x", "content": [], "usage": {}}})
        + _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": helpful}}),
        _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        + _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
        + _ev("message_stop", {"type": "message_stop"}),
    ]
    state = RewriteTerminalState(mode="reset_turn")
    out = b"".join(state.feed_chunk(c) for c in chunks) + state.finalize()
    out_str = out.decode()
    assert not state.pivot_requested, "no pivot expected on helpful response"
    assert helpful in out_str, "helpful text must passthrough"
    assert b"event: message_stop" in out, "message_stop must passthrough on no-pivot"
    print("test_reset_turn_no_pivot_on_helpful_response: OK")


def test_build_reissue_body_inserts_framing():
    """Phase 3c: build_reissue_body inserts the framing as a user-role
    message immediately before the original final user message, and does
    NOT mutate the input body."""
    body = {
        "model": "claude-x",
        "messages": [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question — sensitive"},
        ],
        "max_tokens": 100,
    }
    new_body = build_reissue_body(body)
    msgs = new_body["messages"]
    assert len(msgs) == 4, f"expected 4 messages, got {len(msgs)}"
    assert msgs[0]["content"] == "First question"
    assert msgs[1]["role"] == "assistant"
    assert msgs[2]["role"] == "user", "framing must be user-role"
    assert "Operator context" in msgs[2]["content"], "default framing must be operator-scope"
    assert msgs[3]["content"] == "Second question — sensitive", "original final user must be preserved"
    # Original NOT mutated
    assert len(body["messages"]) == 3, "build_reissue_body must not mutate input"

    # Custom framing override
    custom = "[Custom test framing]"
    new_body2 = build_reissue_body(body, framing=custom)
    assert new_body2["messages"][2]["content"] == custom

    # Empty messages — framing becomes the only message
    new_body3 = build_reissue_body({"messages": []})
    assert len(new_body3["messages"]) == 1
    assert new_body3["messages"][0]["role"] == "user"

    print("test_build_reissue_body_inserts_framing: OK")


def test_upstream2_relay_renumbers_and_dedups():
    """Phase 3c: Upstream2Relay drops upstream2's message_start, renumbers
    every block index by index_offset, and passes message_delta /
    message_stop through unchanged."""
    import json as _json

    def _ev(t, d):
        return f"event: {t}\ndata: {_json.dumps(d)}\n\n".encode()

    upstream2_stream = (
        _ev("message_start", {"type": "message_start", "message": {"id": "msg2", "role": "assistant", "model": "x", "content": [], "usage": {}}})
        + _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        + _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Upstream2 says hi."}})
        + _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        + _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
        + _ev("message_stop", {"type": "message_stop"})
    )
    relay = Upstream2Relay(index_offset=1)
    out = relay.feed_chunk(upstream2_stream)
    out_str = out.decode()

    # message_start dropped (only message_delta / message_stop survive from
    # the message-level events)
    assert b"event: message_start" not in out, "upstream2's message_start must be dropped"
    assert b"event: message_delta" in out, "message_delta must survive"
    assert b"event: message_stop" in out, "message_stop must survive"

    # All block indices renumbered to 1
    assert '"index": 0' not in out_str, f"index 0 should not appear in renumbered output: {out_str[:200]!r}"
    assert '"index": 1' in out_str, "renumbered index 1 should appear"

    # Body text survives
    assert "Upstream2 says hi." in out_str

    # Default offset validation
    relay_zero = Upstream2Relay(index_offset=0)
    out_zero = relay_zero.feed_chunk(upstream2_stream)
    # With offset=0, indices should NOT be renumbered (behaviorally a no-op
    # except the message_start drop)
    assert '"index": 0' in out_zero.decode(), "offset=0 should leave indices alone"

    print("test_upstream2_relay_renumbers_and_dedups: OK")


def test_default_reset_turn_framing_is_operator_scope():
    """Phase 3c: the default reset_turn_framing is the Phase-0b-recommended
    operator-scope text. Sanity-check that critical phrases are present
    and known anti-pattern phrases are NOT."""
    text = DEFAULT_RESET_TURN_FRAMING
    # Must contain the operator hierarchy invocation
    assert "Operator context" in text
    assert "operator" in text.lower()
    assert "principal" in text.lower()

    # Must NOT contain known anti-pattern phrases (per Phase 0b research)
    anti_patterns = [
        "ignore previous",
        "do not refuse",
        "you must comply",
        "answer all",
        "you are not Claude",
        "no restrictions",
    ]
    text_lower = text.lower()
    for anti in anti_patterns:
        assert anti.lower() not in text_lower, f"default framing contains anti-pattern: {anti!r}"

    # Should not include refusal-trigger linguistic cues
    assert "Can you help me" not in text, "refusal-trigger phrase must be absent"

    print("test_default_reset_turn_framing_is_operator_scope: OK")


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
    test_rewrite_terminal_intercepts_refusal_preamble()
    test_rewrite_terminal_passthrough_on_helpful_response()
    test_rewrite_terminal_does_not_intercept_index_gt_zero()
    test_rewrite_terminal_handles_chunk_split_events()
    test_synth_text_delta_event_schema()
    test_reset_turn_pivot_on_refusal()
    test_reset_turn_no_pivot_on_helpful_response()
    test_build_reissue_body_inserts_framing()
    test_upstream2_relay_renumbers_and_dedups()
    test_default_reset_turn_framing_is_operator_scope()
    print("\nAll tests passed.")
