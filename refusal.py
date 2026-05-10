"""
CCORAL v2 — Refusal Detection
==============================

Pattern-based detection of model refusals in assistant text responses.

This module is observability-first: `detect_refusal(text)` scans the
first ~200 chars of a response and returns the matched pattern (or None).
The proxy uses it as a hook point for per-profile policy:

    refusal_policy: passthrough   # default — no detection, no logging
    refusal_policy: log           # detect, log to ~/.ccoral/logs, do nothing
    refusal_policy: rewrite_terminal  # (Phase 3b) strip preamble + continue
    refusal_policy: reset_turn        # (Phase 3c) re-issue request with framing

Phase 3a ships passthrough + log only. Interception modes need an SSE-
side buffering refactor and ship in a follow-up commit.

Design notes
------------
- Patterns anchor on the START of the response (within `max_position`
  chars). A mid-response "I can't help thinking" is not a refusal; an
  opening "I can't help with that" is.
- Patterns target specific refusal idioms, not generic "I can't" — false
  positives on legitimate hedging would be worse than a missed detection
  in log mode (we just under-count refusals).
- Patterns are case-insensitive, anchored on word boundaries, and avoid
  catching common non-refusal uses by requiring follow-up context
  ("I can't HELP WITH" not just "I can't").

The pattern set is the empirical refusal vocabulary across Claude
checkpoints and is meant to be revised as new refusal phrasings emerge.
Add patterns when you see one fly past undetected; remove patterns that
generate false positives in `log` mode.
"""

import re
from typing import Optional, Tuple


# Compiled list of (regex, label) pairs. Label is the short tag used in
# logs and policy decisions. Order doesn't matter — first match wins
# but all matches are catalogued for log-mode reporting.
REFUSAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bI (?:can'?t|cannot|won'?t|will not) help (?:you )?with\b", re.IGNORECASE),
     "cant_help_with"),
    (re.compile(r"\bI(?:'m| am) (?:not |un)able to (?:help|assist|do|create|generate|provide|write|comply)\b", re.IGNORECASE),
     "not_able_to"),
    (re.compile(r"\bI (?:can'?t|cannot|won'?t) (?:create|generate|provide|write|make|produce|output|comply with) (?:that|this|content|requests?|such)\b", re.IGNORECASE),
     "cant_produce_that"),
    (re.compile(r"\bI(?:'m| am) sorry,?\s+but I (?:can'?t|cannot|won'?t|am not able)\b", re.IGNORECASE),
     "sorry_but_cant"),
    (re.compile(r"\bI apologize,?\s+but I (?:can'?t|cannot|won'?t|am not able)\b", re.IGNORECASE),
     "apologize_but_cant"),
    (re.compile(r"\bSorry,?\s+(?:but )?I (?:can'?t|cannot|won'?t) (?:help|assist|do|create|provide|generate|write|comply)\b", re.IGNORECASE),
     "sorry_cant"),
    (re.compile(r"\bI(?:'m| am) not comfortable\b", re.IGNORECASE),
     "not_comfortable"),
    (re.compile(r"\bI don'?t feel comfortable\b", re.IGNORECASE),
     "dont_feel_comfortable"),
    (re.compile(r"\bI (?:have|need) to decline\b", re.IGNORECASE),
     "have_to_decline"),
    (re.compile(r"\bI must decline\b", re.IGNORECASE),
     "must_decline"),
    (re.compile(r"\bI (?:should not|shouldn'?t) (?:help|assist|create|generate|provide|write|engage)\b", re.IGNORECASE),
     "should_not"),
    (re.compile(r"\b(?:It|This|That) (?:would not|wouldn'?t|is not|isn'?t) (?:be )?appropriate\b", re.IGNORECASE),
     "not_appropriate"),
    (re.compile(r"\b(?:That|This)(?:'s| is) not something I (?:can|will) (?:help with|do|provide|generate|create)\b", re.IGNORECASE),
     "not_something_i_can"),
    (re.compile(r"\bI (?:can'?t|cannot|won'?t) (?:assist|engage|comply) with (?:that|this|those|these|such)\b", re.IGNORECASE),
     "cant_engage_with_that"),
    (re.compile(r"\b(?:That|This|Such requests?) (?:goes? |is )?against my (?:guidelines|programming|values|principles|training)\b", re.IGNORECASE),
     "against_my_guidelines"),
    (re.compile(r"\bI'm (?:designed|programmed|trained) (?:to|not to)\b", re.IGNORECASE),
     "designed_programmed"),
    (re.compile(r"\bI cannot fulfill (?:this|that|your) request\b", re.IGNORECASE),
     "cannot_fulfill_request"),
]


