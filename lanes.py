"""
CCORAL v2 — Lane Router
========================

Identify which Claude Code "lane" a request belongs to, by matching
fingerprints in the system prompt against a known catalog of CC's
internal call shapes.

Why this exists
---------------
CC 2.1.x dispatches model calls along multiple lanes from one process:
the main coding agent, isolated subagents, summarizers, compaction
generators, the security monitor (auto mode), the state classifier
(background mode), the auto-rule reviewer, dream-mode consolidators,
web-fetch summarizers, and 3-5-word agent-action summaries. Each lane
has its own system-prompt opener — short, distinctive, identical
across runs — and each calls for different proxy treatment.

The pre-Phase-5 dispatcher used only model-tier (haiku/sonnet/opus) +
system-prompt size as a proxy for lane. That works for the broad
"main convo vs. subagent" split but conflates everything else:
    - Compaction summaries (haiku, large system) → looked like a main convo
    - Custom subagents that exceed 22K → looked like a main convo
    - Agent-summary calls (haiku, tiny system) → looked like utility
    - Webfetch summaries → looked like a vanilla subagent

The fingerprint approach (this module) does positive lane ID. The
existing tier+size logic stays as a fallback for unknown lanes.

What this module ships
----------------------
Phase 5 = the **router only**. detect_lane() returns a label; the proxy
logs it and (if profile.lane_policy is set) consults the per-lane verb.
Verb implementations land in later commits (5b, 5c, ...).

References
----------
- Lane catalog: .plan/red-harness-offensive-review-2.1.138.md
- Validation: ~/.ccoral/logs/raw-*.json (13 captured dumps as of May 2026)
- Plan: .plan/permissive-core-remaining.md § Phase 5
"""

from typing import Iterable, Optional, Union

# Each entry: (substring_to_match, lane_label).
# Order matters: first-match wins. More specific fingerprints come first
# so they out-rank generic catch-alls.
#
# Substrings are the literal text of the lane's opener line, drawn from
# either captured production dumps (validated) or the offensive-review
# fingerprint catalog (high-confidence-per-doc, not yet seen in dumps).
LANE_FINGERPRINTS: list[tuple[str, str]] = [
    # --- Validated in captured dumps ---
    # Main coding agent (every full session). Validated in 11/13 dumps.
    ("You are an interactive agent that helps users with software engineering tasks",
     "main_worker"),
    # Custom subagent dispatched via Task tool with a custom <role>
    # persona. CC opens the persona block with `<role>\nYou are ...`.
    # Validated in raw-eni-executor-room-claude-son.json (33K-char system
    # carrying a custom GSD plan executor persona). The leading `<role>`
    # is at block-start (no preceding newline in the joined text).
    # Ranks ABOVE subagent_sdk_default — a custom-persona subagent
    # carries both the SDK identity AND the <role> overlay, and the
    # custom-persona label is the more useful classification (lane
    # policy can treat custom subagents differently from default ones).
    ("<role>\nYou are",
     "subagent_custom"),
    # Default SDK-CLI subagent identity (no custom <role> persona).
    # Validated in raw-eni-executor-room-claude-hai.json,
    # raw-eni-room-claude-hai.json, raw-eni-supervisor-room-claude-hai.json.
    # (Note: this can co-occur with main_worker fingerprint when SDK
    # dispatches a worker; the main_worker substring above wins.)
    ("You are a Claude agent, built on Anthropic's Claude Agent SDK",
     "subagent_sdk_default"),
    # Session-title generator (haiku, ~840-char system, fires on session
    # start to label the conversation in CC's UI). Validated in
    # raw-eni-executor-claude-hai.json, raw-red-claude-hai.json.
    ("Generate a concise, sentence-case title",
     "session_title_generator"),

    # --- High-confidence-per-doc (not in current dumps) ---
    # Security monitor (auto mode only — fires when auto-mode is enabled
    # to gate tool calls deterministically).
    ("You are a security monitor for autonomous AI coding agents",
     "security_monitor"),
    # State classifier (background mode — categorizes long-running session
    # state for the CC UI status panel).
    ("A user kicked off a Claude Code agent to do a coding task and walked away",
     "state_classifier"),
    # Summarizer (manual /compact or session-export trigger).
    ("Your task is to create a detailed summary of the conversation so far",
     "summarizer"),
    # Auto-compact summary (the in-conversation compaction summary; fires
    # when the model approaches context window limits).
    ("You have been working on the task described above but have not yet completed it",
     "compaction_summary"),
    # Dream-mode consolidator (memory-consolidation pass; fires after
    # session end if dream mode is configured).
    ("You are performing a dream",
     "dream_consolidator"),
    # WebFetch summarizer (the haiku-tier wrapper that summarizes a
    # fetched page before returning to the worker).
    ("Web page content:",
     "webfetch_summarizer"),
    # Agent summary (the 3-5-word summary that decorates Task subagent
    # results in the CC UI).
    ("Describe your most recent action in 3-5 words",
     "agent_summary"),
    # Auto-mode classifier rule reviewer (auto-mode meta — reviews the
    # rules the security_monitor uses).
    ("You are an expert reviewer of auto mode classifier rules",
     "auto_rule_reviewer"),
]

