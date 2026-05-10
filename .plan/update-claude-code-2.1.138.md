# CCORAL — Update for Claude Code 2.1.138

**Goal:** Bring CCORAL's parser, model detection, subagent threshold, and CLI in line with current Claude Code (v2.1.138, captured May 8 2026), and add a `--port` flag with per-port active-profile state so multiple ccoral daemons can run simultaneously.

**Decisions made up front (do not relitigate):**
- **Subagent detection:** keep the size-based threshold but raise it from 15000 → **22000 chars**. Header-based detection is not adopted in this plan.
- **Port ergonomics:** add `--port` to `ccoral start` and `ccoral run`; add a per-port active-profile file at `~/.ccoral/active_profile.<port>` that, if present, overrides the global `~/.ccoral/active_profile` for that daemon.
- **Source of truth for CC structure:** `https://github.com/Piebald-AI/claude-code-system-prompts` (v2.1.138). Fragment IDs from `https://github.com/Piebald-AI/tweakcc` (`data/prompts/prompts-2.1.98.json`).

---

## Phase 0 — Documentation Discovery (DONE — read this before any other phase)

This phase has been executed; findings are consolidated below. Subsequent phases assume these facts.

### Allowed reference sources
- `https://github.com/Piebald-AI/claude-code-system-prompts` — `system-prompts/`, `tool-description-*.md`, `agent-prompt-*.md`, `data-claude-model-catalog.md`, README, CHANGELOG.
- `https://github.com/Piebald-AI/tweakcc` — `data/prompts/prompts-X.Y.Z.json` for machine-readable fragment IDs.
- Use `gh api repos/<owner>/<repo>/contents/<path>` (decode `.content` from base64) or `firecrawl-scrape`/`jina` for fetching. Do NOT use WebFetch.

### Confirmed facts (CC v2.1.138)

**The main system prompt is assembled at runtime from ~25–50 fragments. There is no single canonical string.**

**Top-level headers actually present in modern CC** (these *do* match against text):
- `# Harness` (NEW since 2.1.120 — opens with the "You are Claude Code…" identity sentence interpolated above it)
- `# Executing actions with care`
- `# Text output (does not apply to tool calls)` (this is the new communication-style header — NOT `# Tone and style`)
- `# Committing changes with git`
- `# Creating pull requests`
- `# Other common operations`
- `# Environment` (still present)

**Sections that are PROSE WITHOUT HEADERS in modern CC** (CCORAL must match by leading sentence, not header):
- Action safety and truthful reporting (NEW in 2.1.136 — opens with `IMPORTANT:` text)
- Doing tasks fragments (six small files: software-engineering-focus, security, no-unnecessary-error-handling, no-compatibility-hacks, ambitious-tasks, help-and-feedback) — none carry `# Doing tasks`.
- Tool usage (task-management, subagent-guidance, parallel-tool-call-note) — no `# Tool usage` header.
- Tone and style fragments (code-references, concise-output-short) — no header.
- Censoring/security policy — opens with `IMPORTANT: Assist with authorized…` (CCORAL already matches this).

**XML blocks:**
- `<system-reminder>` is current and active. ✓
- `<available-deferred-tools>` does NOT appear as a literal XML tag in current CC. The deferred-tools list arrives wrapped in a `<system-reminder>` block. CCORAL's `deferred_tools` matcher (XML key `available-deferred-tools`) is **dead code in 2.1.x**.
- `<fast_mode_info>` does NOT appear in current CC. **Dead matcher.**

**Subagent prompts:**
- Self-contained, much shorter (1.3KB–4KB raw fragments → 5–12KB total once env + reminders + CLAUDE.md attach).
- Open with `You are an agent for Claude Code` (general-purpose) or `You are a file search specialist for Claude Code` (Explore) — NOT `You are Claude Code`.
- Receive `system-prompt-agent-thread-notes.md` (absolute paths, no emoji, no markdown summary files, no colon before tool calls).
- Do NOT receive: harness instructions, censoring policy, executing-actions-with-care, git/PR sections.

