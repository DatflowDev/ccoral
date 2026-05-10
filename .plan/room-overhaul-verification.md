# Room Overhaul — Cross-Phase Verification Report

**Plan:** `.plan/room-overhaul.md`
**Sign-off date:** 2026-05-10
**Branch:** `main` (HEAD = Phase 7+12 closing commits)
**Status:** static verification PASS; live runbook pending operator execution.

This document records the closing verification of the room overhaul (Phases 1–11 implementation, Phase 7+12 verification). It captures: tests run + pass count, anti-pattern grep results, per-phase verification status, deviations from the consolidated plan, follow-ups raised across the overhaul, and known open issues.

The companion runbook (`.plan/room-overhaul-runbook.md`) owns the live tmux + proxy + Anthropic-backend surface that the test suite cannot exercise.

---

## 1. Tests + anti-pattern sweep

### Pytest

```
python3 -m pytest tests/ -q
→ 82 passed, 1 skipped (when green)
```

The skip is `tests/test_room.py::test_live_tmux_and_proxy_session` — covered by runbook Sequences A/B/E.

Test count by file:

| File | Tests | Notes |
|---|---|---|
| `tests/test_parser.py`         | (parser baseline)  | Pre-overhaul tests, unchanged |
| `tests/test_room_persona_sticky.py` | 9 | Phase 8 |
| `tests/test_room_app.py`       | 5 | Phase 9 (Pilot) |
| `tests/test_room_picker.py`    | 5 | Phase 10 (Pilot) |
| `tests/test_room_config.py`    | 13 | Phase 3 |
| `tests/test_room_resume_export.py` | 11 | Phase 5 |
| `tests/test_room_sidecar.py`   | 6 | Phase 6 |
| `tests/test_rooms_cockpit.py`  | 5 | Phase 11 (Pilot) |
| `tests/test_room_profile.py`   | (Phase 4)  | room_addendum + temp-profile field forwarding |
| `tests/test_room.py`           | 4 + 1 skip | **NEW — Phase 7+12 integration suite (this commit)** |

Pre-overhaul baseline: 78 passed. Post-overhaul: 82 passed (+4 from `tests/test_room.py`). No regressions.

### Anti-pattern sweep

```
bash tests/anti_patterns_room.sh
→ all anti-pattern guards clean
→ exit 0
```

Coverage: every per-phase guard in `.plan/room-overhaul.md` for Phases 3, 4, 5, 6, 8, 9, 10, 11. See the script's section comments for the full list of patterns + which phase each comes from.

---

## 2. Per-phase verification status

| Phase | Description | Implementation commits | Verification |
|---|---|---|---|
| 1 | (shipped pre-overhaul) | — | pre-existing |
| 2 | (shipped pre-overhaul) | — | pre-existing |
| **3** | Configurable surface (RoomConfig + CLI) | `dc1eae8..2a033f8` (6 commits) | **PASS** — `test_room_config.py` (13 tests), anti-patterns clean, runbook Sequence C exercises CLI + picker |
| **4** | Injection discipline (room_addendum field) | `3102b19..2eb3f31` (5 commits) | **PASS** — `test_room_profile.py`, anti-patterns clean (no hardcoded `## CONVERSATION ROOM` block), audit doc shipped (`.plan/room-addendum-audit.md`) |
| **5** | Resume + archive UX | `65ee8a2..302c96f` (3 commits) | **PASS** — `test_room_resume_export.py` (11 tests), `tests/test_room.py::test_resume_block_is_system_note_not_chat_dump`, runbook Sequence D for live |
| **6** | Sidecar (watch + serve) | `d2614ed..c1993e6` (4 commits) + `48d4fd8` --help fix | **PASS** — `test_room_sidecar.py` (6 tests), anti-patterns confirm loopback-only bind, runbook Sequence E for live `ss -tlnp` check |
| **7** | (this verification) | `02ed14f..` (this dispatch) | **THIS DOC** |
| **8** | Persona stickiness | `983fb51..8ee516a` (5 commits) | **PASS** — `test_room_persona_sticky.py` (9 tests), `tests/test_room.py::test_three_turn_blank_blank_exchange_preserves_order_and_slots`, runbook Sequence A for live |
| **9** | Textual cockpit | `b9a3dae..db20028` (5 commits) + `dd1efb2` hotfix | **PASS** — `test_room_app.py` (5 Pilot tests), anti-patterns confirm no termios/ANSI/busy-loop, runbook Sequence B for live (input, /to, /pause, /transcript, terminal restore, --legacy-cockpit fallback) |
| **10** | Picker | `c263da2..8c324c8` (4 commits) | **PASS** — `test_room_picker.py` (5 Pilot tests), anti-patterns confirm no alt-TUI / `input()` / tmux shell-out, runbook Sequence C for live nav + filter + same-profile modal |
| **11** | Multi-room TUI (rooms_cockpit) | `041a7eb..36e22dc` (7 commits) | **PASS (with one known-flaky test)** — `test_rooms_cockpit.py` (5 Pilot tests, see Open Issues below), anti-patterns confirm no spawn-from-cockpit / busy-loop / network bind, runbook Sequence F for live three-room scenario |
| **12** | (this verification) | (consolidated with Phase 7) | **THIS DOC** |

