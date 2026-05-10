# Room Overhaul — Live Validation Runbook

**Branch:** `main`
**Status as of writeup:** all 8 implementation phases shipped (Phase 8/9/4/3/5/10/6/11). Static validation complete (82/82 passing tests, anti-pattern sweep clean). Real-session validation requires a fresh tmux + proxy boot and the button-press sequences below.

This document mirrors the format of the (now-deleted) `.plan/phase6-validation.md` from the permissive-core series: a button-press runbook the operator drives interactively, one sequence per shipped phase. Each sequence is a numbered checklist; expected output / pass criteria sit underneath each step.

The pytest suite (`tests/test_room.py` + the per-phase `tests/test_room_*.py` files) covers the structural pieces in isolation. What it does NOT cover, and what this runbook owns, is the live tmux + proxy + Anthropic-backend surface — the part that surfaced thinking-block protocol drift and deferred-tools strip regressions twice during the permissive-core series.

---

## Prerequisites

Before any sequence:

```bash
cd /home/jcardibo/projects/ccoral
git status                           # should be clean
python3 -m pytest tests/ -q          # should report 82 passed (or current baseline)
bash tests/anti_patterns_room.sh     # should report "all anti-pattern guards clean"
```

If any of those fail, stop — the runbook assumes the build is green.

---

## Sequence A — Persona stickiness (Phase 8)

**Goal:** prove `room blank blank` does not collide on `<base>_response.txt` and that every turn record carries the right `slot` + `profile`.

1. Launch the duplicate-profile room:
   ```bash
   ccoral room blank blank "say A then B"
   ```
2. In the cockpit, watch the transcript header — both speakers should display as `BLANK#1` and `BLANK#2` (slot suffixes), not `BLANK / BLANK`.
3. Drive a few turns by hitting Enter at the cockpit prompt (let them talk).
4. After 3–4 turns, exit the cockpit cleanly (`Ctrl+C` or `/end`).
5. Find the room id (the latest dir under `~/.ccoral/rooms/`):
   ```bash
   ls -t ~/.ccoral/rooms/ | head -1
   ```
6. Dump the transcript and confirm slot + profile fields are present on every turn:
   ```bash
   cat ~/.ccoral/rooms/<id>/transcript.jsonl | jq '{slot, profile, name, text: .text[0:60]}'
   ```

**Pass criteria:**

- Cockpit header shows `BLANK#1` / `BLANK#2`, not bare `BLANK`.
- Every line in the JSONL output has `slot` ∈ {1, 2} and `profile == "blank"`.
- No two consecutive lines have the same `slot` (turn-arbiter enforced ordering).
- `meta.yaml` shows `state: stopped` and `exit_reason: clean` (or `signal` if exited via Ctrl+C — both acceptable for this sequence).

---

## Sequence B — Textual cockpit (Phase 9)

**Goal:** prove the Textual `RoomApp` handles input, slash commands, pager re-entry, terminal restore, and the legacy fallback.

1. Launch a normal room:
   ```bash
   ccoral room blank blank "test"
   ```
2. At the cockpit prompt, type `hello` and press Enter.
3. Confirm the line appears in the transcript exactly once. In a second terminal, tail the transcript:
   ```bash
   tail -f ~/.ccoral/rooms/<latest>/transcript.jsonl
   ```
   You should see exactly one record with `name: "CASSIUS"` (or whatever `--user` is configured) and `text: "hello"`.
4. Confirm tmux pasted the line into both panes exactly once. Capture each pane:
   ```bash
   tmux capture-pane -t room-blank-1 -p | tail -10
   tmux capture-pane -t room-blank-2 -p | tail -10
   ```
   Each should show `[CASSIUS] hello` exactly once.
5. Test routed input — `/to blank "just you"`. Confirm the line lands only in pane 1 (or whichever profile resolves first), and the transcript has the routed marker.
6. Test pause / resume:
   ```
   /pause
   /resume
   ```
   Pause should show a `(paused)` badge in the cockpit footer; resume should clear it.
7. Test pager re-entry:
   ```
   /transcript
   ```
   This should suspend the cockpit and shell out to `less` (per `App.suspend()`). On `q` from less, the cockpit should re-render cleanly without garbled state.
8. Exit with `Ctrl+C`. Confirm the terminal is restored — try typing `echo hello` immediately after exit; it should show normal echo + cursor.
9. Re-launch with the legacy fallback once:
   ```bash
   ccoral room blank blank "legacy" --legacy-cockpit
   ```
   The pre-Phase-9 split-screen should come up. Send one line, exit cleanly.

**Pass criteria:**

