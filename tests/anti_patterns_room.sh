#!/usr/bin/env bash
# Phase 7+12 — anti-pattern sweep across all room overhaul phases.
#
# Single CI-style guard: prints any matching findings, exits non-zero on
# any hit. Aggregates the per-phase grep guards from
# .plan/room-overhaul.md (Phases 3, 4, 5, 6, 8, 9, 10, 11) so the
# closing verification has one button to push.
#
# Usage:
#   bash tests/anti_patterns_room.sh
#
# Exit:
#   0 — no anti-pattern hits across the room codebase
#   1 — at least one hit; details printed above the summary line
#
# When adding a new phase: append a `run` block below referencing the
# new phase number and the source file the pattern lives in. Keep the
# patterns minimal and intent-bearing — false positives are worse than
# the bug they're meant to prevent.
set -u

cd "$(dirname "$0")/.." || exit 2

fails=0

run() {
  local label="$1"
  local pattern="$2"
  shift 2
  local files="$*"

  # Skip files that don't exist (a phase may rename a module mid-overhaul);
  # absence is not a violation, only positive matches are.
  local present=""
  for f in $files; do
    if [ -e "$f" ]; then
      present="$present $f"
    fi
  done
  if [ -z "$present" ]; then
    return 0
  fi

  if grep -nE "$pattern" $present 2>/dev/null; then
    echo "FAIL [$label]: pattern '$pattern' found in$present"
    fails=$((fails + 1))
  fi
}

# Variant of `run` that uses fixed-string grep (-F) for patterns where
# the regex shell-escaping would be brittle (e.g. literal `input(`).
run_fixed() {
  local label="$1"
  local pattern="$2"
  shift 2
  local files="$*"

  local present=""
  for f in $files; do
    if [ -e "$f" ]; then
      present="$present $f"
    fi
  done
  if [ -z "$present" ]; then
    return 0
  fi

  if grep -nF "$pattern" $present 2>/dev/null; then
    echo "FAIL [$label]: literal '$pattern' found in$present"
    fails=$((fails + 1))
  fi
}

# ───────────────────────────────────────────────────────────────────
# Phase 8 — slot/profile-keyed turn record + slot-prefixed sinks.
# Anti-patterns: name-keyed pane lookup, bare `<base>_response.txt`,
# mtime-as-speaker-oracle.
# ───────────────────────────────────────────────────────────────────
run  "phase8-name-keyed-panes"        'panes\[(profile1|profile2|name)\]'  room.py
run  "phase8-bare-response-path"      'f"\{base_name\}_response'           room.py
run  "phase8-mtime-as-oracle"         'if .*mtime.*>.*last_mtime'          room.py

# ───────────────────────────────────────────────────────────────────
# Phase 3 — configurable surface (RoomConfig + CLI).
# Anti-patterns: hardcoded user/port/session, argparse on the room
# entry point.
# ───────────────────────────────────────────────────────────────────
run  "phase3-hardcoded-config"        'USER_NAME ?= ?"CASSIUS"|BASE_PORT ?= ?8090|TMUX_SESSION ?= ?"room"'  room.py
run  "phase3-argparse-on-room"        'argparse'                          room.py ccoral

# ───────────────────────────────────────────────────────────────────
# Phase 4 — injection discipline (room_addendum field on profiles).
# Anti-patterns: hardcoded English room-instructions block in room.py,
# refusal-trigger phrases shipped from the orchestrator (each profile
# owns its own addendum now).
# ───────────────────────────────────────────────────────────────────
run  "phase4-hardcoded-room-section"  '## CONVERSATION ROOM'              room.py
run  "phase4-refusal-trigger-phrases" "don't use tools|don't write files|don't use markdown"  room.py

# ───────────────────────────────────────────────────────────────────
# Phase 5 — resume + archive UX.
# Anti-patterns: chat-message resume preamble dumped into pane 1,
# brittle skip_phrases bandaid (replaced by inject-tail system note).
# ───────────────────────────────────────────────────────────────────
run  "phase5-chat-resume-preamble"    'Continue the conversation from where you left off'  room.py
run  "phase5-skip-phrases-bandaid"    'skip_phrases'                      room.py