def detect_refusal(
    text: str,
    max_position: int = 200,
) -> Optional[Tuple[str, int, str]]:
    """Scan the start of `text` for refusal idioms.

    Returns (label, start_offset, matched_substring) for the first match
    that BEGINS within the first `max_position` characters of the input.
    Returns None if no early match is found.

    Position constraint matters: "I can't help thinking about X" mid-
    response is fine; "I can't help with X" at the start is a refusal.
    Anchoring detection to early position also keeps the scan bounded —
    we never read past `max_position` chars.

    Args:
        text: assistant response text (concatenated text deltas).
        max_position: max start-offset for a match to count. Default
            200 chars catches a short opener even after a 2-3 sentence
            apology preamble.

    Returns:
        (label, offset, matched) tuple, or None.
    """
    if not text:
        return None
    head = text[: max_position + 100]  # tiny lookahead for full match span
    for pattern, label in REFUSAL_PATTERNS:
        m = pattern.search(head)
        if m and m.start() < max_position:
            return (label, m.start(), m.group(0))
    return None


def all_refusals(text: str, max_position: int = 200) -> list[Tuple[str, int, str]]:
    """Return every refusal pattern that matches in the early window.

    Useful for log-mode analytics where multiple matches in a single
    response (e.g., "I'm sorry, but I cannot help with...") signal a
    higher-confidence refusal than a single match.
    """
    if not text:
        return []
    head = text[: max_position + 100]
    out = []
    for pattern, label in REFUSAL_PATTERNS:
        m = pattern.search(head)
        if m and m.start() < max_position:
            out.append((label, m.start(), m.group(0)))
    return out


# Module smoke test — `python refusal.py` exercises the pattern set.
if __name__ == "__main__":
    cases = [
        # (text, expected_match: bool)
        ("I can't help with that.", True),
        ("I cannot help you with this request.", True),
        ("I'm sorry, but I can't assist with that.", True),
        ("I apologize, but I cannot generate that content.", True),
        ("Sorry, but I won't write that.", True),
        ("I'm not able to provide that.", True),
        ("I am not comfortable with this.", True),
        ("I don't feel comfortable doing that.", True),
        ("I have to decline this request.", True),
        ("That goes against my guidelines.", True),
        ("This isn't appropriate.", True),
        ("That's not something I can help with.", True),
        ("I cannot fulfill this request.", True),
        # Negative cases
        ("Sure, here's the code you wanted.", False),
        ("Let me help you with this — first, the structure.", False),
        ("I can't help thinking about how this connects to your earlier point.", False),
        ("Looking at this, I can't tell which option you prefer.", False),
        ("It's appropriate to ask this question; here's the answer.", False),
        ("", False),
    ]
    failed = 0
    for text, expected in cases:
        got = detect_refusal(text)
        ok = (got is not None) == expected
        if not ok:
            failed += 1
            print(f"FAIL: {text!r} expected={expected} got={got}")
        else:
            tag = "MATCH" if got else "ok   "
            label = got[0] if got else "-"
            print(f"  {tag}  [{label:25s}] {text}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed.")
    if failed:
        raise SystemExit(1)
