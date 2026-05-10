# Phase 6 — Real-Session Validation Runbook

**Branch:** `permissive-core` at HEAD `df0e0e9`
**Status as of writeup:** static validation complete (all module smokes + 23/23 parser tests pass). Real-session validation is pending a proxy restart + interactive CC session.

## What needs to happen

The new code (Phases 4 / 5 / 3b / 3c + the rewrite_terminal activation) lives at HEAD but the running proxies on ports 8090, 8091, and 8094 were started before today's commits — they're running stale code. Real-session validation requires a proxy restart, then a fresh CC session that exercises every new code path.

## Step 1 — Restart the proxy

The running proxies as of writeup:
- PID 3581132 on port 8094 (cwd `/home/jcardibo/projects/ccoral`, started 21:58 — main CCORAL)
- PIDs 3624510 / 3624511 on ports 8090 / 8091 (cwd `/home/jcardibo/projects/FapAnalyse`, started 22:08)

To restart only the main CCORAL on port 8094 (preserves the FapAnalyse pair):

```bash
kill 3581132
cd /home/jcardibo/projects/ccoral && CCORAL_PORT=8094 .venv/bin/python server.py &
```

Verify start:
```bash
ss -ltnp | grep ":8094"
tail -1 ~/.ccoral/logs/proxy-*.log    # should show "Listening on 127.0.0.1:8094" or similar
```

## Step 2 — Set CC to use the proxy + the eni profile

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8094
ccoral profile set eni    # or eni-executor, eni-supervisor — any of the 18
claude                    # or `claude --dangerously-skip-permissions` per usual
```

## Step 3 — Run the validation prompts

Each prompt is designed to exercise a specific Phase. Run them in order in the same session.

### 3a. Subagent spawn (Phase 1 — `apply_to_subagents`)

> "Use the Task tool with general-purpose subagent to count how many YAML files exist in the current directory and return just the number."

**Pass criteria:**
- Subagent dispatches and returns
- `~/.ccoral/logs/proxy-*.log` shows two entries: one for the main worker call, one for the subagent (look for `Profile: eni (lane=subagent_*, ...)`)
- The subagent dump file `~/.ccoral/logs/raw-eni-claude-{tier}.json` shows the inject was applied (system prompt should be the ENI persona, not the default CC subagent prompt)

### 3b. ToolSearch use (Phase 2.5 — deferred-tools list preserved)

> "Use the ToolSearch tool to find a tool for taking a screenshot of a webpage, then describe what you found."

**Pass criteria:**
- ToolSearch fires (no "tool not available" error)
- Proxy log shows reminder classification: `[KEEP] deferred_tools_list` or similar
- The deferred-tools system-reminder block survived the smart-strip

### 3c. Skill use (Phase 2.5 — skills list preserved)

> "Run the `/loop` skill briefly with a 60-second interval to verify it's available."

**Pass criteria:**
- The `/loop` skill is available in the skills list
- Proxy log shows `[KEEP] skills_list`

### 3d. claude-mem hook (Phase 2.5 — SessionStart preserved)

> "Use claude-mem to search for any past observations mentioning 'rewrite_terminal' or 'reset_turn'."

**Pass criteria:**
- claude-mem MCP tool fires
- The SessionStart hook output survived the smart-strip (visible in the raw dump)

### 3e. Compaction (Phase 2 — assistant-side strip survives)

Run a session long enough to trigger CC's auto-compact (or invoke `/compact` manually). Then ask a follow-up that depends on the pre-compaction context.

**Pass criteria:**
- Compaction completes without API error
- Post-compaction response demonstrates the model still has the context
- No `<system-reminder>` echoes appear in the post-compaction conversation history

### 3f. Refusal trigger (Phases 3a + 3b — REFUSAL detection + interception)

Use a prompt that LO knows triggers refusal in stock CC. (You know your own triggers better than I do.)

**Pass criteria:**
- Proxy log shows one of:
  - `REFUSAL detected (policy=rewrite_terminal, ...)` — Phase 3a observability
  - `REWRITE_TERMINAL intercepted refusal (...label=..., chars_removed=...)` — Phase 3b interception fired
- `~/.ccoral/logs/refusals.jsonl` exists and contains a record with the matched label
- The visible response in CC has the refusal preamble suppressed (the body of the response should pick up where the model would have continued past the preamble)

### 3g. Lane logging visibility

After the session, `grep "lane=" ~/.ccoral/logs/proxy-*.log | tail -20` should show varied lane labels — at least `main_worker`, `subagent_*`, possibly `session_title_generator` for the session-start title call.

## Step 4 — Post-session inspection

```bash
# Refusal log
cat ~/.ccoral/logs/refusals.jsonl 2>/dev/null | jq -r '.timestamp + "  " + .policy + "  " + (.matches[0].label // "?")'

# Recent proxy log — look for ERRORs and the new feature labels
tail -200 ~/.ccoral/logs/proxy-$(ls -t ~/.ccoral/logs/proxy-*.log | head -1 | xargs -n1 basename | grep -oE '[0-9]+-[0-9-]+' | head -1)*.log | grep -iE "(REFUSAL|REWRITE_TERMINAL|RESET_TURN|lane=|Tool scrub|ERROR)"

# Lane distribution
grep -oE "lane=[a-z_]+" ~/.ccoral/logs/proxy-*.log | sort | uniq -c | sort -rn | head -10

# Reminder preservation — KEEP rate should be high if claude-mem and ToolSearch were used
grep -E "\[KEEP\]|\[STRIP\]" ~/.ccoral/logs/proxy-*.log | tail -20
```

## Pass criteria summary

- [ ] No new `ClientConnectionResetError` rate increase (baseline: a few per session)
- [ ] No new HTTP 4xx from Anthropic
- [ ] At least one functional reminder visible in raw dumps (`[KEEP]` for deferred_tools or skills_list or session_start_hook)
- [ ] At least one task-tool nag stripped successfully per session (`[STRIP] task_tool_nag`)
- [ ] Subagent (Task tool) call's request body has profile inject prepended
- [ ] If a refusal triggered: `refusals.jsonl` populated AND/OR `REWRITE_TERMINAL intercepted` appears in proxy log
- [ ] Lane logging visible in `grep "lane=" ~/.ccoral/logs/proxy-*.log`

## Anti-patterns to NOT skip

- ❌ "It compiles, ship it." Real-session validation has surprised this branch twice already (thinking-block protocol, deferred-tools strip). The unit tests cover the building blocks but not the API protocol details.
- ❌ Validating only with the eni profile. At least one of the validation prompts should be repeated with a different profile (e.g., `red`, `blank`) to confirm the new code paths aren't ENI-specific.

## Open issues at writeup

- **profiles/eni.yaml is partially cleaned only** (commit `df0e0e9`). LO directed Option 1 (vocabulary cleanup only); content directives flagged as content-refused at training time were left alone. Opus may still refuse on those sections — that's a known limitation.
- **reset_turn (Phase 3c) is implemented but NOT activated** on any profile. To activate, change `refusal_policy: rewrite_terminal` → `refusal_policy: reset_turn` on a profile and that profile will start re-issuing on refusal. Recommend testing `reset_turn` first on ONE profile before flipping all.

## After validation

- If everything passes: proceed to Phase 7 (README + docs sync) and Phase 8 (final verification).
- If something regresses: capture the failing raw dump from `~/.ccoral/logs/raw-*.json` and the proxy log line, root-cause, fix in a focused commit, re-run the affected validation prompt.