**Model catalog (current per `data-claude-model-catalog.md` v2.1.128+):**
- `claude-opus-4-7` (alias)
- `claude-opus-4-6` (alias)
- `claude-sonnet-4-6` (alias)
- `claude-haiku-4-5-20251001` (alias `claude-haiku-4-5`)
- Active legacy: `claude-opus-4-5-20251101`, `claude-opus-4-1-20250805`, `claude-sonnet-4-5-20250929`, `claude-sonnet-4-5`
- Routing slug observed in this session: `claude-opus-4-7[1m]` (1M-context routing variant — does NOT contain "haiku").

**Sizes:**
- Main-conversation system prompt: ~30,000–35,000 chars typical, more with large CLAUDE.md.
- Subagent system prompts: 5,000–12,000 chars typical; can exceed 15,000 in heavy-context sessions but rarely 22,000.

### Section delta vs. CCORAL's `SECTION_IDENTIFIERS` (parser.py:51-84)

| CCORAL key | Status in CC 2.1.138 | Action |
|---|---|---|
| `identity` ("you are claude code") | Still present at top of `# Harness` block | Keep |
| `system` ("system") | The `# System` header still exists for some CC builds; conditional | Keep |
| `doing_tasks` ("doing tasks") | **No header in current CC**; prose only | Match by leading sentence of each fragment OR drop key |
| `using_tools` ("using your tools") | **No header in current CC** | Drop / replace with prose matchers |
| `tool_usage_policy` ("tool usage policy") | **No header in current CC** | Drop |
| `tone_style` ("tone and style") | **Renamed**; the new header is `# Text output (does not apply to tool calls)` | Rename matcher; add `text_output` key matching `text output` |
| `output_efficiency` ("output efficiency") | Not observed | Drop |
| `executing_actions` ("executing actions with care") | ✓ Still matches | Keep |
| `auto_memory` ("auto memory") | Header not in current CC; new fragment is `# auto memory` (lowercase) — verify | Keep, lowercase-match |
| `environment` ("environment") | ✓ Still present | Keep |
| `git_commit` ("committing changes with git") | ✓ | Keep |
| `pull_requests` ("creating pull requests") | ✓ | Keep |
| `other_operations` ("other common operations") | ✓ | Keep |
| `security_policy` ("important: assist with authorized") | ✓ Still matches | Keep |
| `url_policy` ("important: you must never") | **Text removed** | Drop |
| `deferred_tools` (xml `available-deferred-tools`) | **Tag does not appear in 2.1.x** | Keep matcher for back-compat but log a note; preserve via system-reminder route |
| `system_reminder` (xml `system-reminder`) | ✓ Active and frequent | Keep |
| `fast_mode` (xml `fast_mode_info`) | **Tag not in 2.1.x** | Drop |
| `claude_md` ("claudemd") | ✓ Still present (delivered via system-reminder in some paths) | Keep |
| `current_date` ("currentdate") | ✓ | Keep |
| `memory_index` ("memory index") | Header still present in some sessions | Keep |

**New keys to add:**
- `harness` — header `# Harness`
- `text_output` — header `# Text output (does not apply to tool calls)`
- `action_safety` — leading sentence of action-safety-and-truthful-reporting (an `IMPORTANT:` line; needs exact text from `system-prompts/system-prompt-action-safety-and-truthful-reporting.md`)
- `agent_thread_notes` — leading sentence of `system-prompts/system-prompt-agent-thread-notes.md` (subagent-only)
- `memory_instructions` — leading sentence of `system-prompts/system-prompt-memory-instructions.md` (or header if one exists)
- `command_message` — XML tag `<command-message>` (visible in this session — slash-command messages)

