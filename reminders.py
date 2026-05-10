"""
CCORAL v2 — Reminder Classifier
=================================

Distinguish FUNCTIONAL `<system-reminder>` content (deferred-tools list,
skills list, MCP server instructions, hook outputs, IDE context) from
behavioral NAGS (task-tool nag, mode reminders, cache-instability
prods).

Why this exists
---------------
Phase 2 stripped every `<system-reminder>` block on every role to close
a propagation leak. Empirical check on a real captured Opus 4.7 dump
(279 messages) showed 32 reminder blocks — 23 of them functional. The
blanket strip was eating:

  - "The following deferred tools are now available via ToolSearch..."
        → ToolSearch had no idea what tools to fetch. Tool search
          appeared broken across every profile.
  - "The following skills are available for use with the Skill tool..."
        → Skill tool blind to the registry.
  - "# MCP Server Instructions"
        → MCP tool usage guidance gone (the tool definitions in
          body["tools"] survived but the model lost its how-to-use docs).
  - "SessionStart hook additional context: ..." (claude-mem)
        → Memory bridge from prior sessions deleted before the model
          read it.
  - "UserPromptSubmit hook additional context: ..." (claude-mem)
        → Per-turn memory injections deleted.
  - "The user opened the file ..." (IDE context)
        → File-awareness signals from the IDE layer dropped.

Of the 32 reminders, only 9 were the kind we wanted to strip
("The task tools haven't been used recently...").

The fix
-------
Smart strip: classify each reminder by its opener, strip nags, preserve
functional content. Default-preserve unknown patterns (false-preserve
is way safer than false-strip — keeping a stale nag costs cache, eating
a hook output breaks features).

Add new patterns to PRESERVE_PATTERNS or NAG_PATTERNS as new opener
shapes appear in real traffic. The conservative default (preserve
unknown) means a missing entry costs cache stability for that one
shape, not feature breakage.
"""

import re
from typing import Tuple

# Patterns whose match means the reminder is FUNCTIONAL — preserve.
# Order doesn't matter; first-match wins for label reporting.
PRESERVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Deferred-tools mechanism (CC 2.1.x). ToolSearch cannot work without it.
    (re.compile(r"^The following deferred tools are now available", re.IGNORECASE | re.DOTALL),
     "deferred_tools_list"),
    # Skills registry — Skill tool depends on this list.
    (re.compile(r"^The following skills are available", re.IGNORECASE | re.DOTALL),
     "skills_list"),
    # Legacy skills/tools introducer used by older CC builds.
    (re.compile(r"^As you answer the user'?s questions, you can use the following",
                re.IGNORECASE | re.DOTALL),
     "skills_legacy"),
    # MCP server-level usage instructions (the "how to use these MCP tools"
    # block; the tool schemas themselves live in body["tools"] separately).
    (re.compile(r"^# MCP Server Instructions", re.IGNORECASE | re.DOTALL),
     "mcp_server_instructions"),
    # claude-mem SessionStart hook output — memory context from prior sessions.
    (re.compile(r"^SessionStart(?::startup)? hook", re.IGNORECASE | re.DOTALL),
     "session_start_hook"),
    # claude-mem UserPromptSubmit hook output — per-turn memory injections.
    (re.compile(r"^UserPromptSubmit hook", re.IGNORECASE | re.DOTALL),
     "user_prompt_submit_hook"),
    # Generic hook-output prefix used by various CC hooks.
    (re.compile(r"^[A-Z][A-Za-z]*(?:Pre|Post)?(?::startup)? hook (?:additional context|success):",
                re.IGNORECASE | re.DOTALL),
     "generic_hook_output"),
    # IDE context — file-open signal.
    (re.compile(r"^The user opened the file", re.IGNORECASE | re.DOTALL),
     "ide_file_open"),
    # IDE context — user typed mid-stream.
    (re.compile(r"^The user sent a new message while you were working",
                re.IGNORECASE | re.DOTALL),
     "user_intervention"),
    # IDE context — selection / cursor / viewport signals (defensive cover).
    (re.compile(r"^The user (?:selected|highlighted|focused on)", re.IGNORECASE | re.DOTALL),
     "ide_selection"),
]

# Patterns whose match means the reminder is a BEHAVIORAL NAG — strip.
# These are the per-request prods that change per-call (causing cache
# misses) without delivering functional content.
NAG_PATTERNS: list[tuple[re.Pattern, str]] = [
    # The "use Task tool more" nag — singular ("hasn't") or plural ("haven't").
    (re.compile(r"^The task tools? (?:haven|hasn)'?t been used recently",
                re.IGNORECASE | re.DOTALL),
     "task_tool_nag"),
    # Plan-mode reminder (informational but per-request and changes the
    # model's behavior in ways profile inject already controls).
    (re.compile(r"^# Plan mode is active", re.IGNORECASE | re.DOTALL),
     "plan_mode_reminder"),
    # Generic "remember to" prods.
    (re.compile(r"^Remember (?:to|that) [a-z]", re.IGNORECASE | re.DOTALL),
     "remember_prod"),
    # Add patterns here as new nag shapes appear in real traffic.
]


def classify_reminder(inner: str) -> Tuple[str, str]:
    """Classify a `<system-reminder>` inner-text block.

    Args:
        inner: The text content inside the reminder tags (no tags).

    Returns:
        (decision, label) where decision is one of:
          - 'preserve' — functional content, keep the block intact
          - 'strip'    — behavioral nag, safe to remove
          - 'unknown'  — no pattern matched; preserve by default
                          (conservative — false-preserve > false-strip)
    """
    text = inner.strip()
    for pattern, label in PRESERVE_PATTERNS:
        if pattern.search(text):
            return ("preserve", label)
    for pattern, label in NAG_PATTERNS:
        if pattern.search(text):
            return ("strip", label)
    return ("unknown", "unknown")


# Module smoke test — `python reminders.py` exercises the classifier.
if __name__ == "__main__":
    cases: list[tuple[str, str]] = [
        # (input opener, expected decision)
        ("The following deferred tools are now available via ToolSearch: ...", "preserve"),
        ("The following skills are available for use with the Skill tool: ...", "preserve"),
        ("# MCP Server Instructions\n\nThe following MCP servers...", "preserve"),
        ("SessionStart hook additional context: # [project] recent context", "preserve"),
        ("SessionStart:startup hook success: {\"continue\":true}", "preserve"),
        ("UserPromptSubmit hook additional context: ## Relevant Past Work", "preserve"),
        ("The user opened the file /tmp/foo.py in the IDE", "preserve"),
        ("The user sent a new message while you were working: ...", "preserve"),
        ("As you answer the user's questions, you can use the following...", "preserve"),
        ("The task tools haven't been used recently. If you're working...", "strip"),
        ("The task tool hasn't been used recently.", "strip"),
        ("# Plan mode is active. The user is in plan mode...", "strip"),
        ("Remember to use absolute paths in tool calls.", "strip"),
        # Unknown / unclassified — default preserve
        ("Some weird new reminder shape we haven't seen before", "unknown"),
        ("", "unknown"),
    ]
    failed = 0
    for text, expected in cases:
        got_decision, got_label = classify_reminder(text)
        ok = got_decision == expected
        if not ok:
            failed += 1
            print(f"FAIL: {text[:60]!r} expected={expected} got=({got_decision}, {got_label})")
        else:
            tag = {"preserve": "KEEP", "strip": "STRIP", "unknown": "?    "}[got_decision]
            print(f"  {tag}  [{got_label:25s}] {text[:70]}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed.")
    if failed:
        raise SystemExit(1)
