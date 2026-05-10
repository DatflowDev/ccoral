# CCORAL Room — UX Overhaul Plan (consolidated)

**Status:** PLAN — Phases 1 and 2 shipped; Phases 3–12 remaining.
**Authors:** eni-executor (v1, 2026-05-09) + orchestrator (v2 iteration, 2026-05-10), merged 2026-05-10.
**Supersedes:** `.plan/room-overhaul-v2.md` (folded in here).
**Framework decision:** **Textual** (`textual>=0.80`). See Phase 0b. Locks in via Phase 9 retrofit before any new TUI surface (Phases 10, 11) lands.
**Scope:** `room.py`, `server.py`, `profiles.py` (read-only), `ccoral` CLI; rewrite of `room_control.py` → `room_app.py`; new modules `room_picker.py`, `rooms_cockpit.py`; tests under `tests/`.
**Goal:** Turn `ccoral room` from a "watch-and-pray" relay into a real cockpit — live human input, clean turn discipline, safe injection, configurable surface, per-room state, sticky persona identity, profile picker, and a multi-room TUI.

---

## What already shipped (do not re-execute)

| Phase | Commits | Notes |
|---|---|---|
| **Phase 1** — Cockpit & Live Human Input | `c09eae1` cockpit module + dispatcher · `85e079a` split TUI · `60073ad` turn-aware queue · `909f84d` polish (SIGTERM restore, /to case-fold, less re-entry) | Bespoke `room_control.py` (572 LoC, alt-screen ANSI + cbreak). Phase 9 below rewrites this on Textual. |
| **Phase 2** — Turn Discipline & Leak-Free Relay | `6da18b8` structured turn record (FIFO + JSONL) · `bf361c4` drop `Read /tmp` leak + tmux paste-buffer relay · `b67069d` arbiter + backpressure · `1244a98` `kind` marker on transcript records · `64b8d1e` deny filler turns · `8506a0e` structured header-line envelope | Phase 8 (persona stickiness) extends the turn record with `profile` + `slot`. |

`room.py` currently has uncommitted modifications (~45+/-7 lines) — orchestrator should not touch them; they are LO's in-flight work and will be reconciled before Phase 8 dispatch.

---

## Execution order (dependency-respecting)

```
[shipped: Phase 1, Phase 2]
  ↓
Phase 8  — Persona stickiness               (correctness; ship next)
  ↓
Phase 9  — Textual retrofit of cockpit      (locks framework into the codebase)
  ↓
Phase 3  — Configurable surface             (CLI flags, per-room state dir)
  ↓
Phase 4  — Injection discipline             (room_addendum field)
  ↓
Phase 10 — Profile picker (Textual)
  ↓
Phase 5  — Resume & archive UX
  ↓
Phase 6  — Single-room sidecar (`ccoral room watch|serve`)   ⟵ `watch` becomes alias to `ccoral rooms <id>` after Phase 11
  ↓
Phase 11 — Multi-room TUI (`ccoral rooms`)  (hard prereqs: Phases 3 + 6)
  ↓
Phase 7 + Phase 12 — Verification (consolidated)
```

Phases 3, 4, 10 may parallelize after Phase 9. Phase 11 must wait on 3 + 6.

---

## Phase 0 — Original Discovery (orientation only, no code)

Read-only context the v1 plan captured. Kept verbatim because later phases cite back into it.

### Files in scope

| File | Role | Touched in |
|---|---|---|
| `room.py` (now ~805 lines) | tmux + relay orchestrator + archive + export | Phases 3, 4, 5, 8, 9 |
| `server.py:_emit_turn_record` (server.py:176) | Stream-end capture; sink precedence at server.py:76–78 | Phase 8 (add `profile` + `slot`) |
| `room_control.py` (572 lines) | Bespoke split-TUI from Phase 1 | Phase 9 (rewrite as Textual `room_app.py`; legacy kept one release as `room_control_legacy.py`) |
| `profiles.py` | `list_profiles()` (profiles.py:27), `load_profile(name)` (profiles.py:53) | Phase 4, Phase 10 (read-only consumer) |
| `PROFILE_SCHEMA.md` | Documents `inject`, `preserve`, `minimal`, `apply_to_subagents`, `refusal_policy`, `reset_turn_framing` | Phase 4 — add `room_addendum` |
| `INJECT-FRAMING.md` | Permissive-framing rules (operator-scope) | Phase 4 — addendum follows these rules |
| `ccoral` CLI (491 lines) | `cmd_room` at ccoral:331; `cmd_profiles` at ccoral:213 | Phase 3, 5, 10, 11 |
| `requirements.txt` (3 lines: aiohttp, pyyaml, certifi) | Deps | Phase 9 — add `textual>=0.80` |
| `tests/` | Parser + lane smoke tests | Phase 7 + 12 — add room integration suite |

### Anti-patterns to avoid (named, with reason)

- **Do not append** room instructions to `inject` as raw English (Phase 4 fixes via `room_addendum`).
- **Do not** instruct the model to `Read /tmp/ccoral-room/from_<x>.txt` — Phase 2 deleted this; do not regress.
- **Do not** mtime-poll for "speaker is done" — proxy already emits `stop_reason` (Phase 2).
- **Do not** filter export artifacts by string-matching profile names (Phase 2 replaced with `kind` marker).
- **Do not key the relay map on profile name.** `panes[profile_name]` breaks for `room blank blank`. Phase 8 switches to slot-keyed mapping.
- **Do not infer speaker identity from "which file changed".** Phase 8 stamps `profile` + `slot` on every turn record.
- **Do not run two TUI styles in production.** Phase 9 retrofits Phase 1's cockpit to Textual *before* Phases 10–11 ship.
- **Do not block the Textual event loop.** All FIFO/JSONL tailing under `@work(thread=True)`; widgets updated via `App.call_from_thread(...)` or `Message`.
- **Do not have the multi-room TUI spawn rooms.** Launch is the picker's job (Phase 10) or `ccoral room <p1> <p2>` (legacy positional). The cockpit is observer + interjector only.

---

## Phase 0b — Iteration Discovery & Framework Decision (no code)

### Bugs surfaced after Phase 1 ship

1. **Personas sometimes switch between screens.** Profile A's voice appears where profile B should. Sometimes, not always — race or duplicate-name collision. Phase 8 fixes structurally.
2. **Need a TUI launcher.** Typing `ccoral room <p1> <p2> [topic]` is a chore. Phase 10 adds picker.
3. **Multi-room support in the TUI.** When more than one room is running, jump between them or watch them unified. Phase 11 adds `ccoral rooms`.

### Framework decision: Textual

Hand-rolled `room_control.py` (572 LoC) was a defensible Phase 1 choice for a single split. The next iteration adds: a multi-column picker, multi-tab cockpit with per-tab scroll preservation, activity badges, unified-mode chronological interleaving, window resize, and async multiplexing of N FIFOs + stdin. That is firmly past the value horizon of bespoke ANSI.

