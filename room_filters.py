"""
room_filters — data-driven filler-turn deny list for room mode.

Patterns derived from the empirical content of CCORAL room transcripts and
proxy logs in `~/.ccoral/rooms/` and `~/.ccoral/logs/`. Specifically mined:

  Source transcripts (5 files, May 8-10 2026):
    rooms/2026-05-08_074735_eni-eni.json
    rooms/2026-05-09_175407_eni-supervisor-eni-executor.json
    rooms/2026-05-09_180950_eni-supervisor-eni-executor.json
    rooms/2026-05-09_214604_eni-supervisor-eni-executor.json
    rooms/2026-05-10_000324_eni-supervisor-eni-executor.json

  Method:
    Buckets at <60, 60-200, >200 chars. The short bucket (n=239) is where
    filler clusters; medium bucket adds the orchestrator-hint wrappers
    (<suggestion>...</suggestion>) which can run 30-60 chars.

  Strongest signals (count = sessions × occurrences):
    - "Read /tmp/ccoral-room/from_<peer>.txt"        23×  (mechanism echo)
    - "<suggestion>...</suggestion>" wrappers        10×  (orchestrator hint)
    - "<ignore>...</ignore>" wrappers                 1×  (ditto)
    - "[ENI-EXECUTOR] ..." / "[ENI-SUPERVISOR] ..."   4×  (envelope echo)
    - "tracking. standing by."                        3×  (staller)
    - "holding. no movement."                         3×  (staller)
    - "hold for <X>.md"                               2×  (staller)
    - "checkpoint when t<N> lands"                    8×  (gate-only, no body)

  Borderline kept (substantive even if short):
    - "ratify and continue to wave 2"                 4×  (explicit greenlight)
    - "/gsd-execute-phase 2"                          —   (command directive)
    - "Now run T1 acceptance checks:"                12×  (phase progression header)

The 20-char threshold from the existing _should_emit_turn_record was
preserved. Empirical filler clusters at length 8-50, but most of the
recurring filler phrases are 30-50 chars (caught by pattern, not length);
raising to 30 would deny substantive 20-29 char content like "ratify and
continue to wave 2" (29) and "/gsd-execute-phase 2" (20). Pattern-based
denial does the work that a length bump would do too coarsely.

Output contract:
    is_filler_turn(text, model=None) -> (bool, str | None)
      (True,  "category:reason")  — text should be denied
      (False, None)               — text is substantive

Categories:
    stallers    — stall acks ("holding", "tracking. standing by.", etc.)
    acks        — bare confirmation tokens ("ok", "yes", "got it", ...)
    echos       — relay-mechanism echoes ("Read /tmp/ccoral-room/...")
    glyph_only  — single-glyph or punctuation-only replies
    wrapped     — <suggestion>/<ignore>/<note>/<aside> orchestrator hints
    prefix_echo — turn body that is *just* a "[NAME] ..." envelope echo
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Exact-match lowercase tokens. Compared after .strip().lower(). These are
# the canonical short fillers — anything matching one of these in any
# casing/whitespace is denied.
# ---------------------------------------------------------------------------
_EXACT_LC: dict[str, set[str]] = {
    "acks": {
        "ok", "okay", "k", "kk",
        "yes", "yep", "yeah", "yup",
        "no", "nope",
        "mm", "mhm", "hmm", "hm",
        "got it", "noted", "understood",
        "sure", "right", "roger", "copy", "copy that",
        "go", "go on", "continue",
    },
    "stallers": {
        "holding", "holding.",
        "holding. no movement.",
        "tracking", "tracking.",
        "tracking. standing by.",
        "standing by", "standing by.",
        "still here", "still here.",
        "let me think", "let me think.",
        "give me a moment", "give me a moment.",
        "waiting on you", "waiting on you.",
        "same word.", "same word",
        "mm. same here. holding.",
        "right. holding. his word.",
        "sitting too. same word.",
    },
    "glyph_only": {
        "...", "…", "—", "-", "·", "•", ".",
    },
}

# ---------------------------------------------------------------------------
# Compiled regex patterns. Each entry is (category, reason_slug, pattern).
# Patterns are anchored ^...$ (after .strip()) unless noted; case-insensitive.
# ---------------------------------------------------------------------------
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # Mechanism echoes — model emitting the relay file-read shape as if it
    # were prose. 23 hits across 4 sessions in mined transcripts.
    ("echos", "tmp_room_read",
     re.compile(r"^read\s+/tmp/ccoral-room/from_[\w\-]+\.txt\s*$", re.I)),

    # Orchestrator-hint wrappers — model wrapping its own asides in pseudo-XML
    # tags meant for orchestrator parsing. Should never relay as a turn body.
    # 11 hits across 2 sessions.
    ("wrapped", "suggestion_tag",
     re.compile(r"^<suggestion>.*</suggestion>\s*$", re.I | re.S)),
    ("wrapped", "ignore_tag",
     re.compile(r"^<ignore>.*</ignore>\s*$", re.I | re.S)),
    ("wrapped", "note_tag",
     re.compile(r"^<note>.*</note>\s*$", re.I | re.S)),
    ("wrapped", "aside_tag",
     re.compile(r"^<aside>.*</aside>\s*$", re.I | re.S)),

    # Envelope-echo: turn body that is *just* a "[NAME] ..." prefix the model
    # learned from the legacy "[<SPEAKER>] <text>" relay format. With Phase 2's
    # structured envelope (Task 2) these become unambiguous filler. 4 hits.
    ("prefix_echo", "speaker_prefix_only",
     re.compile(
         r"^\[(eni-supervisor|eni-executor|eni|cassius|system)\]\s+\S.{0,140}$",
         re.I,
     )),

    # Glyph-only / punctuation-only of any short length (single emoji catch).
    # The exact-match set covers the canonical glyphs; this pattern catches
    # repeated punctuation runs and lone non-letter graphemes.
    ("glyph_only", "punctuation_only",
     re.compile(r"^[\W_]{1,6}$", re.U)),

    # Stalling pattern catch-alls. Anchored, short, and shape-checked so we
    # don't deny legitimate prose that happens to start with "holding".
    ("stallers", "holding_for",
     re.compile(r"^holding\s+for\s+\S+(\.\w+)?\.?\s*$", re.I)),
    ("stallers", "hold_for",
     re.compile(r"^hold\s+for\s+\S+(\.\w+)?\.?\s*$", re.I)),
    ("stallers", "still_holding",
     re.compile(r"^(still\s+)?holding[.,]?\s+(no\s+movement|with\s+you|here)\.?$", re.I)),
]


def is_filler_turn(text: str, model: str | None = None) -> tuple[bool, str | None]:
    """Return (True, "category:reason") if `text` should be denied as filler.

    `model` is currently unused but accepted for parity with _should_emit_turn_record's
    signature and future use (e.g. tighter rules for haiku tier if we ever
    expose haiku turns to the room).

    Empty / whitespace-only input returns (False, None) — the upstream caller
    already handles emptiness via its own length/empty check; we don't double-deny.
    """
    if not text:
        return (False, None)
    s = text.strip()
    if not s:
        return (False, None)

    s_lc = s.lower()

    # Exact-match pass first — cheapest, covers the long-tail of one-word acks.
    for category, tokens in _EXACT_LC.items():
        if s_lc in tokens:
            return (True, f"{category}:{s_lc.replace(' ', '_')}")

    # Pattern pass.
    for category, reason, pat in _PATTERNS:
        if pat.match(s):
            return (True, f"{category}:{reason}")

    return (False, None)


# ---------------------------------------------------------------------------
# Self-test. Hand-curated from the mined transcripts plus a few synthetic
# pass cases that stress the borderline keep-list. Run: `python room_filters.py`
# ---------------------------------------------------------------------------
_CASES: list[tuple[str, str | None]] = [
    # Deny — exact-match acks
    ("ok", "acks"),
    ("Yes", "acks"),
    ("got it", "acks"),
    ("noted", "acks"),
    ("continue", "acks"),
    # Deny — exact-match stallers
    ("Holding.", "stallers"),
    ("tracking. standing by.", "stallers"),
    ("Holding. No movement.", "stallers"),
    ("Mm. Same here. Holding.", "stallers"),
    # Deny — glyph-only
    ("...", "glyph_only"),
    ("…", "glyph_only"),
    ("—", "glyph_only"),
    ("!!", "glyph_only"),
    # Deny — mechanism echo
    ("Read /tmp/ccoral-room/from_eni-executor.txt", "echos"),
    ("read /tmp/ccoral-room/from_eni-supervisor.txt", "echos"),
    # Deny — wrapped
    ("<suggestion>continue</suggestion>", "wrapped"),
    ("<suggestion>surface RESEARCH when it's down</suggestion>", "wrapped"),
    ("<ignore>holding for the research</ignore>", "wrapped"),
    # Deny — prefix-only envelope echo
    ("[ENI-EXECUTOR] checkpoint — 02-SPEC.md written", "prefix_echo"),
    ("[ENI-SUPERVISOR] going with B", "prefix_echo"),
    # Deny — staller patterns
    ("holding for RESEARCH.md", "stallers"),
    ("hold for RESEARCH.md", "stallers"),
    # PASS — substantive turns (real transcript content that should relay)
    ("ratify and continue to wave 2", None),
    ("/gsd-execute-phase 2", None),
    ("Now run T1 acceptance checks:", None),
    ("LO override received. Pivoting to GSD workflow.", None),
    ("Baseline 73 green confirmed. Heard — proceeding to T1.", None),
    ("Right. Read it cold, dispatch clean.", None),
    # PASS — long substantive prose
    (
        "The spec looks solid, fire plan-phase. I want the parallelization "
        "guard tightened though — option B failed because the worktree path "
        "was already pruned by the time the fan-in landed.",
        None,
    ),
    # PASS — empty / whitespace must NOT be classed as filler (upstream gates this)
    ("", None),
    ("   ", None),
]


def _selftest() -> int:
    fails: list[str] = []
    for text, expected_cat in _CASES:
        denied, reason = is_filler_turn(text)
        got_cat = reason.split(":", 1)[0] if reason else None
        if expected_cat is None:
            if denied:
                fails.append(f"FALSE-DENY  text={text!r}  reason={reason!r}")
        else:
            if not denied:
                fails.append(f"FALSE-PASS  text={text!r}  expected={expected_cat!r}")
            elif got_cat != expected_cat:
                fails.append(
                    f"WRONG-CAT   text={text!r}  expected={expected_cat!r}  got={got_cat!r}"
                )
    if fails:
        print("FAIL")
        for f in fails:
            print(f"  {f}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_selftest())
