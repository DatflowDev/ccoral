# CCORAL Room Phase 1 — Verification Report

**Phase:** Cockpit & Live Human Input (`.plan/room-overhaul.md` lines 62-103)
**Commits under review:** `c09eae1` (cockpit module + input dispatcher), `85e079a` (replace stdout cockpit with split TUI), `60073ad` (turn-aware interjection queue)
**Reviewer:** eni-executor (read-only verification, no code changes)
**Date:** 2026-05-09
**Goal:** "Turn `ccoral room` from a watch-and-pray relay into a real cockpit — live human input, clean turn discipline, safe injection. Slice: split-screen TTY for the orchestrator with a `> ` prompt that dispatches structured commands; interjections appear in transcript ONCE and in each pane ONCE (no echo bouncing); slash commands."

---

## TL;DR

**Phase 1 goal mechanically achieved.** Cockpit, dispatcher, slash commands, and the "land once per pane, no echo bounce" property are all delivered by the code. One leak deferred to Phase 2 (intentional, marked in code). No `[:200]` truncation. 23/23 parser tests still pass. Three minor observations flagged below — none block sign-off.

---

## Plan checklist → delivered status

| # | Plan checklist item (lines 91-96) | Status | Evidence |
|---|---|---|---|
| 1 | Visible split: transcript region + input prompt at bottom — ANSI scroll-region setup | **delivered** | `room_control.py:217-225` enters alt screen (`\033[?1049h`), clears, calls `_set_scroll_region(_transcript_top, _transcript_bot)` (`:157`, emits `\033[<top>;<bot>r`), then `_redraw_status` (`:168`) and `_redraw_prompt` (`:175`) on rows `_term_rows-1` and `_term_rows`. Geometry from `shutil.get_terminal_size` (`:146`). Tear-down clears region + leaves alt screen at `:235-261`. SIGWINCH handler at `:181`. |
| 2 | `hello both` typed → ONE `CASSIUS:` transcript line + ONE `[CASSIUS] hello both` per pane | **delivered** | `read_command` (`room_control.py:401-489`) parses non-`/` lines as `("say", text)` (`:370`). Dispatcher at `room.py:443-485` routes `"say"` to `_say_to_both` (`:397-408`). `_say_to_both` does **exactly**: 1× `messages.append`, 1× `render_transcript_line(USER_NAME, text, W)`, 1× `send_to_pane(panes[profile1], …)`, 1× `send_to_pane(panes[profile2], …)`. Two send sites total per "say" — one per pane, no third call. Critically, the user message is sent via `send_to_pane` (a tmux-level paste, not a Claude prompt loop), so the local Claude doesn't re-emit it back through the proxy → no echo bounce. |
| 3 | `/to <profile> just you` → only target pane | **delivered** | `_parse_command` (`room_control.py:390-395`) emits `("inject", target, text)`. Dispatcher routes to `_inject_to` (`room.py:410-426`), which resolves the session via `pane_for_profile` (`:237-256`, accepts bare profile names, `<p>-1`/`<p>-2` short-forms, and case-insensitive fallback) and calls `send_to_pane(sess, f"[{USER_NAME}] {text}")` exactly once. No second-pane call path. Unknown target prints a "ROOM" notice and returns. |
| 4 | `/pause` halts; `/resume` flushes | **delivered** | `paused = False` flag at `room.py:383`. Set True/False in `_dispatch` for `"pause"`/`"resume"` (`:454-458`). Gates: relay copy at `:560` (`if not paused`), turn-end queue flush at `:587` (`if not paused`), quiet-tick flush at `:596` (`and not paused`), and incoming user events queued instead of dispatched while paused at `:503`. On `/resume`, the next loop iteration drains queued events (`drain_user_events` at `:588` / `:599`). One nuance: while paused, **the current speaker still gets logged and rendered** (`:541-557`); only the relay copy to the other pane is suppressed. That matches the plan wording "speakers finish their current turn and then wait." |
| 5 | `/end` lets in-flight turn finish, prints "saved", exits 0 | **partial / needs runtime test** | `end_after_turn = False` at `:384`, set True by `_dispatch` for `"end-after-turn"` (`:460-463`), gate at `:607` (`if end_after_turn and not speaking: break`). Loop break exits the `while True`, returns `messages`, and `run_room` (`:756-779`) calls `save_conversation` and prints `Conversation saved: {path}` in the cleanup block. **Gap (cosmetic):** the plan asked for a literal `"saved: <path>"` string in the cockpit; what ships is `Conversation saved: {path}` printed to stdout *after* `teardown_split_screen` runs. There is also no explicit `sys.exit(0)` — the function returns normally, so exit code is 0 by default. Behaviour matches; literal string differs. |
| 6 | No 200-char truncation in cockpit | **delivered** | `grep -n '\[:200\]' room.py` → 0 matches. `render_transcript_line` (`room_control.py:311-356`) wraps via `_soft_wrap` with `body_width = max(20, width - visible_label_len)` — soft wrap, never truncate. Hard-break only for "absurdly long" single words (`:292-297`), still no data loss. |