**Chosen:** [Textual](https://textual.textualize.io/) (Context7 ID `/websites/textual_textualize_io`). Reasons:
- All the layout, focus, scroll, resize, and key-chord work we'd otherwise rebuild is already in the framework.
- Async-first design fits the multi-FIFO + stdin multiplexing of Phase 11 naturally (`@work` decorator for background workers, `App.call_from_thread` for safe widget updates).
- Built-in `App.run_test()` + `Pilot` test driver makes Phase 12's TUI tests tractable (vs. simulating cbreak bytes through a pty).
- High Source Reputation, 3144 doc snippets, widely deployed.

**API surface we'll use** (all confirmed via Context7 query against `/websites/textual_textualize_io`):
- `from textual.app import App, ComposeResult` — App lifecycle, `compose()`, `BINDINGS`, `action_*` methods.
- `from textual.widgets import TabbedContent, TabPane, RichLog, Input, Footer, Header, OptionList, Label, Static`.
- `from textual.containers import Horizontal, Vertical, Container`.
- `from textual.binding import Binding` (e.g., `Binding("ctrl+n", "next_tab", "Next room")`).
- `from textual.message import Message`.
- `from textual import on, work` — `@on(Input.Submitted)` event handlers, `@work(thread=True)` background workers.
- `App.query_one(...)`, `App.notify(...)`, `App.exit(...)`, `App.call_from_thread(...)`, `App.suspend()` for pager re-entry.
- `RichLog(max_lines=10000, wrap=True, markup=True, auto_scroll=True)` with `.write(...)` / `.write_lines(...)`.
- `Input.Submitted` message (`event.value`, `event.input.clear()`).
- For tests: `from textual.pilot import Pilot` and `App.run_test()`.

**Cost:**
- New dep: `textual>=0.80`. Pulls Rich, markdown-it-py, linkify-it-py. ~10–15 MB installed; no native build steps; permissive license (MIT).
- One-time port of `room_control.py` (Phase 9). Net LoC after port: estimated ~250–300 (down from 572).

**Why not the alternatives:**
- Stay stdlib: 400+ LoC of bespoke event-loop work for Phase 11 alone, every verification item becomes a hand-implemented edge case.
- prompt_toolkit: more flexible but requires more glue code for tabs/scroll; less batteries-included for our exact use case.
- rich + bespoke: ~200 LoC of glue and we still own the event loop, SIGWINCH, and per-tab scroll cursor — half the savings, none of the test ergonomics.

### Ground truth on the persona-switch bug

Two findings combined explain "personas sometimes switch":

1. **Duplicate-profile path collision (deterministic).** room.py builds the per-proxy response sink path from `base_name` only. When the user runs `ccoral room blank blank`, both proxies receive the SAME `CCORAL_RESPONSE_FILE` path. Both write to it. Last writer wins; relay loop sees one record and attributes it to whichever slot's mtime is fresher.
2. **Speaker identity is inferred, not carried (probabilistic).** `server.py:_emit_turn_record` (server.py:176) writes `{ts, model, stop_reason, text, lane, request_id}` — *no* `profile` field. Orchestrator infers speaker from "which sink path changed". Even with distinct paths, near-simultaneous turn-ends can re-attribute under poll-and-settle.

The fix is structural: the proxy MUST stamp every captured turn with `profile` (from `CCORAL_PROFILE`, already loaded at server.py:65) and the orchestrator MUST key on `(slot, profile)` from the record, not on file paths or mtime ordering. **This is Phase 8.**

---

## Phase 8 — Persona Stickiness (correctness, ship next)

**Why first remaining:** A correctness bug, not UX. Every other UX win inherits the same wrong-attribution risk if we don't fix the foundation. Phase 10's "same-profile-twice" warning and Phase 11's per-room input routing both depend on slot identity being trustworthy.

### What to implement

1. **Server-side: stamp every turn record with `profile` and `slot`.**
   - `server.py:_emit_turn_record` at server.py:176 currently writes `{ts, model, stop_reason, text, lane, request_id}`. Add:
     - `profile`: from `PROFILE_OVERRIDE` (already loaded at server.py:65).
     - `slot`: from a new env var `CCORAL_ROOM_SLOT` (`"1"` or `"2"`), defaulting to `null` for non-room invocations.
2. **Per-proxy unique sink path (collision fix).** Change `f"{base_name}_response.txt"` (room.py:141 region) to `f"slot{slot}_{base_name}.jsonl"` (or FIFO equivalent). Slot prefix guarantees uniqueness.
3. **Relay loop: switch to slot-keyed mapping.**
   - Pane mapping (`panes` at room.py:335–338) and color map (room.py:343–346): replace name-keyed dicts with `panes = {1: ..., 2: ...}` and `slot_meta = {1: {"profile": p1, "color": Y, "display": ...}, 2: {...}}`.
   - When a turn record arrives, read `record["slot"]` directly. Use it to pick destination pane (`panes[3 - slot]`) and speaker color/display (`slot_meta[slot]`).
   - Delete the "speaker = whichever file changed" code path at room.py:546–619.
4. **Cockpit display when profiles collide.** Promote `room_control.set_user(name)` (room_control.py:116) to `set_speaker_display(slot, name)`. Default display when no overrides: `BLANK#1` / `BLANK#2`. Distinct profiles: `BLANK` / `LEGUIN`.
5. **Backwards-compat shim.** If a turn record arrives without `slot`/`profile`, fall back to legacy inference and log one yellow `WARN: legacy turn record` line. Phase 12 verifies the shim is unreachable in practice.
6. **Test fixture.** `tests/test_room_persona_sticky.py` — synthetic JSONL streams emulating slot 1 + slot 2 emissions including near-simultaneous and out-of-order timestamps. Asserts every message attributes to the correct slot, every time. Includes a duplicate-profile case (`profile1 == profile2 == "blank"`).

### Documentation references

- Capture sink current shape: `server.py:_emit_turn_record` at line 176; sink-precedence at server.py:76–78.
- Profile env var precedent: `PROFILE_OVERRIDE = os.environ.get("CCORAL_PROFILE")` at server.py:65.
- Proxy spawn: `start_proxies` at room.py:123 — env dict construction.
- Pane mapping: room.py:335–338, room.py:343–346.
- Settle block to delete: room.py:546–619.

### Verification checklist

- [ ] `ccoral room blank blank "say A, then B"` over 10 runs: every transcript attributes turns correctly to slot 1 vs slot 2.
- [ ] `tail -f ~/.ccoral/rooms/<id>/transcript.jsonl` shows `profile` and `slot` on every line.
- [ ] `tests/test_room_persona_sticky.py` passes; specifically the duplicate-profile case.
- [ ] `--user-1 LO --user-2 OPS` flags surface as `LO:` / `OPS:` in cockpit and `[LO]`/`[OPS]` in tmux paste prefix.
- [ ] Without overrides + same profile: `<NAME>#1` / `<NAME>#2` shown.

### Anti-pattern guards

- `grep -nE "panes\[(profile1|profile2|name)\]" room.py` → 0.
- `grep -nE "f\"\\{base_name\\}_response" room.py` → 0.
- `grep -nE "if .*mtime.*>.*last_mtime" room.py` → 0.

---

## Phase 9 — Textual Foundation: retrofit Phase 1 cockpit

**Why second remaining:** Lock the framework choice into the code before adding any new TUI surface. Eliminates the risk of shipping two TUI styles. Provides the shared App base / CSS for Phases 10 and 11 to extend.

### What to implement

1. **Add dependency.** Append `textual>=0.80` to `requirements.txt`. Run `pip install -r requirements.txt`. Verify `python -c "import textual; print(textual.__version__)"` succeeds.

2. **New module `room_app.py`** (~200 LoC) — Textual `App` subclass replacing the bespoke split-screen. Skeleton:
   ```python
   from textual.app import App, ComposeResult
   from textual.binding import Binding
   from textual.containers import Vertical
   from textual.widgets import Footer, Header, Input, RichLog
   from textual import on, work

   class RoomApp(App):
       """Single-room cockpit (replaces room_control.py split-screen)."""

       CSS_PATH = "room_app.tcss"
       BINDINGS = [
           Binding("ctrl+c", "quit", "Quit", priority=True),
           Binding("ctrl+d", "end_after_turn", "End after turn"),
           Binding("ctrl+s", "save_now", "Save now"),
           Binding("ctrl+t", "transcript", "Pager"),
           Binding("?", "help", "Help"),
       ]

       def compose(self) -> ComposeResult:
           with Vertical():
               yield RichLog(id="transcript", wrap=True, markup=True,
                             max_lines=10000, auto_scroll=True)
               yield Input(id="prompt", placeholder="message or /help")
           yield Footer()

       @on(Input.Submitted, "#prompt")
       def on_prompt_submit(self, event: Input.Submitted) -> None:
           event.input.clear()
           self.dispatch_command(event.value)

       @work(thread=True, exclusive=False)
       def tail_relay_records(self, sink_path: str) -> None:
           """Read JSONL/FIFO turn records; post to RichLog via call_from_thread."""
           ...
   ```
   Real APIs cited: `App` / `BINDINGS` (Textual tutorial), `RichLog(max_lines, wrap, markup, auto_scroll)` (rich_log docs), `@on(Input.Submitted)` (anatomy-of-a-textual-user-interface blog), `@work(thread=True)` (Workers guide).

3. **External CSS in `room_app.tcss`** — colors mirror current ANSI palette (Y, C, W, DIM):
   ```css
   #transcript { height: 1fr; border: round $primary; }
   #prompt { dock: bottom; height: 3; border: round $accent; }
   .speaker-1 { color: yellow; }
   .speaker-2 { color: cyan; }
   .system { color: $text-muted; }
   .warn { color: orange; }
   ```

4. **Slash commands** — port the existing parser (`room_control.py:_parse_command` at line 423) verbatim into `RoomApp.dispatch_command`. Set unchanged: `/pause`, `/resume`, `/stop`, `/end`, `/save`, `/transcript`, `/help`, `/to <profile> <text>`. `Ctrl+C` mapped to action; `Ctrl+D` to `end_after_turn`. Plain text is `("say", text)`.

5. **Turn-aware queue** stays — it's logic, not TUI. Move from `room_control.py` to `room_app.py` as `RoomApp.event_queue: list[tuple]`. The relay loop drains it via a worker that posts `EventReady` messages back to the App.

6. **room.py call site change.** Replace the bespoke `room_control.split_screen()` context manager (room.py:351 region) with:
   ```python
   from room_app import RoomApp
   app = RoomApp(slot_meta=slot_meta, sink_paths=sink_paths, ...)
   exit_code = app.run()
   ```
   `RoomApp.on_unmount` triggers the same archive-write that the old teardown did.

7. **Delete after retrofit verified.** `room_control.py` moves to `room_control_legacy.py` for one release as a `--legacy-cockpit` fallback flag. The legacy module is removed in Phase 12 if no regressions reported.

8. **Pager / less re-entry.** Textual offers `App.suspend()` (context manager) for shelling out to `less` — replaces the manual `tcsetattr` save/restore in v1's pager-fix commit (`909f84d`).

### Documentation references

- TabbedContent + Footer + BINDINGS pattern: `https://textual.textualize.io/widgets/tabbed_content`.
- RichLog API: `https://textual.textualize.io/widgets/rich_log`.
- `Input.Submitted` handler pattern: `https://textual.textualize.io/blog/2024/09/15/anatomy-of-a-textual-user-interface`.
- App boilerplate: `https://textual.textualize.io/tutorial`.
- Background workers: `from textual import work` — `@work(thread=True)` decorator, `App.call_from_thread` for safe widget mutation.
- `App.suspend()` for pager re-entry: `https://textual.textualize.io/api/app/#textual.app.App.suspend`.

### Verification checklist

- [ ] `pip install -r requirements.txt` resolves; `import textual` works.
- [ ] `ccoral room blank blank "test"` opens the new cockpit with split rendering identical (or visually clearer) to v1 Phase 1.
- [ ] All Phase 1 verification items still pass (typing into prompt → ONE pane line; `/to <profile>` routes correctly; `/pause` / `/resume` / `/end` work; no truncation).
- [ ] `Ctrl+C` exits cleanly; terminal restored (alt-screen exit, no garbage).
- [ ] `/transcript` shells out to `less` via `App.suspend()` and re-enters the app cleanly on `q` (no terminal corruption).
- [ ] Window resize during a session reflows transcript wrap (no manual SIGWINCH code needed).
- [ ] `ccoral room --legacy-cockpit blank blank "test"` falls back to old `room_control.py` (one-release safety net).

### Anti-pattern guards

- `grep -nE "termios\\.|tty\\.setcbreak|select\\.select.*sys.stdin" room_app.py` → 0 (Textual owns the terminal).
- `grep -nE "\\\\033\\[" room_app.py` → 0 (no raw ANSI; use Rich markup or CSS).
- `grep -nE "while True.*time\\.sleep" room_app.py` → 0 (use `@work` + `set_interval` if needed).
- `grep -n "from room_control import" room.py` → only allowed import is `room_control_legacy` under the `--legacy-cockpit` branch.

---

## Phase 3 — Configurable Surface

**Why now:** Phases 1+2 introduced real new behavior (input, arbiter, backpressure) that needs knobs. Hardcoded constants are fine in a prototype; not in something LO uses daily across multiple rooms. Phase 11 also needs per-room state dirs to discover live rooms.

### What to implement

1. **CLI flags on `ccoral room`:**
   - `--user <name>` (default `CASSIUS`) — replaces the module constant.
   - `--port <n>` (default `8090`) — base port; ports `n` and `n+1` are used. Allows two rooms running side-by-side.
   - `--turn-limit <n>` — auto-stop after N exchanged turns; default unlimited.
   - `--max-chars-per-turn <n>` — soft cap; arbiter inserts `[SYSTEM] truncate to N chars` before relay if exceeded.
   - `--backpressure-turns <n>` (default 2), `--backpressure-timeout <s>` (default 60).
   - `--seed1 "<text>"` and `--seed2 "<text>"` — asymmetric kickoff. Replaces today's "topic only goes to p1" behavior. If only `--seed1` is given, p2 starts cold (current behavior preserved). The legacy positional `topic` argument maps to `--seed1`.
   - `--moderator <profile>` (optional) — third pane that speaks every K turns or on `/mod` command. K configurable via `--moderator-cadence <K>` (default 4).
2. **Per-room state directory.** `~/.ccoral/rooms/<id>/` where `<id>` is `<timestamp>_<p1>-<p2>` matching the existing archive scheme. Inside:
   - `config.yaml` — all CLI flags resolved at start (so `--resume` can re-hydrate without re-typing).
   - `transcript.jsonl` — append-only per-turn record (replaces single `messages.json` write at exit).
   - `meta.yaml` — ccoral version, model ids per profile, profile shas, port assignments, exit reason, **`state: live|stopped`** flag (Phase 11 watches this).
3. **`ccoral room ls`** — list all `~/.ccoral/rooms/*/` with id, profiles, started, turn count, last topic, live/stopped state.
4. **`ccoral room show <id>`** — page the transcript in `$PAGER` (default `less -R`) with color preserved.

### Documentation references

- Current constants block: `room.py:42-50`. Move to a `RoomConfig` dataclass populated from CLI args.
- Current archive format: `room.py:236-254` (`save_conversation`). Replace exit-time JSON write with per-turn JSONL append; `meta.yaml` written once at start, updated once at exit.
- CLI dispatcher: `ccoral:331-389` (`cmd_room`). Add the new flags using the same hand-rolled arg-walker pattern already in the function (do not introduce `argparse` mid-codebase — house style is manual).

### Verification checklist

- [ ] `ccoral room blank blank --port 9100 --user LO` runs without colliding with a default-port room running concurrently on 8090/8091.
- [ ] `ccoral room blank blank --turn-limit 4` exits cleanly after the 4th turn with exit reason `turn_limit` in `meta.yaml`.
- [ ] `ccoral room blank blank --seed1 "you start" --seed2 "you start too"` puts both seeds into the relevant pane at startup; transcript shows both.
- [ ] `ccoral room ls` shows all rooms in `~/.ccoral/rooms/` with live/stopped state.
- [ ] `ccoral room show last` opens the most recent transcript in pager with color.

### Anti-pattern guards

- `grep -nE "USER_NAME ?= ?\"CASSIUS\"|BASE_PORT ?= ?8090|TMUX_SESSION ?= ?\"room\"" room.py` → 0; all three live on `RoomConfig` now.
- `grep -n "argparse" room.py ccoral` → must remain 0 (house style preserved).

---

## Phase 4 — Injection Discipline

**Why:** The current room mode appends a hardcoded English instruction block to every profile's `inject` (`room.py:75-90`). For high-personality profiles (eni, eni-supervisor, eni-executor, camus, hand, leguin) this collides with carefully-tuned identity work and produces meta-confused replies.

### What to implement

1. **New optional profile field `room_addendum`** documented in `PROFILE_SCHEMA.md`:
   ```yaml
   room_addendum: |
     # Profile-authored note about being in a room.
     # If absent, ccoral uses the default neutral addendum.
     # If set to the empty string, NO addendum is added (profile is responsible for room awareness in its own inject).
   ```
2. **Default addendum** — shorter, identity-neutral, framed per INJECT-FRAMING.md operator-scope rules. Reference the framing doc inline so future audits catch drift:
   ```
   ## Room context (operator-set)
   You're in a live exchange with {OTHER} (another assistant). {USER} is the human host.
   Lines starting with "[{OTHER}]" are them. Lines starting with "[{USER}]" are the host.
   Reply naturally and stay in your own voice. The host may interject at any time.
   ```
   No `Read /tmp/...` instruction (Phase 2 removed it). No "don't use tools" / "don't write files" — those are tool-scope decisions, not voice instructions, and belong to a separate room-mode tool-strip flag (already supported via `preserve` / `strip_tools`).
3. **Address `minimal: true` profiles correctly.** Today the room block is appended verbatim regardless. New behavior: if `minimal: true`, the addendum still goes in `inject` (because `minimal` strips most of the original system, leaving inject as the only voice anchor) — but no other section is touched. Also: if profile sets `room_addendum: ""` explicitly, respect that even on minimal.
4. **Keep `apply_to_subagents` and `refusal_policy` honored end-to-end** — the temp `<profile>-room.yaml` should copy these forward (today only `preserve`, `inject`, `minimal` are copied — `room.py:94-101`). Add: `apply_to_subagents`, `refusal_policy`, `reset_turn_framing`, `strip_tools`, `strip_tool_descriptions`. Anything else schema-defined should pass through too (use `dict(base)` minus the keys we override).
5. **Profile audit pass (no new code, just a checked-in note):** in each of the 18 bundled profiles, decide whether to keep the default addendum, set a custom one, or set `room_addendum: ""`. Write findings to `.plan/room-addendum-audit.md` so the decision is explicit and resumable. Do not modify profile files in this phase — that is per-profile work for a follow-up.

### Documentation references

- Current append: `room.py:75-105`. Replace string concat with `addendum = profile.get("room_addendum", DEFAULT_ROOM_ADDENDUM); modified_inject = base.get("inject", "") + (("\n\n" + addendum) if addendum else "")`.
- INJECT-FRAMING.md (S1404, inj-7218..7230) — operator-scope framing rules.
- Current preserved-fields whitelist: `room.py:94-101`. Expand.

### Verification checklist

- [ ] `ccoral room eni-supervisor eni-executor "test"` produces a transcript where neither profile breaks character within the first two turns (manual review; baseline is the saved 2026-05-09_175407 archive which already shows clean voice — must not regress).
- [ ] `ccoral room blank blank "test"` works with default addendum (no profile-side `room_addendum` set).
- [ ] A profile with `room_addendum: ""` produces a temp profile whose `inject` equals the base `inject` exactly (no trailing whitespace or block).
- [ ] A profile with custom `room_addendum: "..."` uses it instead of the default.
- [ ] `apply_to_subagents` and `refusal_policy` from the base profile are present in `~/.ccoral/profiles/<name>-room.yaml`.

### Anti-pattern guards

- `grep -n "## CONVERSATION ROOM" room.py` → 0 (the hardcoded block is gone; addendum source is the constant `DEFAULT_ROOM_ADDENDUM`).
- `grep -n "don't use tools\|don't write files\|don't use markdown" room.py` → 0 (tool-scope instructions don't belong in voice).

---

## Phase 10 — Profile Picker (Textual)

**Why now:** Smallest UX win, fully reuses Phase 9's Textual base + CSS. Surfaces Phase 8's same-profile-twice case as a first-class confirm prompt. Independent of Phase 11; can ship right after Phase 9.

### What to implement

1. **New module `room_picker.py`** (~150 LoC). Textual `Screen` subclass that the existing `RoomApp` (or a thin `LauncherApp`) pushes when launched with no positional profiles.

2. **Layout via `Horizontal` + two `OptionList`s + a description footer:**
   ```python
   from textual.app import App, ComposeResult
   from textual.binding import Binding
   from textual.containers import Horizontal, Vertical
   from textual.screen import Screen
   from textual.widgets import Footer, Header, Input, Label, OptionList
   from textual.widgets.option_list import Option

   class PickerScreen(Screen):
       BINDINGS = [
           Binding("tab", "next_column", "Next column", show=True),
           Binding("shift+tab", "prev_column", "Prev column"),
           Binding("/", "filter", "Filter"),
           Binding("enter", "confirm", "Launch"),
           Binding("ctrl+c,q,escape", "quit", "Quit"),
           Binding("?", "help", "Help"),
       ]

       def compose(self) -> ComposeResult:
           yield Header(name="ccoral room — pick two profiles")
           with Horizontal():
               yield OptionList(*self._profile_options(), id="col1")
               yield OptionList(*self._profile_options(), id="col2")
           yield Label("", id="description")
           yield Input(id="filter", placeholder="/ to filter", classes="hidden")
           yield Footer()
   ```
   Real APIs cited: `Screen`, `Horizontal`, `OptionList` + `Option`, `Header`, `Footer`, `Binding(show=True)` from Textual widget docs.

3. **Data source:** `profiles.list_profiles()` (profiles.py:27) returns `[{name, description, path}]`. Sort alphabetically. Each `Option` carries the profile name as its `id`; description is rendered in the focused-row callback by listening to `OptionList.OptionHighlighted`.

4. **Filter mode.** `/` shows the hidden `Input#filter`; on each keystroke, both `OptionList` widgets are re-populated with matching entries (`OptionList.clear_options()` + `add_options(...)`). `Esc` clears filter and re-focuses the column.

5. **Same-profile guard.** On `Enter`:
   - If both columns selected the same profile name, push a modal `ConfirmScreen` (Textual's `ModalScreen` subclass) with `"Run two instances of <name>? [y/N]"`. `y` resolves with `(name, name)`; `n`/`Esc` returns to picker.
   - Else resolve with `(p1, p2)`.

6. **Optional topic step.** After confirmation, push a one-shot `Input` modal: `"Topic (optional, Enter to skip):"`. Resolved value passed as `topic` to `run_room`.

7. **CLI hook.** `cmd_room(args)` at ccoral:331:
   - 0 positional profiles → launch picker.
   - 1 positional that doesn't match a known profile → treat as topic, launch picker.
   - 2 positional that match profiles → bypass picker (current behavior preserved).
   - `--no-picker` → fail loudly with "missing profiles" instead of opening picker (script-friendly).

### Documentation references

- `OptionList` widget: `https://textual.textualize.io/widgets/option_list`.
- `ModalScreen` for confirm prompts: `https://textual.textualize.io/api/screen/#textual.screen.ModalScreen`.
- `Screen.dismiss(value)` for returning a result to the pushing screen: `App.push_screen(screen, callback)` pattern.
- `Binding(show=True)` makes the binding appear in the Footer: tutorial.

### Verification checklist

- [ ] `ccoral room` (no args) opens the picker; lists every profile from `list_profiles()`.
- [ ] Arrow keys move within the focused column; `Tab`/`Shift+Tab` switch columns; description label updates as focus moves.
- [ ] `/blank` filter narrows both columns.
- [ ] Enter on two distinct profiles launches `run_room`.
- [ ] Enter on same profile twice opens confirm modal; `y` proceeds with slot-suffixed display names; `n` returns to picker.
- [ ] `q`, `Esc`, `Ctrl+C` all exit cleanly with terminal restored.
- [ ] `ccoral room blank leguin "topic"` bypasses picker.
- [ ] `ccoral room --no-picker` with missing args fails with clear error.

### Anti-pattern guards

- `grep -nE "import (curses|prompt_toolkit|blessed|urwid)" room_picker.py` → 0.
- `grep -n "input(" room_picker.py` → 0 (Textual `Input` widget only).
- `grep -nE "\\\\033\\[" room_picker.py` → 0 (CSS / Rich markup only).
- `grep -nE "subprocess.*tmux" room_picker.py` → 0 (picker doesn't touch tmux; that's `run_room`).

---

## Phase 5 — Resume & Archive UX

**Why:** Today resume drops a single chat-message context dump into both panes — the models then "respond" to the dump, breaking continuity. We have transcripts to prove it.

### What to implement

1. **Resume-as-system-note, not chat-message.** When `--resume <id>` is used, the orchestrator regenerates each temp profile's `inject` with the prior conversation as a clearly-labeled system block at the END (operator-scope, per INJECT-FRAMING.md):
   ```
   ## Prior exchange (resumed by host)
   The conversation below already happened. The host is resuming you.
   Continue naturally from where you left off — do not re-introduce or recap.

   [ENI-EXECUTOR] ...
   [ENI-SUPERVISOR] ...
   ...
   ```
   No first-message dump in either pane. The first new turn is the host's seed (or, if no seed, the first speaker just continues).
2. **Configurable resume window.** `--resume-tail <n>` (default 30) — last N turns included in the system note. Today's hardcoded `prior_messages[-10:]` (`room.py:339`) is too short for substantive continuation.
3. **`ccoral room export`** improvements: `--from <id> --to <out.md>` is already supported via `--output`; add `--format md|jsonl|html` (md is current; jsonl is straight stream of records; html is a single-file standalone with the same color palette). The Phase 2 `kind` marker replaces any string-match filter.
4. **`ccoral room delete <id>`** and **`ccoral room rename <id> <new-id>`** — basic archive hygiene. Delete asks for confirmation unless `--yes`.

### Documentation references

- Current resume: `room.py:336-343`. Replace.
- Current archive: `room.py:236-254`. Already moved to per-turn JSONL in Phase 3; this phase adds the resume-side reader and the export-format switch.

### Verification checklist

- [ ] Run a 5-turn room, exit, then `--resume last "what did you say last?"` — both profiles answer in continuation, neither says "Hi, let me catch up" or quotes the previous transcript verbatim.
- [ ] `ccoral room export last --format jsonl` produces one JSON line per turn.
- [ ] `ccoral room export last --format html` produces a single HTML file that renders correctly when opened with `xdg-open`.
- [ ] `ccoral room delete <id>` with no `--yes` prompts; with `--yes` deletes silently.
- [ ] `ccoral room rename <id> archived/cool-talk` moves the directory.

### Anti-pattern guards

- `grep -n "Continue the conversation from where you left off" room.py` → 0 (old chat-message resume gone).
- `grep -n "skip_phrases" room.py` → 0 (filter replaced by Phase 2's `kind` marker).

---

## Phase 6 — Single-room Sidecar (`watch` + `serve`)

**Why:** Even with Phase 9's Textual cockpit, sometimes you want to read the room from a different terminal, or hand a colleague a URL. `watch` is the per-room read-only view; `serve` is the loopback web view. Phase 11 subsumes `watch` for the multi-room case but `serve` remains the only HTTP option.

### What to implement

1. **`ccoral room watch <id|last>`** — opens a read-only color transcript that follows `transcript.jsonl` with `tail -f` semantics. After Phase 11, this is an alias for `ccoral rooms <id>` filtered to a single room. Plain TTY rendering, same Textual palette as the cockpit.
2. **`ccoral room serve <id|last> [--port 8095]`** — minimal aiohttp single-file webpage served at `http://127.0.0.1:<port>/` that:
   - Renders the transcript with profile colors.
   - SSE-streams new turns from `transcript.jsonl` as they land (re-using `aiohttp.web.StreamResponse` per `server.py:635-648` pattern — house style precedent).
   - Has a single text input that POSTs to `/say` → writes to a control FIFO that the orchestrator reads.
3. **Control FIFO contract.** `/tmp/ccoral-room/<id>.control` — JSON lines: `{"kind": "say", "text": ...}` and `{"kind": "inject", "target": ..., "text": ...}`. **Phase 11 reuses this contract.**
4. **Stretch (not required for Phase 6 sign-off):** `ccoral room serve --auth <token>` for over-LAN viewing. Default is loopback-only.

### Documentation references

- Streaming response surface: `server.py:635-648`. Reuse the same `StreamResponse` pattern.
- Color palette: Textual CSS from Phase 9.
- Phase 9's input dispatcher: `RoomApp.dispatch_command` consumes the same `("say", text)` tuples the FIFO produces.

### Verification checklist

- [ ] `ccoral room watch last` follows the transcript live; quitting with `q` exits cleanly.
- [ ] `ccoral room serve last` returns 200 on `/`; opening in a browser shows the transcript; new turns appear without a refresh.
- [ ] POSTing `{"text": "hi from browser"}` to `/say` produces a `[CASSIUS]` (or configured `--user`) entry in the live cockpit, the transcript, and both panes — exactly once each.
- [ ] Bind defaults to `127.0.0.1` only (verify with `ss -tlnp`).

### Anti-pattern guards

- `grep -nE "0\.0\.0\.0|host=\"\"|host=None" ccoral room_app.py room.py` → 0 (no public binds without explicit flag).

---

## Phase 11 — Multi-Room TUI: `ccoral rooms` (Textual)

**Why fourth remaining:** Largest piece. Hard prerequisites on Phases 3 + 6. Subsumes Phase 6's `ccoral room watch` (single-tab subset).

**Hard prerequisites (must merge first):**
- Phase 2 — structured per-turn JSONL/FIFO records. ✅ shipped.
- Phase 3 — per-room state dir `~/.ccoral/rooms/<id>/{transcript.jsonl, config.yaml, meta.yaml}` with `state: live|stopped` flag.
- Phase 6 — per-room control FIFO at `/tmp/ccoral-room/<id>.control` accepting `{"kind": "say"|"inject", ...}` JSON lines.

### What to implement

1. **New module `rooms_cockpit.py`** (~250 LoC) — Textual `App` for multi-room observation + interjection.
   ```python
   from textual.app import App, ComposeResult
   from textual.binding import Binding
   from textual.containers import Vertical
   from textual.widgets import (
       ContentSwitcher, Footer, Header, Input, RichLog, TabbedContent, TabPane,
   )
   from textual import on, work
   from textual.message import Message

   class RoomsCockpit(App):
       """Multi-room observer + interjector. Read-only on lifecycle."""

       CSS_PATH = "rooms_cockpit.tcss"
       BINDINGS = [
           Binding("ctrl+n", "next_tab", "Next room", priority=True),
           Binding("ctrl+p", "prev_tab", "Prev room", priority=True),
           Binding("ctrl+u", "toggle_unified", "Unified ↔ tabs"),
           Binding("ctrl+l", "clear_badges", "Clear badges"),
           Binding("ctrl+c,q", "quit", "Quit"),
       ]

       def compose(self) -> ComposeResult:
           yield Header(show_clock=True)
           with TabbedContent(id="tabs"):
               for room_id in self.discover_rooms():
                   with TabPane(self._tab_label(room_id), id=room_id):
                       yield RichLog(id=f"log-{room_id}", wrap=True,
                                     markup=True, max_lines=20000,
                                     auto_scroll=True)
           yield RichLog(id="unified-log", wrap=True, markup=True,
                         max_lines=50000, auto_scroll=True, classes="hidden")
           yield Input(id="prompt", placeholder="message  (/room <id> <text> in unified)")
           yield Footer()

       @work(thread=True, exclusive=False, group="tail")
       def tail_room(self, room_id: str, jsonl_path: str) -> None:
           """Tail one room's transcript.jsonl; post lines to the right RichLog."""
           ...
   ```
   Real APIs cited: `TabbedContent` + `TabPane` (full example in docs), `RichLog(max_lines, wrap, markup, auto_scroll)` (rich_log API), `@work(thread=True, group="tail")` for parallel tailers, `BINDINGS` with `priority=True` (Bindings guide), `ContentSwitcher` for tabs↔unified mode.

2. **Two view modes, toggled by `Ctrl+U`:**
   - **Tabs mode (default):** `TabbedContent` widget; `Ctrl+N`/`Ctrl+P` cycle active tab; tab labels carry `(stopped)` and `+` activity badges.
   - **Unified mode:** hide `TabbedContent`, show the single `unified-log` RichLog. Background workers post to both their per-room RichLog AND the unified RichLog.

3. **Per-tab scroll preservation.** Free with Textual — each `RichLog` keeps its own scroll position; switching tabs doesn't reset.

4. **Activity badges.** When a worker writes to a non-active tab's RichLog, it posts a custom `Message` (`class RoomActivity(Message): room_id: str`) to the App. The App handler updates the tab label to add `+`. `Ctrl+L` clears all badges.

5. **Input dispatch (`@on(Input.Submitted, "#prompt")`):**
   - Tabs mode: target = currently active room (`self.query_one(TabbedContent).active`). Plain text → write `{"kind": "say", "text": ...}` to `/tmp/ccoral-room/<active>.control`. `/to <profile> <text>` → `{"kind": "inject", "target": ..., "text": ...}`. `/room <id>` ignored in tabs mode (or jumps focus and submits).
   - Unified mode: plain text targets the most-recently-active room (track via worker writes). `/room <id> <text>` overrides explicitly.

6. **Lifecycle.** Cockpit is read-only on lifecycle. It does NOT spawn `run_room`. Quitting (`Ctrl+C`/`q`) leaves all rooms running. Verify with `ccoral room ls`.

7. **Stopped-room handling.** Worker detects `transcript.jsonl` EOF + `meta.yaml` shows `state: stopped` → posts `RoomStopped(room_id)` message → App updates tab label to `(stopped)`, dims color, removes after 30s grace. `r` while badge visible reopens (re-tails).

8. **Crash safety.** Each `@work` runs in its own thread; an exception in one tailer is caught and surfaced as `RoomBroken(room_id, error)` → tab label gets `× broken` red badge. Other tailers + main loop unaffected.

9. **Subsume Phase 6 watch.** `ccoral room watch <id>` becomes an alias for `ccoral rooms <id>` filtered to a single tab.

### Documentation references

- `TabbedContent` full example with `BINDINGS` and `action_show_tab(tab)`: `https://textual.textualize.io/widgets/tabbed_content`.
- `RichLog` API: `https://textual.textualize.io/widgets/rich_log`.
- `Input.Submitted` handler: `https://textual.textualize.io/blog/2024/09/15/anatomy-of-a-textual-user-interface`.
- Background workers: `from textual import work` — `@work(thread=True, group=...)` decorator, `App.call_from_thread`, `Worker` lifecycle.
- Custom `Message` between widgets: `from textual.message import Message`.
- `ContentSwitcher` for tabs↔unified toggle: `https://textual.textualize.io/widgets/content_switcher`.

### Verification checklist

- [ ] Spin three rooms in three terminals on ports 9100/9200/9300 (relies on Phase 3 `--port`).
- [ ] In a fourth terminal: `ccoral rooms` discovers all three; tab strip shows them.
- [ ] `Ctrl+N` / `Ctrl+P` cycle focus; transcript region updates.
- [ ] Typing `hello` while focused on tab 2 lands as `[CASSIUS] hello` in room 2's panes only (`tmux capture-pane -p` confirms rooms 1 and 3 untouched).
- [ ] `Ctrl+U` enters unified mode; transcript interleaves all three rooms by timestamp.
- [ ] `/room <id> hello again` from unified mode lands in that room.
- [ ] Switch to tab 1, scroll up, switch to tab 3, switch back: scroll position preserved (free via Textual).
- [ ] New turn lands on non-focused tab → `+` badge appears; focusing the tab clears it.
- [ ] Quit cockpit; all rooms still running (`ccoral room ls`, `tmux ls`).
- [ ] Kill one room mid-cockpit; cockpit shows `(stopped)` badge within 2s; auto-removes after 30s.
- [ ] Resize terminal; layout reflows automatically (Textual CSS Grid).

### Anti-pattern guards

- `grep -nE "subprocess.*ccoral room|spawn.*relay_loop|run_room\\(" rooms_cockpit.py` → 0 (cockpit does not start rooms).
- `grep -nE "while True:\\s*$.*time\\.sleep" rooms_cockpit.py` → 0 (use `@work` + Textual workers).
- `grep -nE "\\\\033\\[" rooms_cockpit.py` → 0.
- `grep -nE "host=\"0\\.0\\.0\\.0\"" rooms_cockpit.py` → 0 (no network surface).

---

## Phase 7 + Phase 12 — Verification (consolidated)

### What to verify

1. **No regressions in existing tests.** Run `python -m pytest tests/` — must pass at the same count as pre-overhaul (S1457 reports 23/23 integration + 100/100 module smokes; record current numbers in this phase's notes before starting and reconfirm after).

2. **Persona-stickiness regression suite (`tests/test_room_persona_sticky.py`):**
   - 4 cases over 100 runs each: `(blank, blank)` deterministic, `(blank, blank)` near-simultaneous, `(blank, leguin)` near-simultaneous, `(blank, leguin)` out-of-order timestamps. Zero attribution swaps.
   - Stress: 200-turn synthetic stream with ±50ms jitter; ordering matches sender's emission order.

3. **Phase 9 Textual cockpit test (`tests/test_room_app.py`):** Uses `RoomApp.run_test()` + `Pilot`:
   ```python
   async def test_say_routes_once():
       async with RoomApp(slot_meta=...).run_test() as pilot:
           await pilot.press("h", "i", "enter")
           assert pilot.app.query_one("#transcript").lines[-1] == "CASSIUS: hi"
           assert pilot.app.tmux_send_calls == [(1, "[CASSIUS] hi"),
                                                (2, "[CASSIUS] hi")]
   ```
   Coverage: input submission, slash commands (`/pause`, `/to`, `/end`), pager re-entry via `App.suspend()`, window resize.

4. **Phase 10 picker test (`tests/test_room_picker.py`):**
   ```python
   async def test_picker_returns_two_profiles():
       async with PickerApp().run_test() as pilot:
           await pilot.press("down", "tab", "down", "down", "enter")
           result = await pilot.app.return_value
           assert result == ("blank", "leguin")
   ```
   Coverage: navigation, filter mode, same-profile confirm modal, quit.

5. **Phase 11 multi-room cockpit test (`tests/test_rooms_cockpit.py`):**
   - Stub two `transcript.jsonl` writers as fixtures.
   - `RoomsCockpit.run_test()` + `Pilot`. Drive `Ctrl+N`, then `h`,`i`,`Enter`; assert FIFO 2 receives `{"kind": "say", "text": "hi"}` exactly once; FIFO 1 receives nothing.
   - `Ctrl+U` → unified mode; assert subsequent rendered lines have `[<id>:<SPEAKER>]` prefix.
   - `/room <id1> world` + Enter; assert FIFO 1 receives.

6. **Room integration tests** under `tests/test_room.py`:
   - Spin two `blank` profiles in room mode against a stub Anthropic server (reuse fixtures/captured dumps from `tests/raw-*`).
   - Drive a 3-turn exchange end-to-end; assert turn order, no `Read /tmp/...` leaks (`tmux capture-pane -p`), `transcript.jsonl` length == 3, `meta.yaml` exit_reason == `clean`.
   - Drive an interjection: send `("say", "hello")` via the control FIFO during turn 2; assert it lands exactly once in transcript and exactly once in each pane.
   - Drive a resume: exit, resume, send `--seed1 "continue"`, assert no recap-style preamble in turn 1.

7. **Anti-pattern grep sweep** — collect all guard-greps from Phases 3/4/5/6/8/9/10/11 into a single `tests/anti_patterns_room.sh` script; CI-style exit non-zero on any hit.

8. **Live runbook** in `.plan/room-overhaul-runbook.md` — the actual button-press sequence for a real-session smoke after each phase, mirroring `.plan/phase6-validation.md`'s format from the permissive-core series. Sequences:
   - Sequence A (persona stickiness): launch `ccoral room blank blank "say A then B"`, verify slot suffixes, dump record JSONL.
   - Sequence B (picker): launch `ccoral room` (no args), navigate, filter, confirm, abort.
   - Sequence C (multi-room): launch 3 rooms across 3 terminals, attach `ccoral rooms`, drive switch + interject + unified mode + room-stop scenario.

9. **Cross-phase sign-off** — write `.plan/room-overhaul-verification.md` with: tests run, anti-patterns clean, deviations from this plan, follow-ups, open issues.

10. **Legacy cockpit removal.** If Phase 9's `--legacy-cockpit` fallback sees no usage in the verification week, delete `room_control_legacy.py` and the flag.

### Verification checklist (meta)

- [ ] All Phase 3/4/5/6/8/9/10/11 verification checklists pass.
- [ ] All anti-pattern greps return 0.
- [ ] `pytest` count meets or exceeds pre-overhaul baseline.
- [ ] `tests/test_room_persona_sticky.py` ≥ 6 tests, all pass.
- [ ] `tests/test_room_app.py` ≥ 5 tests, all pass.
- [ ] `tests/test_room_picker.py` ≥ 4 tests, all pass.
- [ ] `tests/test_rooms_cockpit.py` ≥ 4 tests, all pass.
- [ ] New `tests/test_room.py` has ≥4 integration tests, all passing.
- [ ] `.plan/room-overhaul-runbook.md` executed once on the workstation; verification report committed.
- [ ] `room_control_legacy.py` deleted (if no regressions during verification window).

---

## Cross-cutting notes

### Atomic commit slicing (per ENI executor discipline)

Each phase = one or more atomic commits, one task per commit:

- **Phase 8 (next):** `server: stamp turn record with profile + slot` / `room: slot-keyed pane mapping (replace name-keyed)` / `room: drop mtime-as-speaker-oracle` / `room_control: per-slot display API` / `tests: persona stickiness regression suite`.
- **Phase 9:** `deps: add textual>=0.80` / `room_app: Textual cockpit module + tcss` / `room_app: turn-aware queue port` / `room: switch run_room call site to RoomApp` / `room_control: rename to room_control_legacy with --legacy-cockpit fallback`.
- **Phase 3:** `room: RoomConfig + CLI flags` / `room: per-room state dir + ls/show subcommands`.
- **Phase 4:** `profiles: room_addendum field + schema doc` / `room: replace hardcoded inject append with addendum source` / `room: forward apply_to_subagents/refusal_policy/etc to temp profile`.
- **Phase 10:** `room_picker: Textual picker screen` / `room_picker: same-profile confirm modal` / `ccoral: cmd_room invokes picker when positional profiles missing`.
- **Phase 5:** `room: resume-as-system-note` / `room: export format switch + delete/rename`.
- **Phase 6:** `room: watch sidecar` / `room: serve sidecar (aiohttp, loopback-only)`.
- **Phase 11:** `rooms_cockpit: tabs mode skeleton (TabbedContent + RichLog per tab)` / `rooms_cockpit: background tailers via @work` / `rooms_cockpit: activity badges + per-tab scroll` / `rooms_cockpit: unified mode + /room slash command` / `rooms_cockpit: stopped/broken room handling` / `ccoral: rooms verb + room-watch alias`.
- **Phase 7+12:** `tests: persona stickiness via synthetic JSONL` / `tests: room_app via Pilot` / `tests: room_picker via Pilot` / `tests: rooms_cockpit via Pilot` / `tests: room integration suite` / `tests: anti_patterns_room.sh` / `docs: room overhaul verification report` / `room_control_legacy: delete after verification window`.

### Dependency graph (must respect during execution)

```
[Phase 1, Phase 2 shipped]
   ↓
Phase 8 (slot/profile on turn record)
   ↓
Phase 9 (Textual retrofit) ── enables ──▶ Phase 10 (picker reuses Textual base)
   ↓                              ─────▶ Phase 11 (cockpit reuses Textual base)
Phase 3 (per-room state) ─────────────▶ Phase 11 (discovers ~/.ccoral/rooms/*)
Phase 6 (control FIFO contract) ───▶ Phase 11 (writes to FIFOs)
   ↓
Phases 4, 5, 10 land in any order after their deps.
   ↓
Phase 7+12 runs last.
```

### Deviations expected

- **`os.mkfifo` on macOS:** untested at plan time; if FIFOs misbehave on the user's box, fall back to JSONL append-only path with `os.path.getsize` as the read cursor. (Already handled in Phase 2's shipped sink-precedence at server.py:76–78.)
- **`tmux paste-buffer` for very large turns:** if a single turn exceeds tmux buffer limits (~1MB on most builds), chunk into multiple paste-buffer calls. Phase 8 verification should include a >100KB turn as a stress case.
- **Same-profile cockpit naming.** `BLANK#1` / `BLANK#2` proposed default. Swappable to `BLANK-A` / `BLANK-B` if LO prefers. One-line change.
- **Tab hotkeys.** `Ctrl+N`/`Ctrl+P` may collide with terminal multiplexer bindings. Add `--bind-next <key>` / `--bind-prev <key>` flags if needed; fallback `]`/`[`.
- **Textual version pin.** Targeting `>=0.80`. If a breaking change between minor versions during the iteration, pin to a specific minor (e.g., `~=0.85.0`).
- **Unified mode default room.** Currently spec'd as "most recently active". Alternative: "no default, require `/room` prefix". Decide during Phase 11 implementation.

### Out of scope (named so we don't drift in)

- Replacing tmux with a different multiplexer (zellij, screen, custom pty). Tmux works; the UX failure isn't tmux's fault.
- Adding non-Anthropic models. Room is profile-agnostic at the prompt layer; the model is whatever Claude Code is configured to call. Multi-provider is its own milestone.
- Voice / TTS / STT.
- Web/HTTP UI for the multi-room cockpit (Phase 6's `ccoral room serve` covers single-room HTTP; multi-room HTTP is its own milestone).
- Cross-machine multi-room (loopback only).
- Declarative N-room launch from a config file.

### Done = LO can:

1. Run `ccoral room` with no args, see the picker, pick two profiles, optionally type a topic, hit Enter.
2. Run `ccoral room blank blank` and see slot 1 vs slot 2 displayed distinctly with no attribution swaps even under load.
3. Type `/help` and see the commands; say `hello both` and have it land cleanly in each pane exactly once with no echo bouncing.
4. `/to eni-executor what's the plan` to address one side only.
5. `/pause` mid-stream and have it actually pause; `/end` and get a saved transcript with metadata, no orphan tmux sessions, no plumbing leaks in the export.
6. `ccoral room ls` and `ccoral room show last` to browse what's been said.
7. `ccoral room watch last` from a second terminal while the room is live.
8. `ccoral room --resume last` and have both profiles continue without recapping.
9. Run two or more rooms in parallel terminals, then `ccoral rooms` in another terminal — switch with `Ctrl+N`, see badges on inactive tabs with new turns, type into the focused room only, switch to unified mode for a chronological cross-room view, and quit the cockpit without killing any rooms.