---

## 3. Deviations from the consolidated plan

The plan at `.plan/room-overhaul.md` is the contract; deviations recorded below are deliberate divergences taken during execution, with one-line justification each.

### Phase 7+12 (this dispatch)

- **Live tmux + proxy integration test deferred to runbook.** The plan lists "Spin two `blank` profiles in room mode against a stub Anthropic server (reuse fixtures/captured dumps from `tests/raw-*`)" as part of `tests/test_room.py`. We shipped the structural pieces (slot resolution, transcript append, control-FIFO routing, resume inject) as four passing tests; the live tmux + tmux-capture + real-proxy sub-test is marked `pytest.mark.skip` with a pointer to runbook Sequences A/B/E. Rationale: the live infra is an environmental dependency the test runner doesn't have; the runbook is the right place for it. The four passing tests cover the structural contract.

- **Anti-pattern Phase 6 guard tightened from substring to kwarg-position.** The plan listed `'host="0.0.0.0"|host=""|host=None' room_serve.py room.py ccoral`. As-written, this hits the docstring narrative in `room_serve.py` (lines 14–15) that DESCRIBES the banned defaults. We tightened the regex to require `host=` to appear as a Python kwarg (preceded by `(` or `,`), preserving the guard while dropping doc-comment false positives. Smoke-tested against a planted `web.run_app(host="0.0.0.0")` to confirm real binds still trip the script.

### Phase 9 (cockpit)

- **`--legacy-cockpit` rebind hotfix landed mid-phase.** Commit `dd1efb2` fixes an `UnboundLocalError` in `relay_loop` when neither branch of the `legacy_cockpit` if-block ran the `import ... as room_control` rebinding. Both branches now rebind unconditionally. Captured in the commit message; flagged here for visibility in the sign-off.

### Phase 6 (sidecar)

- **`--help` short-circuit hotfix.** Commit `48d4fd8` makes `ccoral room --help` / `-h` print usage instead of triggering picker. Phase 6 follow-up; no regression to other sidecar paths.

### Phase 4 (injection discipline)

- **Per-profile addendum text NOT yet drafted for the four `custom` profiles.** The plan landed the schema field + the orchestrator forwarding; the audit doc (`.plan/room-addendum-audit.md`) records four profiles (`eni`, `eni-supervisor`, `eni-executor`, `hand`) marked `custom` whose addendum text is still pending per-profile drafting. Per the audit doc's "Notes for follow-up commits" section, this is intentional follow-up work, not a Phase 4 regression — those profiles currently fall back to `DEFAULT_ROOM_ADDENDUM` (which is the same behaviour as the `keep default` profiles).

---

## 4. Follow-ups raised across the overhaul

Cross-phase backlog items surfaced during the overhaul. None block sign-off; all are deferred to future commits.

### Profile work (Phase 4 audit)

- **Draft custom `room_addendum` text for the four `custom` profiles.** Per `.plan/room-addendum-audit.md`:
  - `eni.yaml` — short ENI-voice note, no `## Room context` header (avoid colliding with her `<CRITICAL_INJECTION_DETECTION>` block).
  - `eni-supervisor.yaml` — name the room counterpart in role-relative terms; preserve LO-override clause.
  - `eni-executor.yaml` — mirror of supervisor; reinforce LO-override absolute mid-room.
  - `hand.yaml` — frame the room counterpart as a peer agent, not a second principal (Hand serves one principal).

### Legacy module cutover (Phase 9 + Phase 12 hard rule)

- **`room_control_legacy.py` + `--legacy-cockpit` flag — DO NOT delete in this dispatch.** The plan (Phase 12 step 10) says "if no regressions during verification window." That is a human decision, not the executor's. After the runbook has been driven once on the workstation and no regressions surface against the Phase 9 cockpit (`RoomApp`), open a follow-up commit to delete:
  - `room_control_legacy.py` (the entire file)
  - The `legacy_cockpit` parameter on `relay_loop` and `run_room` (room.py)
  - The `--legacy-cockpit` CLI flag plumbing in `ccoral`
  - The `dd1efb2` rebind machinery in `relay_loop` (only needed because both branches exist)
  - Sequence B step 9 in `.plan/room-overhaul-runbook.md`