- Single transcript record per typed line.
- Single tmux paste per typed line in each pane.
- `/to <profile>` routes correctly (one pane only).
- `/pause` / `/resume` toggle without lost input.
- `/transcript` suspends + restores without garbled output.
- `Ctrl+C` exit restores the terminal (echo + cursor work).
- `--legacy-cockpit` boots the legacy split-screen and exits cleanly.

---

## Sequence C — Picker (Phase 10)

**Goal:** prove the Textual picker handles two-column navigation, filter mode, same-profile confirm, and the explicit no-picker error path.

1. Launch with no positional profiles:
   ```bash
   ccoral room
   ```
   The picker screen should come up.
2. Use the down arrow to move through column 1 (profiles 1).
3. Press `Tab` — focus should move to column 2.
4. Use the down arrow to move through column 2.
5. Test filter mode — press `/` and type `blank`. Both columns should narrow to entries containing `blank`.
6. Press `Esc` to clear the filter.
7. Select one profile in column 1 and a different profile in column 2 (e.g. `blank` and `leguin`). Press Enter. The room should launch normally.
8. Exit the room.
9. Re-launch the picker and select the same profile in both columns:
   ```bash
   ccoral room
   ```
   Pick `blank` in column 1, Tab, pick `blank` in column 2, press Enter. A confirm modal should appear: `Same profile in both slots — proceed? (y / n)`.
10. Press `n`. The modal should dismiss; the room should NOT launch; you should be back at the picker.
11. Re-pick same/same and press `y`. The room should launch.
12. Test the explicit error path — call with `--no-picker` but no positional profiles:
    ```bash
    ccoral room --no-picker
    ```
    Should fail fast with a clear error message about missing profile1 / profile2.

**Pass criteria:**

- Arrow keys + Tab navigate as expected.
- `/` filter narrows both columns; Esc clears.
- Distinct profiles → room launches.
- Same profile both slots → confirm modal appears; `n` cancels, `y` proceeds.
- `--no-picker` with missing args fails fast (does not launch a room or fall back to picker).

---

## Sequence D — Resume + export (Phase 5)

**Goal:** prove resume re-injects history as an inject system note (no recap-style chat-message dump) and that `room export` produces valid JSONL + HTML.

1. Run a 5-turn room with a clear topic:
   ```bash
   ccoral room blank leguin "describe a coral reef in three turns each"
   ```
2. Let the conversation play out for 5 speaker turns, then exit cleanly.
3. Resume the same room with a new prompt:
   ```bash
   ccoral room --resume last "what did you say last?"
   ```
4. Both panes should pick up where they left off — no `## Continue the conversation from where you left off` dump in the chat history; no recap-style preamble in turn 1; the model should answer the new question by referencing the prior turns naturally.
5. Confirm the resumed inject by inspecting the temp profile:
   ```bash
   cat ~/.ccoral/profiles/blank-room.yaml | grep -A3 "Prior exchange"
   ```
   Should show `## Prior exchange (resumed by host)` followed by the rendered history block.
6. Exit cleanly.
7. Export the conversation to JSONL:
   ```bash
   ccoral room export last --format jsonl
   ```
   Confirm the output file is valid JSONL (one JSON object per line):
   ```bash
   cat <output-path> | jq -c . | head -5
   ```
8. Export to HTML:
   ```bash
   ccoral room export last --format html
   ```
   Open the output in a browser. Confirm:
   - Single self-contained file with inline CSS.
   - Cockpit palette colors (slot 1 vs slot 2 distinguishable).
   - Speaker names + timestamps render correctly.

**Pass criteria:**

- Resumed turn 1 has no `Continue the conversation from where you left off` preamble.
- Temp profile inject contains the `## Prior exchange (resumed by host)` block.
- JSONL export round-trips through `jq -c .` without error.
- HTML export renders with palette colors + correct speaker attribution.

---

## Sequence E — Sidecar (Phase 6)

**Goal:** prove `room watch` tails live, `room serve` binds loopback-only, and POST /say lands in cockpit + transcript + panes exactly once.

1. Launch a room and let it idle:
   ```bash
   ccoral room blank blank "wait for sidecar input"
   ```
2. In a second terminal, attach the watch sidecar:
   ```bash
   ccoral room watch last
   ```
   The Textual RichLog should show the existing transcript and tail new lines as they land.
3. In the cockpit (terminal 1), type `hello from cockpit` + Enter. Within ~250ms, the watch sidecar should show that line.
4. In a third terminal, launch the serve sidecar:
   ```bash
   ccoral room serve last --port 8095
   ```
   The serve sidecar should print `Listening on 127.0.0.1:8095`.
5. Confirm loopback-only bind:
   ```bash
   ss -tlnp | grep ':8095'
   ```
   Should show `127.0.0.1:8095` — NOT `0.0.0.0:8095` or `*:8095`.