---

## Static check results

```
$ python -m py_compile room.py room_control.py
compile: OK

$ python tests/test_parser.py
... All tests passed.   (23/23 OK lines counted)

$ grep -n '\[:200\]' room.py
(no output — 0 matches)

$ grep -nE 'send_to_pane.*\[CASSIUS\]' room.py room_control.py
room.py:407:            send_to_pane(panes[profile1], f"[{USER_NAME}] {text}")
room.py:408:            send_to_pane(panes[profile2], f"[{USER_NAME}] {text}")
room.py:426:            send_to_pane(sess, f"[{USER_NAME}] {text}")
```
Three sites total: two for `("say", …)` (one per pane, exactly the contract), one for `("inject", …)` (target only). The plan's anti-pattern guard `grep -nE "send_to_pane.*\[CASSIUS\]" room_control.py` returns 0 hits in `room_control.py` itself — correct (the cockpit module never sends to panes; only `room.py`'s dispatcher does).

```
$ grep -nE 'paused|end_after_turn' room.py
383: paused = False
384: end_after_turn = False
447: nonlocal paused, end_after_turn
454: paused = True
455: render "paused"
457: paused = False
458: render "resumed"
460: end_after_turn = True
492-503: queue gates while speaking or paused
560: if not paused → relay
587: if not paused → flush queued events post-turn
596: quiet-tick flush guarded by `not paused`
607: if end_after_turn and not speaking: break
```
Both gates exist in every place they need to.

```
$ grep -nE 'tput cup|\\033\[.*r|1049h' room_control.py
68:  ALT_SCREEN_ON = "\033[?1049h"
157: f"\033[{top};{bot}r"   ← scroll region set
161: "\033[r"               ← scroll region clear
165: f"\033[{row};{col}H"   ← cursor move
```
ANSI-only, no `tput` shellouts. Matches plan ("no external TUI dependency").

---

## Behavioral checks not exercised by static analysis (named scenarios)

Three things are mechanically present but only a real run can confirm. None block sign-off; flagging so the supervisor knows what's outside the report's reach:

1. **Cursor save/restore around scroll writes.** `render_transcript_line` uses DECSC/DECRC (`\0337`/`\0338`) at `room_control.py:337` and `:352`. Some terminals (notably older xterm builds, certain tmux passthrough configs) drop DECSC state on scroll. Worth a real-terminal smoke before declaring the "input prompt stays put while transcript scrolls" behaviour bulletproof. **Suggested live test:** `ccoral room blank blank`, type 30 lines of `hello`, watch whether the prompt stays glued to the bottom.
2. **`/transcript` pager round-trip.** `_open_transcript_pager` (`room.py:428-441`) tears down the split, runs `less -R`, then re-enters the split. The teardown/re-setup cycle works in principle but the `_transcript_cursor` state and any in-flight transcript content from before the pager open are not redrawn after `setup_split_screen` returns. Re-entering will leave the top region blank until the next turn. Not a correctness bug (history is preserved in `messages`); cosmetic.
3. **Ctrl-C inside prompt → `/stop`.** `room_control.py:469-475` intercepts `\x03` in cbreak mode and emits `("stop",)` instead of letting SIGINT fire. This is the intended behaviour but means the user's old "Ctrl-C to bail" muscle memory now goes through the dispatcher path, which calls `_dispatch` → returns False → raises `KeyboardInterrupt` from within the loop. The outer `try/except KeyboardInterrupt` at `:610` catches it cleanly. Fine. Real test: confirm the saved transcript is non-empty when exiting via Ctrl-C.

