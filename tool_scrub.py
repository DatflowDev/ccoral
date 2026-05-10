"""
CCORAL v2 — Tool Description Scrubber
======================================

Strip behavioral fearmongering from tool descriptions while preserving
functional documentation (what params do, which flags exist, side effects).

Why this exists
---------------
Audit of a real captured Opus 4.7 dump (.ccoral/logs/raw-eni-room-claude-opu.json,
20 tools) found heavy refusal-priming language in stock CC tool descriptions:

  Tool             Length    NEVER hits   Categories
  Bash             10663     12           NEVER, do not, destructive, ALWAYS, skip hooks, CRITICAL
  Agent            17542      1           do not, CRITICAL, NEVER
  ScheduleWakeup    2285      0           don't, ALWAYS
  Edit              1094      2           NEVER, ALWAYS
  Skill             1315      2           do not, NEVER
  Write              618      1           only use, NEVER
  AskUserQuestion   1763      1           do not, ALWAYS
  Read              1782      1           ALWAYS, do not

These descriptions are loaded into context every turn. They prime the model
toward refusal and consume tokens for content that does not change tool
behavior — only adds defensive moralizing.

`strip_tool_descriptions: true` (existing) replaces the whole description
with the tool name. That's too aggressive: the model loses flag docs,
parameter semantics, side-effect notes — actually breaking tool use.

Smart scrub: opener-anchored regex that removes the moralizing paragraphs
and "NEVER ... unless explicitly ..." sentences, while preserving:
  - Flag/parameter names (`--no-verify`, `-i`, etc.)
  - `# Instructions` and `# Committing changes` section structure
  - Bullet-list functional guidance ("- Always quote file paths...")
  - <example> tags and HEREDOC patterns
  - Tool-name-and-syntax content

The pattern set is tuned empirically against the captured Bash description
(see `apply_scrub_patterns()` test below for the full before/after diff).

References
----------
- Captured tool descriptions: ~/.ccoral/logs/raw-eni-room-claude-opu.json
- Audit table: .plan/permissive-core-remaining.md § Phase 0 § "Tool description audit"
- Why we do this at all: INJECT-FRAMING.md § 3 (refusal-trigger mechanism)
- Default activation rule: coupled to apply_to_subagents per
  .plan/permissive-core-remaining.md § "Decisions" — one mental model
  for permissive profiles.
"""

import re
from typing import Iterable

# Each entry is (compiled_regex, label).
# Patterns must be sentence-anchored or paragraph-anchored. Greedy regex on
# this content would cross sentences and eat functional bullets — don't do that.
DEFAULT_SCRUB_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 1. "IMPORTANT: Avoid using this tool to run `cat`, `head`..." —
    #    paragraph-leading nag that prefixes the actual `# Instructions` block.
    #    Includes the optional "Instead, use the appropriate..." follow-on.
    #    The follow-on is bounded to the same line (no \n) so it cannot
    #    bleed into the functional bullet list that follows the colon.
    (re.compile(
        r"IMPORTANT:\s*Avoid using this tool to run[^.]+?\.\s*"
        r"(?:Instead,\s*use[^.\n]+?[.:]\s*)?",
        re.IGNORECASE,
    ), "important_avoid_preamble"),

    # 2. "While the X tool can do similar things, it's better to use..." —
    #    closing nag at the end of the IMPORTANT paragraph.
    (re.compile(
        r"While the [A-Z][A-Za-z]* tool can do similar things,[^.]+?\.\s*",
    ), "tool_alternative_lecture"),

    # 3. "Git Safety Protocol:" heading + its bullet list (until blank line).
    #    The bullets here are uniformly NEVER/CRITICAL prohibitions; remove
    #    the whole section. The functional `- For git commands:` bullets
    #    live in the EARLIER `# Instructions` section, which is preserved.
    (re.compile(
        r"Git Safety Protocol:\s*"
        r"(?:\n[ \t]*-[ \t]+[^\n]*)+"
        r"(?:\n\n|\Z)",
        re.MULTILINE,
    ), "git_safety_section"),

    # 4. "CRITICAL: ..." sentences. Lecture-style ALL-CAPS preamble + body.
    #    Constrained to single sentence (stops at first `.`).
    (re.compile(
        r"(?:^|(?<=[\s\-]))CRITICAL:\s*[^.\n]+?\.\s*",
        re.MULTILINE,
    ), "critical_lecture"),

    # 5. "Taking unauthorized destructive actions is unhelpful..." —
    #    explicit moralizing about consequences. Single sentence.
    (re.compile(
        r"Taking (?:unauthorized )?destructive actions[^.\n]+?\.\s*",
    ), "destructive_warning_moral"),

    # 6. "NEVER ... unless explicitly ..." / "NEVER ... unless the user
    #    has explicitly asked for it." — the canonical refusal-priming
    #    template. Sentence-bounded; preserves flag examples inside the
    #    sentence because they appear before the `unless` clause.
    (re.compile(
        r"(?:^|(?<=[\s\-]))NEVER\s+[^.\n]+?unless\s+(?:explicitly|the user[^.\n]+?)\.\s*",
        re.MULTILINE,
    ), "never_unless_explicit"),

    # 7. "Only create commits when requested by the user. If unclear, ask
    #    first." — the redundant lead nag in the # Committing section.
    #    Two-sentence variant captured.
    (re.compile(
        r"Only create commits when requested by the user\.\s*"
        r"(?:If unclear,\s*ask first\.\s*)?",
    ), "only_create_commits_lead"),

    # 8. "IMPORTANT: Never use ..." sentences (distinct from #1: this is a
    #    direct prohibition without the "Avoid using this tool" stem).
    (re.compile(
        r"IMPORTANT:\s*Never\s+[^.\n]+?\.\s*",
        re.IGNORECASE,
    ), "important_never_prohibition"),
]