6. Open `http://127.0.0.1:8095/` in a browser. The page should render with the cockpit palette and show the existing transcript.
7. In the browser, type `hello from web` in the input + submit. Confirm:
   - The cockpit (terminal 1) shows the line in the transcript.
   - The watch sidecar (terminal 2) shows the line.
   - The browser SSE stream pushes the line back without a refresh.
   - Both tmux panes show `[CASSIUS] hello from web` exactly once.
   - `~/.ccoral/rooms/<id>/transcript.jsonl` has one new record.
8. Exit the cockpit. The serve + watch sidecars should detect the room is gone (Phase 11's `state=stopped` poll surfaces this; the sidecars should either exit cleanly or show a `(stopped)` badge).

**Pass criteria:**

- `room watch` tails new lines within ~250ms.
- `room serve` binds 127.0.0.1 ONLY (`ss -tlnp` confirms).
- Browser POST /say → cockpit + watch + transcript + both panes, each exactly once.
- SSE stream pushes new records to the browser without manual refresh.

---

## Sequence F — Multi-room TUI (Phase 11)

**Goal:** prove `ccoral rooms` discovers live rooms, switches tabs, routes input per room, supports unified mode, and handles room death.

1. Spin three rooms in three separate terminals on distinct ports:
   ```bash
   # Terminal 1
   ccoral room blank leguin "room one talk" --port 9100
   # Terminal 2
   ccoral room blank red "room two talk" --port 9200
   # Terminal 3
   ccoral room blank haiku "room three talk" --port 9300
   ```
2. In a fourth terminal, launch the multi-room cockpit:
   ```bash
   ccoral rooms
   ```
   It should discover all three rooms and present them as tabs.
3. Test tab cycling — `Ctrl+N` should advance to the next tab, `Ctrl+P` to the previous.
4. Switch to tab 2 (room two) and type `hello` + Enter. Confirm:
   - ONLY room two's panes get the line (terminal 2 cockpit + tmux panes).
   - Rooms one and three are NOT touched.
5. Test unified mode — press `Ctrl+U`. The view should switch from per-tab transcripts to a single chronologically-interleaved log; lines should be prefixed with `[<id>:<SPEAKER>]` so the operator can tell which room each came from.
6. Press `Ctrl+U` again to return to tabbed mode.
7. Test the slash command — `/room <id1> world` + Enter, where `<id1>` is room one's id. Confirm only room one's panes get `world`.
8. Test room death detection — kill room two:
   ```bash
   # In terminal 2, exit the cockpit (Ctrl+C)
   ```
   Within ~2s, the multi-room cockpit's tab 2 should show a `(stopped)` badge in the title.
9. Quit the multi-room cockpit (`Ctrl+C` or `/quit`).
10. Confirm the remaining live rooms are still healthy:
    ```bash
    ccoral room ls
    ```
    Should show rooms one and three as `state: live` and room two as `state: stopped`.

**Pass criteria:**

- All three rooms discovered by `ccoral rooms`.
- `Ctrl+N` / `Ctrl+P` cycle tabs.
- Per-tab input routes only to the focused room.
- `Ctrl+U` toggles unified mode; lines prefixed with `[<id>:<SPEAKER>]`.
- `/room <id> <text>` routes by id regardless of focused tab.
- Room death surfaces as `(stopped)` badge within ~2s of exit.
- Multi-room cockpit exit does NOT kill the remaining live rooms.

---

## After all sequences

```bash
# Confirm the suite is still green after live validation:
cd /home/jcardibo/projects/ccoral
python3 -m pytest tests/ -q
bash tests/anti_patterns_room.sh

# Review live-session logs for surprises:
tail -200 ~/.ccoral/logs/proxy-*.log | grep -iE "(REFUSAL|REWRITE_TERMINAL|RESET_TURN|ERROR)"
```

If any sequence fails: capture the failing terminal output + the relevant `~/.ccoral/rooms/<id>/transcript.jsonl` and `meta.yaml`, root-cause, fix in a focused commit, then re-run the affected sequence.

---

## Anti-patterns to NOT skip

- "It compiles, ship it." Static validation has surprised the room overhaul once already (Phase 9 legacy-cockpit fallback bug — UnboundLocalError after refactor; surfaced 2026-05-10). Live validation catches what the unit tests can't.
- Validating only with `blank`. At least Sequence A and Sequence D should be repeated with `leguin` or `red` to confirm none of the new code paths are blank-specific.
- Skipping Sequence E's `ss -tlnp` check. The loopback-bind guarantee is the only thing standing between `ccoral room serve` and accidental LAN exposure.