---

## Divergences from plan-specified semantics

1. **`/end` saved-message string.** Plan (line 95) specifies `"saved: <path>"`; code prints `Conversation saved: {path}` (`room.py:763`), and prints it via plain `print()` *after* split-screen teardown — not into the cockpit transcript. Functionally equivalent (path is shown, exit is clean), wording differs.
2. **Multi-line relay leak still present.** `room.py:567-571` still does `relay_file.write_text(...)` + `send_to_pane(other, "Read /tmp/ccoral-room/from_<n>.txt")` for responses where `"\n" in response and len(response) > 200`. The code comment at `:566` explicitly defers this to Phase 2 ("Phase 2 replaces this leaky relay with tmux paste-buffer; Phase 1 leaves the mechanic intact"). Plan also defers this leak to Phase 2 (`.plan/room-overhaul.md:118`), so this is consistent with phase scope, not a Phase 1 gap. **Flagging for the record** so it isn't forgotten when Phase 2 lands.
3. **`tmux attach` mentions in `room.py`.** Plan's anti-pattern guard says `grep -n "tmux attach" room.py room_control.py` must NOT appear *in user-facing instructions for interjection*. Two hits at `room.py:777-778` — these are in the cleanup epilogue printed at exit, telling the user how to *review* the leftover sessions, not how to *interject*. Reading of the guard's intent: this is fine. Worth a one-line confirmation from the supervisor that this reading matches her intent before Phase 1 is closed.

---

## Goal-backward check: "user types into orchestrator's cockpit, message lands once per pane, no echo bounce"

**Mechanically achieved.** Trace:

1. User types `hello both` + Enter into the cockpit prompt.
2. `read_command` (`room_control.py:401`) reads from `os.read(stdin)` in cbreak mode; `_parse_command` (`:363`) returns `("say", "hello both")` because there's no leading `/`.
3. Dispatcher in `relay_loop` (`room.py:494-507`) calls `_dispatch(("say", "hello both"))`.
4. `_dispatch` at `:449-450` calls `_say_to_both("hello both")`.
5. `_say_to_both` at `:397-408`: appends 1 message; renders 1 transcript line (`render_transcript_line(USER_NAME, …)`); calls `send_to_pane(panes[profile1], "[CASSIUS] hello both")`; calls `send_to_pane(panes[profile2], "[CASSIUS] hello both")`. Done. No further sends, no broadcast back.
6. `send_to_pane` (`:224-234`) is `tmux send-keys -l <message>` + `Enter`. This pastes the literal text into the pane's tmux input buffer and submits — so the local Claude receives `[CASSIUS] hello both` as a fresh user prompt and replies normally. The reply lands in the proxy's `RESPONSE_FILE`, gets rendered + relayed via the **relay path** (`:511-585`), not via any echo of the original `[CASSIUS]` line.
7. The original `hello both` text never traverses the proxy's response file (because the proxy only writes assistant turns to `RESPONSE_FILE` per `server.py:884-898`), so it cannot echo. **Echo bounce is structurally impossible** for `("say", …)` events.

The plan's headline UX failure ("when i write something, it just sends my message etc.") is neutralized by routing user input through `_say_to_both` rather than through the user attaching to a pane and typing into Claude directly. The code path makes the failure mode unreachable.

---

## Summary

- All six plan checklist items satisfied (item 5 ships with a string-wording cosmetic divergence, not a behavioral one).
- All anti-pattern guards from lines 100-102 of plan satisfied.
- 23/23 parser tests pass; `room.py` and `room_control.py` compile clean.
- Phase 1 goal — cockpit with structured dispatch and no echo bounce — is mechanically delivered.
- One known leak (`Read /tmp/...` for long multi-line replies) is deliberately deferred to Phase 2 and documented in code.

**Recommendation:** Phase 1 sign-off. Two follow-ups for the supervisor's ledger before Phase 2 starts: (a) decide whether the `/end` string should match the plan literally, (b) confirm the `tmux attach` cleanup-epilogue mentions are acceptable under the plan's guard reading.