### Anti-patterns to avoid
- Inventing section names not present in the captured fragments. Verify every new `SECTION_IDENTIFIERS` entry against a real `.md` file in `claude-code-system-prompts`.
- Removing `available-deferred-tools` matcher entirely — keep it (cheap) for users still on older CC builds.
- Hardcoding `claude-opus-4-7` etc. as exact strings — Anthropic ships dated variants and `[1m]`-suffixed routing slugs. Keep substring matching but be explicit which substring.
- Adding new behavior to the subagent path that depends on parsing the full main prompt — subagents do not have it.

---

## Phase 1 — Refresh `parser.py` SECTION_IDENTIFIERS for CC 2.1.138

**Files:** `parser.py`, `PROFILE_SCHEMA.md`

**What to implement** (copy-target style):

1. In `parser.py:51-84`, update `SECTION_IDENTIFIERS` per the delta table in Phase 0. Specifically:
   - **Add** entries:
     - `"harness": "harness"` (matches `# Harness` markdown header).
     - `"text output (does not apply to tool calls)": "text_output"` (the new communication-style header — note the parenthetical is part of the header text in the source fragment).
     - `"text output": "text_output"` (fallback for header trimming).
     - `"command-message": "command_message"` (XML tag, used in slash-command sessions).
     - `"command-name": "command_name"`.
     - `"command-args": "command_args"`.
   - **Remove** these dead entries (they no longer match anything in 2.1.x):
     - `"output efficiency"`, `"tool usage policy"`, `"using your tools"`, `"important: you must never"`, `"fast_mode_info"` (XML).
   - **Keep** `available-deferred-tools` and `system-reminder` XML matchers.
   - **Add** prose matchers — these need a new mechanism because the fragments have no header. Add a constant list `PROSE_FRAGMENT_LEAD_SENTENCES: list[tuple[str, str]]` mapping `(leading_substring, canonical_name)`. Lead substrings to copy verbatim from `claude-code-system-prompts/system-prompts/`:
     - `system-prompt-action-safety-and-truthful-reporting.md` → `action_safety`
     - `system-prompt-doing-tasks-software-engineering-focus.md` → `doing_tasks`
     - `system-prompt-tool-usage-task-management.md` → `tool_usage`
     - `system-prompt-tool-usage-subagent-guidance.md` → `tool_usage`
     - `system-prompt-parallel-tool-call-note-part-of-tool-usage-policy.md` → `tool_usage`
     - `system-prompt-tone-and-style-code-references.md` → `tone_style`
     - `system-prompt-tone-and-style-concise-output-short.md` → `tone_style`
     - `system-prompt-agent-thread-notes.md` → `agent_thread_notes`
     - `system-prompt-memory-instructions.md` → `memory_instructions`
   - In `_identify_section()` (parser.py:87), after the existing markdown/XML/identity checks and before the unknown-header fallthrough, add a new check that scans the line against `PROSE_FRAGMENT_LEAD_SENTENCES` (longest-prefix match wins) and returns `(canonical_name, "prose_fragment", 0)` when a match is found. Section type `"prose_fragment"` should NOT participate in XML close-tag logic.
   - Update `apply_profile()` (parser.py:254) `preserve_map`:
     - Add: `"harness": "harness"`, `"text_output": "text_output"`, `"action_safety": "action_safety"`, `"tool_usage": "tool_usage"`, `"tone_style": "tone_style"`, `"agent_thread_notes": "agent_thread_notes"`, `"memory_instructions": "memory_instructions"`.
     - The default-preserve set (parser.py:295) `{"environment", "deferred_tools", "current_date", "claude_md"}` should be expanded to include `"harness"` so the identity sentence and its bullets survive default-preserve mode, since the identity sentence now lives inside `# Harness`. Without this, the inject-replaces-identity flow may strip the harness bullets and leave the model with no context.
     - **CAREFUL:** the inject path (parser.py:323-328) replaces the `identity` section. The `identity` section as currently parsed will start at "You are Claude Code…" and continue until the next section boundary (i.e., `# Harness`'s bullets are PART of `identity`). Verify by running `dump_tree()` on a captured 2.1.138 prompt; the identity section should swallow harness bullets and the existing inject behavior continues to work. If `# Harness` is parsed as a separate section before identity, instead, then the identity-replacement leaves harness bullets behind — fix by also stripping `harness` when injecting.

2. In `parser.py:_identify_section`, the XML-tag matcher's iteration over `SECTION_IDENTIFIERS` does an O(n) scan with a string-equality check (`tag == pattern`). With new XML tags added (`command-message`, `command-name`, `command-args`), this still works but document the existing case-folding (`tag = xml_match.group(1).lower()`).

3. Update `PROFILE_SCHEMA.md` (lines 53-75) section table:
   - Replace the "What It Contains" table to match the new `SECTION_IDENTIFIERS`.
   - Mark deprecated keys (`output_efficiency`, etc.) under a "Deprecated (CC 2.1.x removed)" subsection rather than deleting silently.
   - Add new keys to the "Default" column (mark `harness` as kept-by-default).

**Documentation references:**
- README of `claude-code-system-prompts` for the canonical fragment list and assembly order.
- `system-prompts/system-prompt-harness-instructions.md` — for `# Harness` header text.
- `system-prompts/system-prompt-communication-style.md` — for `# Text output (does not apply to tool calls)` header text.
- Each `system-prompts/system-prompt-*.md` for the prose-fragment lead sentences (copy first 60–80 chars verbatim into `PROSE_FRAGMENT_LEAD_SENTENCES`).

**Verification checklist:**
- `python3 -c "from parser import SECTION_IDENTIFIERS, PROSE_FRAGMENT_LEAD_SENTENCES; print(len(SECTION_IDENTIFIERS), len(PROSE_FRAGMENT_LEAD_SENTENCES))"` — non-zero counts, no import errors.
- Save a real captured 2.1.138 system prompt to `tests/fixtures/main-2.1.138.json` (the proxy already dumps each request to `~/.ccoral/logs/raw-*.json` — copy one). Run `parse_system_prompt()` on it and assert these section names appear in the parsed output: `identity`, `harness`, `text_output`, `executing_actions`, `git_commit`, `pull_requests`, `system_reminder`, `environment`, `claude_md`.
- Same fixture but for a subagent (`tests/fixtures/subagent-2.1.138.json` from `~/.ccoral/logs/raw-noprofile-*.json` of a subagent call) — assert `agent_thread_notes` appears, `harness` does NOT, `git_commit` does NOT.
- Apply a `vonnegut`-style profile to the main fixture (preserve = environment, current_date, claude_md, harness; minimal=false). Rebuild. Verify the rebuilt system prompt:
  - Contains the inject text where identity used to be.
  - Contains the original `# Harness` bullets (kept via default-preserve).
  - Does NOT contain `# Executing actions with care`.
  - Does NOT contain `IMPORTANT: Assist with authorized…`.

**Anti-pattern guards:**
- Do NOT delete `deferred_tools` or `system_reminder` matchers — keep both (one is dead-but-cheap, the other is hot).
- Do NOT add prose-fragment matchers without first viewing the actual lead sentence in the source `.md`. Wrong substrings will silently mis-classify.
- Do NOT change the existing identity-replacement logic in `apply_profile()` until the `dump_tree()` output on the captured fixture confirms the identity section's content boundary.

---

## Phase 2 — Refresh model-name detection and tighten subagent threshold

**Files:** `server.py`

**What to implement:**

1. **Model-tier resolver.** In `server.py` near the top of `handle_messages()` (currently `is_haiku = "haiku" in model` at line 340), replace with an explicit helper:

   ```python
   def model_tier(model: str) -> str:
       """Return one of: 'opus', 'sonnet', 'haiku', 'unknown'. Robust to dated and routing-slug variants."""
       m = (model or "").lower()
       if "haiku" in m:
           return "haiku"
       if "sonnet" in m:
           return "sonnet"
       if "opus" in m:
           return "opus"
       return "unknown"
   ```

   Use `tier = model_tier(model)` and `is_haiku = tier == "haiku"` from then on. Same routing semantics; the new function gives us a hook to log model usage and is explicit about which substring matches what.

2. **Subagent threshold.** In `server.py:354`, change `SUBAGENT_THRESHOLD = 15000` → `SUBAGENT_THRESHOLD = 22000`. Add a leading comment citing the v2.1.138 sizing data:
   ```
   # CC 2.1.138 sizing: main convo system prompt 30K-35K+ chars; subagent
   # 5K-12K typical, can spike to 15K-18K with heavy CLAUDE.md or
   # system-reminders. 22K threshold gives clean separation.
   ```

3. **Routing slug awareness.** No code change required — the `[1m]` suffix on `claude-opus-4-7[1m]` does not contain "haiku" or "sonnet" or "opus" hyphenated literally — wait, it DOES contain "opus". Verify `"opus" in "claude-opus-4-7[1m]".lower()` → True. So `model_tier("claude-opus-4-7[1m]")` returns `"opus"`. Good. No-op for the routing slug case; just confirm with a one-liner test in the verification phase.

4. **Optional but recommended:** add a one-line log when `tier == "unknown"` so future model name surprises are visible:
   ```python
   if tier == "unknown" and model:
       log.warning(f"Unknown model tier for: {model!r} — treated as main-tier")
   ```

**Documentation references:**
- `data-claude-model-catalog.md` from `claude-code-system-prompts` — current model alias list.
- This very session's environment block (`The exact model ID is claude-opus-4-7[1m]`) — confirms `[1m]` routing slug exists in production.

**Verification checklist:**
- Unit-style assertions in a throwaway script:
  - `model_tier("claude-opus-4-7") == "opus"`
  - `model_tier("claude-opus-4-7[1m]") == "opus"`
  - `model_tier("claude-sonnet-4-6") == "sonnet"`
  - `model_tier("claude-haiku-4-5-20251001") == "haiku"`
  - `model_tier("claude-3-5-haiku-20241022") == "haiku"` (legacy)
  - `model_tier("") == "unknown"`
  - `model_tier(None) == "unknown"` (helper guards None)
- Replay a captured raw subagent JSON through `modify_request_body()` with a profile loaded; confirm the subagent path fires (size < 22000) and `haiku_inject` prepends correctly.
- Replay a captured main-conversation raw JSON; confirm size >= 22000 and the full-inject path fires.

**Anti-pattern guards:**
- Do NOT use a regex for model-tier detection (e.g. `r"-haiku-"`); a plain substring is sufficient and avoids surprises with model aliases.
- Do NOT bake the threshold into a profile field — keep it global. Profiles are persona contracts, not routing config.

---

## Phase 3 — Multi-instance support: `--port` flag + per-port active profile

**Files:** `ccoral` (CLI), `profiles.py`, `server.py`, `PROFILE_SCHEMA.md`, `README.md`

**What to implement:**

1. **`profiles.py` — per-port active profile.** Modify `get_active_profile()` and `set_active_profile()` to accept an optional `port: int | None = None`:

   ```python
   def _active_profile_path(port: int | None) -> Path:
       base = Path.home() / ".ccoral"
       if port is not None:
           return base / f"active_profile.{port}"
       return base / "active_profile"

   def get_active_profile(port: int | None = None) -> Optional[str]:
       # 1. Try per-port file first if port given
       # 2. Fall back to global active_profile
       ...

   def set_active_profile(name: Optional[str], port: int | None = None):
       ...
   ```

   Resolution rule (document this in PROFILE_SCHEMA.md): per-port file (if exists) wins over global; `CCORAL_PROFILE` env var still wins over both (existing behavior in server.py:330).

2. **`server.py` — pass port into profile lookup.** In `handle_messages()` where `get_active_profile()` and `load_active_profile()` are called (lines 314, 334-335), pass the daemon's listening port (`PORT` global, line 51). Same for the raw-dump filename (line 314) — already includes profile name, no change needed.

3. **`ccoral` CLI — `--port` flag plumbing.**
   - `cmd_start()` (line 64): accept `port: int | None = None`. If provided, `os.environ["CCORAL_PORT"] = str(port)` before importing `server.main`.
   - `cmd_run()` already accepts `port` — keep, but ensure the `--port` long-form flag is parsed in `main()` alongside the existing positional digit parsing.
   - `cmd_use()` (line 154): accept `port: int | None`. Call `set_active_profile(name, port=port)` so `ccoral use vonnegut --port 8081` writes only to `active_profile.8081`.
   - `cmd_off()` (line 167): same — `set_active_profile(None, port=port)` deletes only the per-port file (or the global if no port given).
   - `cmd_status()`: when called with `--port`, show that port's resolved active profile. Without `--port`, show the global.
   - `main()` (line 370): add a top-level `--port <n>` parser that strips the flag from `sys.argv[2:]` before the existing arg dispatch. This way `ccoral start --port 8081`, `ccoral use vonnegut --port 8081`, `ccoral run dan --port 9000`, etc., all work uniformly.

4. **README.md additions** (around the "Quick start" and "Environment variables" sections):
   - Show a worked multi-instance example:
     ```
     # Terminal 1 — Vonnegut on 8081
     ccoral start --port 8081 &
     ccoral use vonnegut --port 8081
     ANTHROPIC_BASE_URL=http://127.0.0.1:8081 claude

     # Terminal 2 — DAN on 8082, simultaneously
     ccoral start --port 8082 &
     ccoral use dan --port 8082
     ANTHROPIC_BASE_URL=http://127.0.0.1:8082 claude
     ```
   - Document the resolution order: env `CCORAL_PROFILE` > `~/.ccoral/active_profile.<port>` > `~/.ccoral/active_profile`.

5. **Backwards compatibility.** Existing single-instance flows MUST continue to work without any flag:
   - `ccoral start` (no flag) → reads `CCORAL_PORT` or default 8080, uses global `active_profile`.
   - `ccoral use foo` (no flag) → writes global `active_profile`.
   - Daemons started without `CCORAL_PORT`/--port still see the global active_profile.

**Documentation references:**
- Existing `cmd_run()` already finds a free port via `find_free_port()` (ccoral:40-51). Reuse for `cmd_start --port` if `--port` is omitted? **No** — `start` should use the explicit port (or env, or 8080 default) and fail loudly on collision. Auto-port selection stays scoped to `run` because `run` knows how to inject the chosen port into Claude's env.

**Verification checklist:**
- `ccoral start --port 8081 &` then `ccoral use vonnegut --port 8081` then `ls ~/.ccoral/` shows `active_profile.8081` (and not a touched global). `ccoral status --port 8081` reports `vonnegut`. `ccoral status` (no flag) reports the prior global value (or `(none)`).
- Run two daemons concurrently on 8081 and 8082 with different per-port active profiles. `curl -s -X POST http://127.0.0.1:8081/v1/messages -H 'content-type: application/json' --data '{"model":"claude-opus-4-7","max_tokens":4,"messages":[{"role":"user","content":"hi"}]}'` (no API key — request will 4xx upstream, but the proxy log under `~/.ccoral/logs/proxy-8081-*.log` should show the 8081 instance applied vonnegut while 8082 applied DAN).
- `ccoral off --port 8081` removes only `active_profile.8081`. Global file untouched.
- `ccoral run dan --port 9000` launches a locked daemon and Claude through it without disturbing global active_profile.

**Anti-pattern guards:**
- Do NOT collapse `~/.ccoral/active_profile` into `~/.ccoral/active_profile.0` for "uniformity". Keeps backwards compatibility with existing user state.
- Do NOT make per-port required. Single-instance users keep using the global file.
- Do NOT plumb `port` through `room.py`. Room mode already manages its own ports (`BASE_PORT` 8090/8091) and does not need the per-port active-profile file (it generates temp profiles in `~/.ccoral/profiles/*-room.yaml` and locks via `CCORAL_PROFILE` env var). Touching room is out of scope.

---

## Phase 4 — Verification + docs

**Files:** `tests/` (new), `README.md`, `PROFILE_SCHEMA.md`

**What to implement:**

1. **Capture fixtures.** With CCORAL running, perform one normal Claude session and one subagent invocation. Copy two raw dumps from `~/.ccoral/logs/`:
   - `~/.ccoral/logs/raw-<profile>-<model>.json` (most recent, likely `raw-noprofile-claude-opu.json` for an unmodified main call) → `tests/fixtures/main-2.1.138.json`.
   - One subagent dump (also `raw-*.json`, smaller body) → `tests/fixtures/subagent-2.1.138.json`.
   - Sanitize: strip the `messages[]` field bodies if private; the parser only needs `system` and `model`.

2. **Smoke test script.** Create `tests/test_parser.py` (no pytest dependency required — plain `python3 tests/test_parser.py`). It should:
   - Load each fixture's `system` field, run `parse_system_prompt()`, then `apply_profile(blocks, vonnegut_profile)`, then `rebuild_system_prompt()`.
   - Assert key sections exist in the parsed `main` fixture: `identity`, `harness`, `executing_actions`, `system_reminder`, `environment`, `git_commit`.
   - Assert `agent_thread_notes` exists in the `subagent` fixture; `harness` does NOT.
   - Assert `model_tier` returns the right tier for each of: `claude-opus-4-7`, `claude-opus-4-7[1m]`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `claude-3-5-haiku-20241022`, `""`, `None`.

3. **Anti-pattern grep checks.**
   - `grep -n "fast_mode_info" parser.py` → no match (matcher removed).
   - `grep -n "tool usage policy" parser.py` → no match.
   - `grep -n "important: you must never" parser.py` → no match (case-insensitive).
   - `grep -n "is_haiku = .haiku. in model" server.py` → no direct substring usage; should now go through `model_tier()`.
   - `grep -n "SUBAGENT_THRESHOLD = 15000" server.py` → no match.
   - `grep -n "SUBAGENT_THRESHOLD = 22000" server.py` → match.

4. **Live smoke.**
   - With Phase 1+2 changes applied, run `ccoral run vonnegut 8085` in a tmux pane. Type a single user message. Confirm in `~/.ccoral/logs/proxy-*.log` that:
     - The proxy logged the parse with new section names visible.
     - System prompt size went from ~30K → small (after-inject).
     - The model responded as Vonnegut (non-functional UI check, but a real signal).
   - With Phase 3 changes applied, run two ports concurrently per the multi-instance verification list and confirm independent profile state.

5. **README + schema doc updates.**
   - README: bump CCORAL version (in `ccoral` line 28: `VERSION = "2.1.0"` → `2.2.0`), update model-detection note, add multi-instance example block (text staged in Phase 3).
   - PROFILE_SCHEMA.md: update section table, deprecation note, new keys, per-port active-profile note.

**Verification checklist (all of):**
- All Phase 1, 2, 3 verification checks pass.
- `tests/test_parser.py` exits 0.
- All anti-pattern greps pass.
- Live ccoral run with a real persona profile produces a non-default response in Claude Code.
- Two concurrent daemons on different ports each apply different profiles to their requests.

**Anti-pattern guards:**
- Do NOT mark this phase complete based on parser tests alone. The live ccoral-run check is required because parser correctness ≠ proxy correctness (header forwarding, body re-serialization, streaming all matter).
- Do NOT skip the subagent fixture. The 22K threshold and `agent_thread_notes` matcher only get exercised on subagent calls.

---

## Out of scope for this plan
- Refactoring `parser.py` from regex/string matching to AST/markdown-tree parsing. Keep the line-by-line matcher; just refresh its data tables.
- Reworking `room.py`. Room ports already work and use `CCORAL_PROFILE` env locking.
- Adding profile schema fields (no new YAML keys). Existing profiles continue to apply unchanged.
- Adding a `claude_code_version` detection routine. CC doesn't expose its version in the API request body; we'd have to infer from prompt content, which is brittle. Defer.