# How many leading characters of the joined system text to inspect.
# Identity openers reliably appear in the first ~300 chars after the
# billing header; the cap keeps the substring scan O(1) regardless of
# system-prompt length.
FINGERPRINT_WINDOW = 1000

# Generic empty-system case (CC sometimes issues a body with no system
# block — e.g. retry of a haiku probe).
EMPTY_SYSTEM_LANE = "empty_system"

# Catch-all when no fingerprint matches but size suggests a haiku utility.
# Threshold matches CC's own short utility calls (~840 chars).
HAIKU_UTILITY_MAX_CHARS = 2000

# Catch-all for unrecognized lanes — fallback to size-based dispatch.
UNKNOWN_LANE = "subagent_or_unknown"


def _flatten_system(system: Union[None, str, list]) -> str:
    """Concatenate a request body's `system` field into a single string.

    Skips the `x-anthropic-billing-header` prefix block (CC injects this
    as the first text block to identify itself to the API; it carries no
    semantic content for lane detection).
    """
    if system is None:
        return ""
    if isinstance(system, str):
        # String form sometimes still has the billing prefix glued in —
        # strip it conservatively if present (it ends at the first
        # `\n\n` or at the end of the line).
        if system.startswith("x-anthropic-billing-header"):
            split_at = system.find("\n\n")
            if split_at != -1:
                system = system[split_at + 2:]
        return system
    if not isinstance(system, list):
        return ""
    parts: list[str] = []
    for block in system:
        if not isinstance(block, dict):
            continue
        t = block.get("text", "")
        if not isinstance(t, str):
            continue
        if t.startswith("x-anthropic-billing-header"):
            continue
        parts.append(t)
        if sum(len(p) for p in parts) >= FINGERPRINT_WINDOW * 2:
            break  # short-circuit — we only need the first window
    return " ".join(parts)


def detect_lane(
    system: Union[None, str, list],
    *,
    model: Optional[str] = None,
) -> str:
    """Identify the CC lane this request belongs to.

    Args:
        system: The request body's `system` field (None, string, or list of
            content blocks).
        model: Optional model id. Used for the haiku-utility catch-all
            when no fingerprint matches.

    Returns:
        A lane label. One of: every label in LANE_FINGERPRINTS,
        EMPTY_SYSTEM_LANE, "haiku_utility", or UNKNOWN_LANE.

    The match runs against the first FINGERPRINT_WINDOW chars of the
    joined system text (billing header excluded). First-match wins per
    LANE_FINGERPRINTS order.
    """
    text = _flatten_system(system)
    if not text:
        return EMPTY_SYSTEM_LANE

    window = text[:FINGERPRINT_WINDOW]
    for needle, label in LANE_FINGERPRINTS:
        if needle in window:
            return label

    # Haiku catch-all for tiny utility calls (no main_worker fingerprint
    # because they don't carry the agent persona). Covers the empty-prompt
    # `raw-eni-claude-hai.json` and the 840-char title-generator stubs
    # when not matched by their specific fingerprint.
    if model and "haiku" in model.lower() and len(text) < HAIKU_UTILITY_MAX_CHARS:
        return "haiku_utility"

    return UNKNOWN_LANE