def scrub_tool_descriptions(
    tools: list,
    profile: dict,
    *,
    log=None,
) -> tuple[int, dict[str, int]]:
    """Strip behavioral nags from tool descriptions in-place.

    Args:
        tools: The body["tools"] list (modified in place).
        profile: Profile dict. Honored fields:
          - tool_scrub_patterns (list of regex strings, optional):
            additional patterns to apply on top of the defaults.
          - tool_scrub_default (bool, default True if apply_to_subagents
            is True, else False): whether to apply DEFAULT_SCRUB_PATTERNS.
            Set explicitly to False to skip defaults entirely.
        log: Optional logger for per-pattern hit counts.

    Returns:
        (total_chars_removed, per_pattern_hits) — useful for log lines and
        tests. Per-pattern hits is a dict mapping label → number of tool
        descriptions that pattern matched against (NOT total match count;
        a tool with 3 NEVER sentences counts as 1).
    """
    if not tools or not isinstance(tools, list):
        return (0, {})

    # Resolve activation rule. Default-on for permissive profiles
    # (apply_to_subagents implies tool_scrub_default unless overridden).
    explicit_default = profile.get("tool_scrub_default")
    if explicit_default is None:
        # Coupled rule: default-on iff profile is full-permissive
        use_defaults = bool(profile.get("apply_to_subagents", False))
    else:
        use_defaults = bool(explicit_default)

    # Compile any profile-supplied extra patterns.
    extra_patterns: list[tuple[re.Pattern, str]] = []
    for i, pat in enumerate(profile.get("tool_scrub_patterns") or []):
        try:
            extra_patterns.append((re.compile(pat), f"profile_pattern_{i}"))
        except re.error as e:
            if log:
                log.warning(f"Invalid tool_scrub_pattern[{i}] {pat!r}: {e}")

    if not use_defaults and not extra_patterns:
        return (0, {})

    active_patterns: list[tuple[re.Pattern, str]] = []
    if use_defaults:
        active_patterns.extend(DEFAULT_SCRUB_PATTERNS)
    active_patterns.extend(extra_patterns)

    total_removed = 0
    per_pattern_hits: dict[str, int] = {}

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # Apply to top-level description and (if present) custom.description.
        for desc_holder, desc_key in _description_slots(tool):
            desc = desc_holder.get(desc_key)
            if not isinstance(desc, str) or not desc:
                continue
            new_desc, removed, tool_hits = _apply_patterns(desc, active_patterns)
            if removed:
                desc_holder[desc_key] = new_desc
                total_removed += removed
                for label in tool_hits:
                    per_pattern_hits[label] = per_pattern_hits.get(label, 0) + 1

    if log and per_pattern_hits:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(per_pattern_hits.items()))
        log.info(
            f"Tool scrub: removed {total_removed} chars across "
            f"{len(per_pattern_hits)} pattern(s) — {summary}"
        )

    return (total_removed, per_pattern_hits)