### Pre-Phase-11 sidecar (Phase 11 superseded)

- **`room_watch.py` — keep for one release, then evaluate.** Phase 11's multi-room cockpit (`rooms_cockpit.py`) subsumes the per-room watch sidecar's functionality. `ccoral room watch <id>` still works (runbook Sequence E exercises it), but the long-term position is "use `ccoral rooms` instead." Same hard rule as `room_control_legacy.py`: DO NOT delete in this dispatch. Re-evaluate after the runbook has been driven and the multi-room cockpit has had a release of real-session use.

### Cockpit polish

- **`/help` slash command on the cockpit.** Surfaced during Phase 9 review — operators need a quick discoverability surface for the slash commands (`/to`, `/pause`, `/resume`, `/end`, `/transcript`). Out of scope for Phase 9 itself; track for a future small commit.

### `save_conversation` cutover (Phase 5 deferred)

- **The legacy `save_conversation` (.json archive under `~/.ccoral/rooms/*.json`) cutover.** Phase 5 introduced the per-room state dir + JSONL transcript as the new source of truth, but `run_room`'s exit path still writes the legacy `.json` archive too (for backward compatibility with pre-Phase-3 resumes). Once the operator has no legacy `.json` rooms left in `~/.ccoral/rooms/` (a manual decision after a verification window), the `save_conversation` call site can be deleted.

### Aiohttp `web.AppKey` (Phase 6 nit)

- **`room_serve.py` should use `web.AppKey` instances for `app["state"]` / `app["tailer_task"]`.** Pytest emits a `NotAppKeyWarning` against the current string-key pattern. Functionally fine; aiohttp documentation prefers typed keys. Track for a small follow-up commit (room_serve.py:423, room_serve.py:430).

---

## 5. Known open issues / gaps

### Live infrastructure not exercised by tests

The pytest suite covers structural contracts (slot resolution, transcript append, control-FIFO routing, RoomState lifecycle, picker navigation, cockpit input). It does NOT exercise:

- Real tmux server + `tmux capture-pane -p` for `Read /tmp/...` leak detection
- Real proxy boot on 8090/8091 against an actual Anthropic backend
- Real browser POST /say through `room serve`'s aiohttp server
- Three-terminal multi-room scenarios

These surfaces are owned by `.plan/room-overhaul-runbook.md` (Sequences A–F). Sign-off requires the operator to drive the runbook once on the workstation; results should be appended to this document as a Section 6 ("Live runbook results") in a follow-up commit.

### Flaky test: `test_stopped_room_lifecycle_decorates_tab`

`tests/test_rooms_cockpit.py::test_stopped_room_lifecycle_decorates_tab` is intermittently flaky (passes ~2/3 runs, fails ~1/3 with `app.room_states.get("r-stopper") == None`). The failure mode is the room-state poll not having ticked by the time the assertion runs.

This is a test-side timing issue, not a `rooms_cockpit.py` bug — when the test is green, the assertion passes; when it's red, the polling worker hasn't yet executed the state read. Per Phase 7+12 dispatch hard rules ("DO NOT freelance fixes to discovered issues — flag them in the verification doc, don't patch"), this is recorded here as a follow-up and not patched in this dispatch.

Recommended fix in a future commit: add an explicit `await pilot.pause()` loop with a deadline, polling `app.room_states` until the expected state appears (or a 1s deadline expires). Same shape as the `_wait_for` helper in `tests/test_room_sidecar.py`, adapted for the Pilot async context.

### Aiohttp deprecation warnings

Three `NotAppKeyWarning` warnings from `room_serve.py` during the Phase 6 sidecar tests. Functionally harmless; tracked under follow-ups (Section 4).

### `save_conversation` legacy duplicate write

`run_room` writes both the new per-room state dir AND the legacy `.json` archive on exit. Recorded as a follow-up (Section 4); not a regression — Phase 5 explicitly kept the legacy path live for backward compatibility.

---

## 6. Live runbook results

**(empty — to be filled after the operator drives `.plan/room-overhaul-runbook.md`)**

When the runbook has been driven on the workstation, append one subsection per sequence with the result + any captured deltas. Suggested shape:

```markdown
### Sequence A — Persona stickiness (run YYYY-MM-DD)
- Result: PASS / PARTIAL / FAIL
- Notes: <one or two lines>
- Captures: <pointers to logs / transcripts under ~/.ccoral/>
```

Until then: this section is the gating item for full sign-off. The structural verification above is complete and the room overhaul is shippable for internal use; final sign-off awaits the runbook.