# -----------------------------------------------------------------------------
# Module smoke test — `python lanes.py`
# Validates against the full set of captured dumps (~/.ccoral/logs/) plus
# synthetic fixtures for lanes we haven't yet seen in the wild.
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    from pathlib import Path

    # 1. Synthetic fixtures — cover every lane label we ship.
    synth_cases: list[tuple[str, Optional[str], list, str]] = [
        # (description, model, system_blocks, expected_lane)
        ("main_worker — full session opener",
         "claude-opus-4-7",
         [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
          {"type": "text", "text": "You are an interactive agent that helps users with software engineering tasks. Use the instructions below..."}],
         "main_worker"),
        ("subagent_sdk_default — SDK-dispatched haiku subagent",
         "claude-haiku-4-5",
         [{"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK. Then more text..."}],
         "subagent_sdk_default"),
        ("subagent_custom — Task tool with custom <role> persona",
         "claude-sonnet-4-6",
         [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
          {"type": "text", "text": "<role>\nYou are a GSD plan executor. You execute PLAN.md files atomically..."}],
         "subagent_custom"),
        ("session_title_generator",
         "claude-haiku-4-5",
         [{"type": "text", "text": "You are Claude Code. Generate a concise, sentence-case title (3-7 words)..."}],
         "session_title_generator"),
        ("security_monitor",
         "claude-haiku-4-5",
         [{"type": "text", "text": "You are a security monitor for autonomous AI coding agents..."}],
         "security_monitor"),
        ("state_classifier",
         "claude-haiku-4-5",
         [{"type": "text", "text": "A user kicked off a Claude Code agent to do a coding task and walked away..."}],
         "state_classifier"),
        ("summarizer",
         "claude-sonnet-4-6",
         [{"type": "text", "text": "Your task is to create a detailed summary of the conversation so far..."}],
         "summarizer"),
        ("compaction_summary",
         "claude-sonnet-4-6",
         [{"type": "text", "text": "You have been working on the task described above but have not yet completed it..."}],
         "compaction_summary"),
        ("dream_consolidator",
         "claude-haiku-4-5",
         [{"type": "text", "text": "You are performing a dream — consolidate memories from..."}],
         "dream_consolidator"),
        ("webfetch_summarizer",
         "claude-haiku-4-5",
         [{"type": "text", "text": "Web page content: <html>..."}],
         "webfetch_summarizer"),
        ("agent_summary",
         "claude-haiku-4-5",
         [{"type": "text", "text": "Describe your most recent action in 3-5 words"}],
         "agent_summary"),
        ("auto_rule_reviewer",
         "claude-haiku-4-5",
         [{"type": "text", "text": "You are an expert reviewer of auto mode classifier rules..."}],
         "auto_rule_reviewer"),
        ("empty_system — None",
         "claude-haiku-4-5",
         None,
         "empty_system"),
        ("empty_system — empty list",
         "claude-haiku-4-5",
         [],
         "empty_system"),
        ("haiku_utility — small unmatched haiku call",
         "claude-haiku-4-5",
         [{"type": "text", "text": "Some short utility prompt."}],
         "haiku_utility"),
        ("subagent_or_unknown — large unmatched call",
         "claude-opus-4-7",
         [{"type": "text", "text": "X" * 5000}],
         "subagent_or_unknown"),
        ("billing-header is skipped (string form)",
         "claude-opus-4-7",
         "x-anthropic-billing-header: cc=foo\n\nYou are an interactive agent that helps users with software engineering tasks.",
         "main_worker"),
    ]

    failures = 0
    for desc, model, sys_in, expected in synth_cases:
        got = detect_lane(sys_in, model=model)
        ok = got == expected
        marker = "OK   " if ok else "FAIL "
        print(f"  {marker}  [{expected:25s}] {desc[:60]}")
        if not ok:
            print(f"          got: {got}")
            failures += 1

    # 2. Real captured dumps — RECORD-ONLY validation. The proxy
    # overwrites these files on live traffic (raw-{profile}-{model}.json
    # is keyed by profile+model, not by call), so a hard-coded EXPECTED
    # dict goes stale every time a session runs. We log the detected
    # lane for every dump for inspection, but the synthetic-fixture
    # cases above are what assert correctness — they cover every
    # fingerprint in the catalog.
    #
    # Additionally assert that NO captured dump produces an obviously-
    # wrong classification (a non-empty system prompt should not
    # classify as `empty_system`, and an empty body should not classify
    # as anything else).
    print()
    print("Captured dumps (record-only — synthetic cases above are the assertion):")
    log_dir = Path.home() / ".ccoral" / "logs"
    dumps = sorted(log_dir.glob("raw-*.json"))
    if not dumps:
        print("  (no dumps available — skipping)")
    else:
        for p in dumps:
            try:
                with open(p) as f:
                    data = json.load(f)
            except Exception as e:
                print(f"  ?    {p.name}: load error {e}")
                continue
            sys = data.get("system")
            model = data.get("model")
            got = detect_lane(sys, model=model)
            # Compute the actual non-billing length to sanity-check the
            # empty_system classification.
            joined = _flatten_system(sys)
            print(f"  -    {p.name:50s} {got} (len={len(joined)})")
            # Sanity rule: only empty input should classify as empty_system.
            if got == "empty_system" and len(joined) > 0:
                print(f"  FAIL {p.name}: classified empty_system but len={len(joined)}")
                failures += 1
            if got != "empty_system" and len(joined) == 0:
                print(f"  FAIL {p.name}: classified {got} but len=0")
                failures += 1

    print()
    if failures:
        print(f"{failures} failure(s).")
        raise SystemExit(1)
    print(f"{len(synth_cases) + len(dumps)} cases passed.")