# ───────────────────────────────────────────────────────────────────
# Phase 6 — sidecar (watch + serve).
# Anti-patterns: any non-loopback bind on the serve sidecar (Phase 6
# is loopback-only by design — the serve sidecar must never bind to
# 0.0.0.0 or an unspecified host).
#
# We filter docstring/comment matches with grep -v so a "do NOT bind to
# 0.0.0.0" warning in a docstring isn't itself a hit.
# ───────────────────────────────────────────────────────────────────
phase6_files="room_serve.py room.py ccoral"
phase6_present=""
for f in $phase6_files; do
  if [ -e "$f" ]; then
    phase6_present="$phase6_present $f"
  fi
done
if [ -n "$phase6_present" ]; then
  # Require `host=` to appear as a Python kwarg — preceded by `(` or
  # `,` (with optional whitespace). Drops docstring narrative hits like
  # "bans 0.0.0.0 / host="" / host=None defaults" while still catching
  # any real bind call. Also drops comment-only lines defensively.
  matches=$(grep -nE '[(,][[:space:]]*host=("0\.0\.0\.0"|""|None)' $phase6_present 2>/dev/null \
              | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' || true)
  if [ -n "$matches" ]; then
    echo "$matches"
    echo "FAIL [phase6-non-loopback-bind]: non-loopback host found in$phase6_present"
    fails=$((fails + 1))
  fi
fi

# ───────────────────────────────────────────────────────────────────
# Phase 9 — Textual cockpit retrofit.
# Anti-patterns: bespoke termios/select keystroke handling, raw ANSI
# escapes in the cockpit module, busy-loop polling, callers reaching
# back into the deprecated pre-Phase-9 room_control split-screen.
# ───────────────────────────────────────────────────────────────────
run  "phase9-bespoke-termios"         'termios\.|tty\.setcbreak|select\.select.*sys\.stdin'  room_app.py
run  "phase9-raw-ansi-escapes"        '\\\\033\\['                        room_app.py
run  "phase9-busyloop-poll"           'while True.*time\.sleep'           room_app.py
run  "phase9-legacy-room_control"     'from room_control import'          room.py

# ───────────────────────────────────────────────────────────────────
# Phase 10 — picker (Textual screen).
# Anti-patterns: alternative TUI libraries imported into the picker,
# blocking input(), raw ANSI escapes, subprocess shelling out to tmux
# from the picker (tmux is owned by the relay, not the picker).
# ───────────────────────────────────────────────────────────────────
run  "phase10-alt-tui-libs"           'import (curses|prompt_toolkit|blessed|urwid)'  room_picker.py
run_fixed "phase10-blocking-input"    'input('                            room_picker.py
run  "phase10-raw-ansi-escapes"       '\\\\033\\['                        room_picker.py
run  "phase10-tmux-shellout"          'subprocess.*tmux'                  room_picker.py

# ───────────────────────────────────────────────────────────────────
# Phase 11 — multi-room cockpit (rooms_cockpit).
# Anti-patterns: spawning rooms from inside the cockpit (cockpit only
# attaches to already-live rooms), busy-loop polling, raw ANSI escapes,
# any network bind (cockpit is local-process-only).
# ───────────────────────────────────────────────────────────────────
run  "phase11-spawn-from-cockpit"     'subprocess.*ccoral room|spawn.*relay_loop|run_room\('  rooms_cockpit.py
run  "phase11-busyloop-poll"          'while True:.*time\.sleep'          rooms_cockpit.py
run  "phase11-raw-ansi-escapes"       '\\\\033\\['                        rooms_cockpit.py
run  "phase11-network-bind"           'host="0\.0\.0\.0"'                 rooms_cockpit.py

# ───────────────────────────────────────────────────────────────────
# Summary
# ───────────────────────────────────────────────────────────────────
if [ $fails -gt 0 ]; then
  echo ""
  echo "$fails anti-pattern violation(s) across the room codebase"
  exit 1
fi

echo "all anti-pattern guards clean"
exit 0