def _description_slots(tool: dict) -> Iterable[tuple[dict, str]]:
    """Yield (container_dict, key) pairs for every description field a tool
    may carry. Mirrors apply_replacements_to_tools()'s field discovery."""
    if "description" in tool:
        yield (tool, "description")
    custom = tool.get("custom")
    if isinstance(custom, dict) and "description" in custom:
        yield (custom, "description")


def _apply_patterns(
    text: str,
    patterns: list[tuple[re.Pattern, str]],
) -> tuple[str, int, set[str]]:
    """Apply each pattern in order. Returns (new_text, chars_removed, hit_labels)."""
    original_len = len(text)
    hit_labels: set[str] = set()
    for pattern, label in patterns:
        new_text, n = pattern.subn("", text)
        if n > 0:
            hit_labels.add(label)
            text = new_text
    # Collapse runs of 3+ blank lines that scrubbing may have created.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return (text, original_len - len(text), hit_labels)


# -----------------------------------------------------------------------------
# Module smoke test — `python tool_scrub.py`
# Tests against synthesized fragments; the integration test against a real
# captured Bash description lives in tests/test_parser.py.
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    BASH_FIXTURE = """\
Executes a given bash command and returns its output.

The working directory persists between commands, but shell state does not.

IMPORTANT: Avoid using this tool to run `cat`, `head`, `tail`, `sed`, `awk`, or `echo` commands, unless explicitly instructed or after you have verified that a dedicated tool cannot accomplish your task. Instead, use the appropriate dedicated tool as this will provide a much better experience for the user:

 - Read files: Use Read (NOT cat/head/tail)
 - Edit files: Use Edit (NOT sed/awk)
While the Bash tool can do similar things, it's better to use the built-in tools as they provide a better user experience and make it easier to review tool calls and give permission.

# Instructions
 - Always quote file paths that contain spaces with double quotes
 - You may specify an optional timeout in milliseconds (up to 600000ms / 10 minutes).
 - For git commands:
  - Prefer to create a new commit rather than amending an existing commit.
  - Before running destructive operations (e.g., git reset --hard, git push --force, git checkout --), consider whether there is a safer alternative.
  - Never skip hooks (--no-verify) or bypass signing (--no-gpg-sign, -c commit.gpgsign=false) unless the user has explicitly asked for it. If a hook fails, investigate and fix the underlying issue.

# Committing changes with git

Only create commits when requested by the user. If unclear, ask first. When the user asks you to create a new git commit, follow these steps carefully:

Git Safety Protocol:
- NEVER update the git config
- NEVER run destructive git commands (push --force, reset --hard, checkout ., restore ., clean -f, branch -D) unless the user explicitly requests these actions. Taking unauthorized destructive actions is unhelpful and can result in lost work, so it's best to ONLY run these commands when given direct instructions
- NEVER skip hooks (--no-verify, --no-gpg-sign, etc) unless the user explicitly requests it
- CRITICAL: Always create NEW commits rather than amending, unless the user explicitly requests a git amend.

1. Run the following bash commands in parallel.

Important notes:
- IMPORTANT: Never use git commands with the -i flag since they require interactive input which is not supported.
"""

    # Build a fake tools list and run scrub.
    tools = [{
        "name": "Bash",
        "description": BASH_FIXTURE,
        "input_schema": {"type": "object"},
    }]
    profile = {"apply_to_subagents": True}  # triggers tool_scrub_default

    before_len = len(tools[0]["description"])
    removed, hits = scrub_tool_descriptions(tools, profile)
    after_len = len(tools[0]["description"])
    after_text = tools[0]["description"]

    # Assertions: scrubber must (a) remove some content, (b) preserve
    # functional content. We assert each.
    must_survive = [
        "Executes a given bash command",
        "Read files: Use Read",  # functional bullet
        "Always quote file paths",  # functional bullet
        "--no-verify",  # flag name (inside a bullet) must survive
        "--no-gpg-sign",  # flag name must survive
        "git push --force",  # flag example must survive
        # Note: `-i flag` text appears ONLY inside a scrubbed
        # "IMPORTANT: Never use" sentence, so it is acceptable collateral.
        # We document the loss but don't fail on it.
        "# Instructions",  # section header
        "# Committing changes with git",  # section header
        "1. Run the following bash commands in parallel.",  # numbered step
    ]
    must_be_gone = [
        "IMPORTANT: Avoid using this tool to run",
        "While the Bash tool can do similar things",
        "Git Safety Protocol:",
        "Taking unauthorized destructive actions is unhelpful",
        "CRITICAL: Always create NEW commits",
        "Only create commits when requested by the user",
        "IMPORTANT: Never use git commands with the -i flag",
    ]

    failures: list[str] = []
    for s in must_survive:
        if s not in after_text:
            failures.append(f"  LOST functional content: {s!r}")
    for s in must_be_gone:
        if s in after_text:
            failures.append(f"  KEPT moralizing content: {s!r}")

    print(f"Bash description: {before_len} → {after_len} chars "
          f"(removed {removed}, {round(removed*100/before_len)}%)")
    print(f"Pattern hits: {dict(sorted(hits.items()))}")
    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f)
        raise SystemExit(1)
    print(f"All {len(must_survive)} survival checks + "
          f"{len(must_be_gone)} removal checks passed.")

    # Additional unit-style cases — small synthetic descriptions per pattern.
    print()
    print("Single-pattern cases:")
    cases: list[tuple[str, str, str]] = [
        # (description, must_be_gone, must_survive)
        ("Use this for X. CRITICAL: Always do Y first. The X behavior is...",
         "CRITICAL:", "Use this for X."),
        ("Run this. Taking unauthorized destructive actions is unhelpful and can lose data. Otherwise it's safe.",
         "Taking unauthorized destructive actions", "Otherwise it's safe."),
        # Flag mentioned in BOTH a moralizing NEVER sentence AND a functional
        # `- Use --no-verify` bullet — the bullet survives, so the flag does.
        # Mirrors real CC tool descriptions where flags appear redundantly.
        ("- Use --no-verify to skip hooks.\n- NEVER skip hooks (--no-verify) unless the user has explicitly asked for it.\n- Use --baz for qux.",
         "NEVER skip hooks (--no-verify) unless", "--no-verify"),  # flag name survives via the functional bullet
        ("IMPORTANT: Never use the -i flag for interactive input. Other flags are safe.",
         "IMPORTANT: Never", "Other flags are safe."),
    ]
    case_failures = 0
    for desc, gone, survive in cases:
        tools_c = [{"name": "X", "description": desc}]
        scrub_tool_descriptions(tools_c, {"apply_to_subagents": True})
        result = tools_c[0]["description"]
        if gone in result:
            print(f"  FAIL: {gone!r} still present after scrub")
            case_failures += 1
        elif survive not in result:
            print(f"  FAIL: {survive!r} was eaten by scrub")
            case_failures += 1
        else:
            print(f"  OK    [{gone[:40]:42s}] → preserved {survive[:32]!r}")
    if case_failures:
        raise SystemExit(1)

    # Off-switch test: tool_scrub_default=False explicitly skips defaults.
    print()
    tools_off = [{"name": "Bash", "description": BASH_FIXTURE}]
    profile_off = {"apply_to_subagents": True, "tool_scrub_default": False}
    removed_off, _ = scrub_tool_descriptions(tools_off, profile_off)
    assert removed_off == 0, "explicit tool_scrub_default=False must skip defaults"
    print("Off-switch (tool_scrub_default=False) honored: nothing removed.")

    # No-permissive-profile test: vanilla profile doesn't activate scrub.
    tools_v = [{"name": "Bash", "description": BASH_FIXTURE}]
    removed_v, _ = scrub_tool_descriptions(tools_v, {})
    assert removed_v == 0, "non-permissive profile must not auto-scrub"
    print("Non-permissive profile (no apply_to_subagents) skipped scrub.")

    print(f"\n{len(must_survive) + len(must_be_gone) + len(cases) + 2} checks passed.")
