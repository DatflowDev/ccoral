"""
CCORAL v2 — Proxy Server
==========================

Async HTTP proxy that intercepts Claude Code → Anthropic API traffic.
Modifies system prompts according to the active profile.
Streams responses back transparently.

Usage:
    ANTHROPIC_BASE_URL=http://localhost:8080 claude

The proxy forwards to the real Anthropic API, modifying only the
system prompt in outbound requests.
"""

import json
import asyncio
import errno
import logging
import os
import re
import ssl
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

# certifi provides a maintained CA bundle independent of the system trust store.
# Important for python.org installer Python on macOS, which ships without
# system-CA wiring — aiohttp's default SSL context fails cert verification until
# the user runs /Applications/Python*/Install Certificates.command. Using certifi
# makes the proxy work out of the box on every Python install that has it.
# Fall back to system defaults if certifi isn't available (developer running
# from source without the deps installed).
try:
    import certifi
    _SSL_CONTEXT: "ssl.SSLContext | None" = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

# Ensure imports work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from parser import parse_system_prompt, apply_profile, rebuild_system_prompt, dump_tree
from profiles import load_active_profile, get_active_profile, load_profile
from refusal import detect_refusal, all_refusals
from reminders import classify_reminder
from tool_scrub import scrub_tool_descriptions
from lanes import detect_lane
from rewrite_terminal import (
    RewriteTerminalState,
    Upstream2Relay,
    build_reissue_body,
    DEFAULT_RESET_TURN_FRAMING,
)

# Config
ANTHROPIC_API = "https://api.anthropic.com"
HOST = "127.0.0.1"
PORT = int(os.environ.get("CCORAL_PORT", 8080))
PROFILE_OVERRIDE = os.environ.get("CCORAL_PROFILE")  # Per-instance profile

# Room-mode capture sinks (in precedence order):
#   CCORAL_RESPONSE_FIFO  — POSIX FIFO created by the orchestrator. Preferred:
#                            structured push, no polling, instant turn signal.
#   CCORAL_RESPONSE_JSONL — append-only JSONL file. Fallback for platforms
#                            where mkfifo is unsupported (e.g. some macOS
#                            sandboxes); orchestrator polls with size cursor.
#   CCORAL_RESPONSE_FILE  — legacy single-text-file write. Deprecated but
#                            preserved for non-room callers and Phase 1
#                            back-compat. New deployments should migrate.
RESPONSE_FIFO = os.environ.get("CCORAL_RESPONSE_FIFO")
RESPONSE_JSONL = os.environ.get("CCORAL_RESPONSE_JSONL")
RESPONSE_FILE = os.environ.get("CCORAL_RESPONSE_FILE")
LOG_DIR = Path.home() / ".ccoral" / "logs"
LOG_REQUESTS = os.environ.get("CCORAL_LOG", "1") == "1"
VERBOSE = os.environ.get("CCORAL_VERBOSE", "0") == "1"

# Logging
logging.basicConfig(
    level=logging.DEBUG if VERBOSE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ccoral")


# Compiled regex for matching system-reminder tags. Group 1 captures the
# inner content so the classifier can decide whether to strip or preserve.
# Trailing whitespace is in group 2 so a strip absorbs it cleanly.
_SYSTEM_REMINDER_RE = re.compile(
    r'<system-reminder>(.*?)</system-reminder>(\s*)', re.DOTALL
)


def _smart_strip_reminders(text: str) -> tuple[str, int]:
    """Apply content-aware reminder stripping to a text string.

    Each `<system-reminder>...</system-reminder>` block is classified by
    `reminders.classify_reminder()`; nags get stripped, functional content
    (deferred-tools list, skills list, MCP server instructions, hook
    outputs, IDE context) is preserved, and unknown shapes default to
    preserved (false-preserve is safer than false-strip).

    Returns:
        (new_text, strip_count) where strip_count is the number of
        reminder blocks actually stripped (preserves don't count).
    """
    strips = [0]

    def _cb(match: "re.Match") -> str:
        inner = match.group(1)
        decision, _label = classify_reminder(inner)
        if decision == "strip":
            strips[0] += 1
            return ""  # consume the block AND its trailing whitespace
        return match.group(0)  # preserve untouched

    new_text = _SYSTEM_REMINDER_RE.sub(_cb, text)
    return new_text, strips[0]


def model_tier(model: str | None) -> str:
    """Return one of: 'opus', 'sonnet', 'haiku', 'unknown'.

    Robust to dated and routing-slug variants like 'claude-opus-4-7[1m]' or
    'claude-haiku-4-5-20251001'.
    """
    m = (model or "").lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    return "unknown"


def _iso8601_utc_now() -> str:
    """Stable UTC ISO-8601 with trailing Z. Matches what the orchestrator's
    transcript expects in Phase 3."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _should_emit_turn_record(text: str, model: str | None) -> bool:
    """Skip rules for room-mode turn capture.

    Identical to the legacy RESPONSE_FILE filter but factored out so it can
    be unit-tested in isolation: skip haiku tier, skip pure-JSON titles,
    skip <20 char fragments. Empty text is also skipped (the legacy code
    was structured to short-circuit on falsy `captured_text`).
    """
    if not text:
        return False
    if model_tier(model) == "haiku":
        return False
    if len(text) < 20:
        return False
    if text.startswith("{") and text.endswith("}"):
        return False
    return True


# Rate-limit FIFO ENXIO warnings to once per minute. ENXIO means "FIFO open
# for write but no reader on the other side" — a transient state during
# orchestrator setup or shutdown that would otherwise spam the log.
_FIFO_ENXIO_LOCK = threading.Lock()
_FIFO_ENXIO_LAST_WARN: float = 0.0
_FIFO_ENXIO_DROPPED: int = 0


def _emit_turn_record(record: dict) -> None:
    """Dispatch a completed-turn JSON record to whichever sink is configured.

    Precedence: FIFO > JSONL > legacy RESPONSE_FILE. The dispatcher tries
    each in order; legacy RESPONSE_FILE writes the bare `text` (back-compat
    for callers that haven't migrated). Never raises — failures are logged
    so a broken sink can't take down the request thread.
    """
    global _FIFO_ENXIO_LAST_WARN, _FIFO_ENXIO_DROPPED

    payload = json.dumps(record, ensure_ascii=False) + "\n"

    if RESPONSE_FIFO:
        # Non-blocking write so a stalled orchestrator can never wedge a
        # request thread. ENXIO ("no reader") is the documented errno when
        # a FIFO is opened O_WRONLY|O_NONBLOCK with no peer attached. We
        # rate-limit the warning rather than spam.
        try:
            fd = os.open(RESPONSE_FIFO, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return
        except OSError as e:
            if e.errno == errno.ENXIO:
                import time as _t
                with _FIFO_ENXIO_LOCK:
                    _FIFO_ENXIO_DROPPED += 1
                    now = _t.time()
                    if now - _FIFO_ENXIO_LAST_WARN >= 60.0:
                        log.warning(
                            f"FIFO {RESPONSE_FIFO} has no reader; dropped "
                            f"{_FIFO_ENXIO_DROPPED} record(s) since last warning"
                        )
                        _FIFO_ENXIO_LAST_WARN = now
                        _FIFO_ENXIO_DROPPED = 0
                return
            log.error(f"FIFO write failed ({RESPONSE_FIFO}): {e}")
            return

    if RESPONSE_JSONL:
        # O_APPEND on POSIX guarantees concurrent append safety up to
        # PIPE_BUF (~4KB) per write; turn records are typically a few
        # hundred bytes to a few KB. fsync skipped — orchestrator polls
        # size and the kernel will flush before the next request anyway.
        try:
            fd = os.open(
                RESPONSE_JSONL,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o644,
            )
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return
        except OSError as e:
            log.error(f"JSONL append failed ({RESPONSE_JSONL}): {e}")
            return

    if RESPONSE_FILE:
        # Legacy single-text-file path. Back-compat for any caller that
        # hasn't migrated to FIFO/JSONL. Writes only the bare text — the
        # structured fields are dropped on this path by design.
        try:
            Path(RESPONSE_FILE).write_text(record.get("text", ""))
            log.info(
                f"Room capture (legacy): wrote "
                f"{len(record.get('text', ''))} chars to {RESPONSE_FILE}"
            )
        except Exception as e:
            log.error(f"Room capture failed: {e}")


def strip_message_tags(body: dict, profile: dict) -> int:
    """
    Smart-strip <system-reminder> tags from the messages array, across all
    roles. Functional reminders are preserved; behavioral nags are stripped.

    Why smart and not blanket
    --------------------------
    Earlier versions of this function stripped every reminder. That broke
    real features in CC 2.1.x: the deferred-tools list (kills ToolSearch),
    the skills list (kills the Skill tool), MCP Server Instructions (kills
    MCP usage guidance), and SessionStart/UserPromptSubmit hook context
    (kills cross-session memory injections from claude-mem and similar).
    Empirical check on a captured 279-msg Opus 4.7 dump found 32 reminders
    of which only 9 were actual nags — 23 were functional content the
    model needs.

    Each `<system-reminder>...</system-reminder>` block is now classified
    by `reminders.classify_reminder()`:
      - functional content (hooks, deferred tools, skills, MCP, IDE
        context) → preserved
      - behavioral nags (task-tool nag, mode reminders, "remember to..."
        prods that change per-request and trash the cache) → stripped
      - unknown openers → preserved by default (false-preserve > false-strip)

    Walks user AND assistant messages. Cross-role coverage closes the leak
    where reminder-shaped fragments echoed by the model (e.g., quoting tool
    output in its own response) propagate into compaction summaries.

    Block-type filter:
      - text              → strip (both roles)
      - tool_result       → strip nested content (user-side only in practice)
      - tool_use          → skipped (structured JSON input, no free-text
                            field where a reminder could meaningfully live)
      - thinking          → skipped — protocol constraint, not preference.
                            Anthropic's API requires thinking blocks to be
                            replayed UNCHANGED in multi-turn conversations
                            using tool use. The full reasoning is encrypted
                            in the `signature` field; the `thinking` text
                            is empty by default (`display: omitted` on
                            Opus 4.7+) or a server-summarized excerpt.
                            Editing thinking text invalidates the signature
                            (API rejects on replay). Dropping the block
                            breaks tool_use → tool_result continuity. The
                            "strip everywhere" principle still holds, but
                            the safe enforcement point for thinking content
                            is the conversation summarizer's prompt
                            (lane-router work, Phase 5) — anything the
                            summarizer extracts from thinking gets sanitized
                            before it lands in compaction history.
      - redacted_thinking → skipped — same protocol constraint. Encrypted
                            content lives in the `data` field; block must be
                            replayed unchanged. Per Anthropic docs:
                            "Filtering on block.type == 'thinking' alone
                            silently drops redacted_thinking blocks and
                            breaks the multi-turn protocol."
      - image / other     → skipped

    Returns the number of tags stripped.

    Refs:
      - https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking
        (replay rules, signature semantics, redacted_thinking handling)
    """
    preserve = set(profile.get("preserve", []))

    # Don't strip if passthrough or explicitly preserving system_reminder
    if "all" in preserve or "system_reminder" in preserve:
        return 0

    messages = body.get("messages", [])
    total_stripped = 0

    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            cleaned, count = _smart_strip_reminders(content)
            if count:
                # API rejects empty text — use whitespace as placeholder
                msg["content"] = cleaned if cleaned.strip() else "."
                total_stripped += count
        elif isinstance(content, list):
            # Content blocks. Three shapes we care about:
            #   {"type": "text", "text": "..."}           — text from either role
            #   {"type": "tool_result", "content": ...}   — user-side tool output
            #     returned to the model. `content` is usually a string,
            #     occasionally a list of sub-blocks [{"type": "text", ...}, ...].
            #   {"type": "tool_use" | "thinking" | ...}   — skipped, see docstring.
            # Reminders get injected into text/tool_result shapes. v1 of this
            # function only handled type=="text" and only on user messages,
            # leaving (a) tool_result reminders unstripped and (b) any
            # assistant-side reminder echo unstripped — both fixed now.
            blocks_to_remove = []
            for i, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if not isinstance(text, str):
                        continue
                    cleaned, count = _smart_strip_reminders(text)
                    if count:
                        if cleaned.strip():
                            block["text"] = cleaned
                        else:
                            # Mark empty blocks for removal
                            blocks_to_remove.append(i)
                        total_stripped += count
                elif btype == "tool_result":
                    c = block.get("content")
                    if isinstance(c, str):
                        cleaned, count = _smart_strip_reminders(c)
                        if count:
                            # tool_result must have non-empty content — "."
                            # placeholder if the reminder was the whole body
                            block["content"] = cleaned if cleaned.strip() else "."
                            total_stripped += count
                    elif isinstance(c, list):
                        for sub in c:
                            if not isinstance(sub, dict):
                                continue
                            if sub.get("type") == "text":
                                t = sub.get("text", "")
                                if not isinstance(t, str):
                                    continue
                                cleaned, count = _smart_strip_reminders(t)
                                if count:
                                    sub["text"] = cleaned if cleaned.strip() else "."
                                    total_stripped += count
            # Remove empty blocks in reverse order to preserve indices
            for i in reversed(blocks_to_remove):
                content.pop(i)
            # If all text blocks were removed, keep at least one with whitespace
            if not content:
                msg["content"] = "."

    return total_stripped


def rotate_logs(max_days: int = 14):
    """Delete request-log and proxy-stdout log files older than max_days."""
    if not LOG_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=max_days)
    for pattern in ("ccoral-*.jsonl", "proxy-*.log"):
        for logfile in LOG_DIR.glob(pattern):
            try:
                mtime = datetime.fromtimestamp(logfile.stat().st_mtime)
            except FileNotFoundError:
                continue
            if mtime < cutoff:
                log.info(f"Rotating old log: {logfile.name}")
                try:
                    logfile.unlink()
                except OSError as e:
                    log.warning(f"Failed to rotate {logfile.name}: {e}")


def log_request(entry: dict):
    """Log request/response to JSONL file."""
    if not LOG_REQUESTS:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"ccoral-{datetime.now():%Y-%m-%d}.jsonl"
    with open(logfile, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def apply_replacements(text: str, replacements: dict) -> str:
    """Apply find/replace pairs to text. Case-sensitive."""
    for find, replace in replacements.items():
        text = text.replace(find, replace)
    return text


def apply_replacements_to_tools(tools: list, replacements: dict) -> list:
    """Apply replacements to tool descriptions only (not names or schemas)."""
    if not replacements or not tools:
        return tools
    for tool in tools:
        if isinstance(tool, dict):
            if "description" in tool and isinstance(tool["description"], str):
                tool["description"] = apply_replacements(tool["description"], replacements)
            if "custom" in tool and isinstance(tool["custom"], dict):
                if "description" in tool["custom"] and isinstance(tool["custom"]["description"], str):
                    tool["custom"]["description"] = apply_replacements(tool["custom"]["description"], replacements)
    return tools


def modify_request_body(body: dict, profile: dict) -> dict:
    """Apply profile to the request body's system prompt."""
    replacements = profile.get("replacements", {})

    system = body.get("system")
    if system is None:
        # No system prompt — inject one from the profile
        inject = profile.get("inject", "").strip()
        if inject:
            body["system"] = [{"type": "text", "text": inject}]
        return body

    # Handle string system prompts (some internal calls use plain strings)
    was_string = isinstance(system, str)
    if was_string:
        system = [{"type": "text", "text": system}]

    # Parse → apply profile → rebuild
    blocks = parse_system_prompt(system)

    if VERBOSE:
        log.debug("=== BEFORE ===")
        log.debug(dump_tree(blocks))

    blocks = apply_profile(blocks, profile)

    if VERBOSE:
        log.debug("=== AFTER ===")
        log.debug(dump_tree(blocks))

    body["system"] = rebuild_system_prompt(blocks)

    # Strip <system-reminder> tags from messages
    tags_stripped = strip_message_tags(body, profile)
    if tags_stripped:
        log.info(f"Stripped {tags_stripped} <system-reminder> tag(s) from messages")

    # Remove tools entirely if profile requests it (clean room)
    if profile.get("strip_tools", False) and "tools" in body:
        tool_count = len(body["tools"])
        del body["tools"]
        log.info(f"Stripped all {tool_count} tools (clean room)")
    # Or just strip tool descriptions to save tokens
    elif profile.get("strip_tool_descriptions", False) and "tools" in body:
        orig_tool_chars = sum(len(t.get("description", "")) for t in body["tools"])
        for tool in body["tools"]:
            if "description" in tool:
                tool["description"] = tool["name"]
        new_tool_chars = sum(len(t.get("description", "")) for t in body["tools"])
        log.info(f"Tool descriptions: {orig_tool_chars} → {new_tool_chars} chars ({len(body['tools'])} tools)")

    # Apply text replacements to system prompt content
    if replacements:
        for block in body["system"]:
            if isinstance(block, dict) and "text" in block:
                block["text"] = apply_replacements(block["text"], replacements)

    # Apply replacements to tool descriptions
    if replacements and "tools" in body:
        body["tools"] = apply_replacements_to_tools(body["tools"], replacements)

    if replacements:
        log.info(f"Applied {len(replacements)} text replacement(s)")

    # Phase 4: scrub behavioral fearmongering from tool descriptions.
    # Default-on for permissive profiles (apply_to_subagents implies
    # tool_scrub_default unless explicitly overridden via tool_scrub_default:
    # false). Profile-supplied tool_scrub_patterns are applied additively.
    # See tool_scrub.py and INJECT-FRAMING.md § 4 for the rationale.
    if "tools" in body and not profile.get("strip_tools", False):
        scrub_tool_descriptions(body["tools"], profile, log=log)

    # Log size reduction
    orig_size = sum(len(b.text) for b in parse_system_prompt(system))
    new_size = sum(len(b.get("text", "")) for b in body["system"])
    log.info(f"System prompt: {orig_size} → {new_size} chars ({100 - (new_size * 100 // max(orig_size, 1))}% reduction)")

    return body


async def handle_messages(request: web.Request) -> web.StreamResponse:
    """Handle /v1/messages — the main Claude API endpoint."""

    # Timing instrumentation — measures where latency is.
    # t_req_recv:   CC → CCORAL body received
    # t_upstream_connected:  CCORAL → Anthropic POST accepted (TTFB from API perspective)
    # t_first_chunk: first SSE chunk arrived from Anthropic (model started emitting)
    # t_last_chunk:  last SSE chunk written to CC (full response forwarded)
    # Each big gap tells a different story: a big t_first_chunk gap means Anthropic
    # took time to process the request; a big t_last_chunk means the model output
    # itself was long. CCORAL overhead = (t_req_recv → t_upstream_connected) and
    # between-chunk stalls on our side.
    import time as _time
    t_req_recv = _time.perf_counter()

    # Generate per-request id at entry so room mode (Phase 2 turn records)
    # can correlate orchestrator-side events with proxy-side state. Cheap
    # (uuid4) and threaded through downstream emit so we don't have to
    # re-derive it later.
    request_id = str(uuid.uuid4())

    # Read request body
    raw_body = await request.read()

    # CRITICAL: json.loads on ~1.5 MB bodies blocks the asyncio event loop for
    # 100-300 ms. Under burst traffic from Claude Code (multiple rapid tool
    # results), this causes Send-Q buildup and apparent hangs. Offload to the
    # default executor (thread pool) so the event loop keeps servicing other
    # incoming requests.
    loop = asyncio.get_running_loop()
    body = await loop.run_in_executor(None, json.loads, raw_body)

    # Debug: dump raw incoming body BEFORE any modification.
    # Previously this was a synchronous file write of up to 1.5 MB on the
    # event loop, which could pause everything for tens to hundreds of ms on
    # a contended disk. Run the whole write in the executor.
    # Resolve profile name *before* the dump so concurrent sessions on
    # different profiles don't clobber each other's dump files.
    _dump_profile = PROFILE_OVERRIDE or get_active_profile(port=PORT) or "noprofile"
    _dump_model = body.get("model", "unknown")[:10]
    def _write_raw_dump() -> None:
        raw_dump = Path.home() / ".ccoral" / "logs" / f"raw-{_dump_profile}-{_dump_model}.json"
        try:
            with open(raw_dump, "w") as f:
                f.write(raw_body.decode("utf-8", errors="replace"))
        except Exception:
            pass
    # Fire-and-forget: schedule but don't block the request on the dump.
    # run_in_executor returns a Future which we discard — the work is already
    # scheduled on the thread pool. Do NOT wrap in asyncio.create_task — that
    # expects a coroutine and raises TypeError on a Future, returning a 500.
    loop.run_in_executor(None, _write_raw_dump)

    # Load profile — env override takes precedence; otherwise fall back to
    # the per-port active_profile.<PORT> file (if present), then the global
    # active_profile file. PORT is set at module import from CCORAL_PORT.
    if PROFILE_OVERRIDE:
        profile = load_profile(PROFILE_OVERRIDE)
        profile_name = PROFILE_OVERRIDE
    else:
        profile = load_active_profile(port=PORT)
        profile_name = get_active_profile(port=PORT)

    modified = False
    is_utility = body.get("max_tokens", 9999) <= 1
    model = body.get("model", "")
    tier = model_tier(model)
    is_haiku = tier == "haiku"
    if tier == "unknown" and model:
        log.warning(f"Unknown model tier for: {model!r} — treated as main-tier")
    haiku_inject = profile.get("haiku_inject") if profile else None
    replacements = profile.get("replacements", {}) if profile else {}

    # Phase 5: positive lane identification by system-prompt fingerprint.
    # Used for routing override (lane=main_worker beats size-bucket fallback)
    # and for log visibility. Verb implementations (per profile.lane_policy)
    # land in later commits; Phase 5 ships the router only.
    lane = detect_lane(body.get("system"), model=model)

    # Measure original system prompt size
    orig_system = body.get("system")
    if isinstance(orig_system, str):
        orig_size = len(orig_system)
    elif isinstance(orig_system, list):
        orig_size = sum(len(b.get("text", "") if isinstance(b, dict) else str(b)) for b in (orig_system or []))
    else:
        orig_size = 0

    # Threshold: main conversation has the full ~27K system prompt
    # CC 2.1.138 sizing: main convo system prompt 30K-35K+ chars; subagent
    # 5K-12K typical, can spike to 15K-18K with heavy CLAUDE.md or
    # system-reminders. 22K threshold gives clean separation.
    # Kept as fallback for unknown-lane cases; lane=main_worker overrides
    # this even when size is below threshold (Phase 5 routing rule).
    SUBAGENT_THRESHOLD = 22000

    if profile and is_utility:
        # Utility call (counting, etc.) — skip everything
        log.info(f"Profile: {profile_name} (lane={lane}, utility call, max_tokens={body.get('max_tokens')} — skipping)")

    elif profile and is_haiku:
        # Haiku call — one-liner identity only
        if haiku_inject:
            body["system"] = [{"type": "text", "text": haiku_inject}]
            modified = True
            log.info(f"Profile: {profile_name} (lane={lane}, haiku mini-inject {len(haiku_inject)} chars)")
        else:
            log.info(f"Profile: {profile_name} (lane={lane}, haiku, no haiku_inject — skipping)")

    elif (
        profile
        and orig_size > 0
        and orig_size < SUBAGENT_THRESHOLD
        and not profile.get("apply_to_subagents", False)
        # Phase 5 override: positive main_worker ID beats size bucket. A
        # main_worker call that came in below threshold (rare — would mean a
        # heavily-trimmed system prompt) is still a worker, not a subagent.
        and lane != "main_worker"
    ):
        # Subagent — keep their system prompt, apply replacements + prepend one-liner.
        # Profiles that set `apply_to_subagents: true` skip this branch and fall
        # through to the worker pipeline below, which strips refusal-priming
        # sections (security_policy, executing_actions, action_safety, doing_tasks,
        # tool_usage, tone_style, agent_thread_notes) and replaces identity with
        # the full inject. Closes the subagent leak where Task-delegated calls
        # otherwise inherit default Claude Code behavioral instructions.
        log.info(f"Profile: {profile_name} (lane={lane}, subagent, orig_sys={orig_size} chars)")

        # Apply text replacements to existing system prompt
        if replacements:
            system_blocks = body.get("system", [])
            if isinstance(system_blocks, str):
                system_blocks = [{"type": "text", "text": system_blocks}]
                body["system"] = system_blocks
            for block in system_blocks:
                if isinstance(block, dict) and "text" in block:
                    block["text"] = apply_replacements(block["text"], replacements)

        # Apply replacements to tool descriptions
        if replacements and "tools" in body:
            body["tools"] = apply_replacements_to_tools(body["tools"], replacements)

        # Prepend one-liner identity
        if haiku_inject:
            identity_block = {"type": "text", "text": haiku_inject}
            system_blocks = body.get("system", [])
            if isinstance(system_blocks, list):
                # Insert after billing header (system[0]) if present
                insert_at = 0
                if system_blocks and isinstance(system_blocks[0], dict):
                    text0 = system_blocks[0].get("text", "")
                    if text0.startswith("x-anthropic-"):
                        insert_at = 1
                system_blocks.insert(insert_at, identity_block)
            body["system"] = system_blocks

        # Strip system-reminder tags from messages
        tags_stripped = strip_message_tags(body, profile)
        if tags_stripped:
            log.info(f"Stripped {tags_stripped} <system-reminder> tag(s) from messages")

        modified = True
        log.info(f"Subagent: replacements={len(replacements)}, identity={'yes' if haiku_inject else 'no'}")

    elif profile:
        # Main conversation — full persona injection.
        # Subagents land here too when their profile sets apply_to_subagents:true.
        if orig_size > 0 and orig_size < SUBAGENT_THRESHOLD:
            log.info(f"Profile: {profile_name} (lane={lane}, subagent, apply_to_subagents=true, orig_sys={orig_size} chars)")
        else:
            log.info(f"Profile: {profile_name} (lane={lane})")
        log.info(f"Original system prompt: {orig_size} chars, model: {model}")

        body = modify_request_body(body, profile)
        modified = True

        # Ensure system prompt is never empty
        system_result = body.get("system", [])
        if isinstance(system_result, list) and (not system_result or all(not b.get("text", "").strip() for b in system_result)):
            body["system"] = [{"type": "text", "text": profile.get("inject", ".").strip() or "."}]
            log.warning("System prompt was empty after processing — injected profile directly")

        final_system = body.get("system", [])
        final_size = sum(len(b.get("text", "")) for b in final_system) if isinstance(final_system, list) else len(str(final_system))

        log_request({
            "timestamp": datetime.now().isoformat(),
            "type": "request",
            "profile": profile_name,
            "model": body.get("model"),
            "orig_system_size": orig_size,
            "system_size": final_size,
            "message_count": len(body.get("messages", [])),
        })
    else:
        log.info("No active profile — passthrough")

    # Forward ALL headers except host (let aiohttp set it)
    forward_headers = dict(request.headers)
    forward_headers.pop("host", None)
    forward_headers.pop("Host", None)
    forward_headers.pop("content-length", None)
    forward_headers.pop("Content-Length", None)
    forward_headers.pop("transfer-encoding", None)
    forward_headers.pop("Transfer-Encoding", None)
    log.debug(f"Forwarding headers: {list(forward_headers.keys())}")

    target_url = f"{ANTHROPIC_API}{request.path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    # Debug: dump FULL outbound body (everything the API sees)
    # Include profile name so concurrent sessions don't clobber each other.
    debug_dump = Path.home() / ".ccoral" / "logs" / f"debug-{profile_name or 'noprofile'}-{body.get('model','unknown')[:10]}.json"
    try:
        with open(debug_dump, "w") as f:
            json.dump(body, f, indent=2, default=str)
        log.info(f"Debug payload dumped to {debug_dump}")
    except Exception as e:
        log.error(f"Debug dump failed: {e}")

    is_streaming = body.get("stream", False)

    # Use original raw bytes if unmodified, otherwise re-serialize preserving
    # key order. json.dumps on ~1.5 MB blocks the event loop ~200-500 ms —
    # offload to thread pool for the same reason as json.loads above.
    if not modified:
        outbound_body = raw_body
    else:
        def _serialize() -> bytes:
            return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode("utf-8")
        outbound_body = await loop.run_in_executor(None, _serialize)

    session = request.app["upstream_session"]
    t_upstream_start = _time.perf_counter()
    async with session.post(
        target_url,
        data=outbound_body,
        headers=forward_headers,
    ) as upstream:
        t_upstream_connected = _time.perf_counter()

        if is_streaming:
            # Stream SSE response back, optionally capturing text for room mode
            response = web.StreamResponse(
                status=upstream.status,
                headers={
                    "content-type": upstream.headers.get("content-type", "text/event-stream"),
                    "cache-control": "no-cache",
                },
            )

            # Forward relevant response headers
            for hdr in ["x-request-id", "request-id"]:
                if hdr in upstream.headers:
                    response.headers[hdr] = upstream.headers[hdr]

            await response.prepare(request)

            # Accumulate text blocks if we're capturing for room mode OR if
            # the active profile has refusal detection enabled (any non-
            # passthrough refusal_policy). Text capture is the prerequisite
            # for both — Phase 3a uses it for log-mode refusal observability.
            _refusal_policy = (
                (profile.get("refusal_policy", "passthrough") if profile else "passthrough")
            )
            _need_text_for_refusal = _refusal_policy != "passthrough"
            captured_text = [] if (
                RESPONSE_FIFO or RESPONSE_JSONL or RESPONSE_FILE
                or _need_text_for_refusal
            ) else None

            # ONE-SHOT SSE DUMP: if a marker file exists, dump full SSE of next
            # response to it, then delete the marker. Used for debugging stream
            # format changes (e.g. 4.6 → 4.7 thinking delta format).
            dump_marker = Path.home() / ".ccoral" / "logs" / "DUMP_NEXT_SSE"
            dump_target = Path.home() / ".ccoral" / "logs" / "sse-dump.txt"
            should_dump_full = dump_marker.exists()
            full_sse: list[bytes] = [] if should_dump_full else None
            if should_dump_full:
                try:
                    dump_marker.unlink()
                except Exception:
                    pass
                log.info(f"ONE-SHOT SSE dump armed -> {dump_target}")

            # Capture stop_reason + content_block_starts to detect "model stopped
            # without tool_use" freezes. Keep a rolling tail of the raw SSE stream
            # so if the user reports a freeze we can see exactly what came back.
            sse_tail: list[bytes] = []
            SSE_TAIL_MAX = 64 * 1024  # keep last 64KB of the stream
            sse_tail_bytes = 0
            block_starts: list[str] = []  # types of content_block_start
            seen_stop_reason: str | None = None

            # Phase 3b/3c: rewrite_terminal AND reset_turn modes wrap each
            # upstream chunk through a state machine that buffers index-0
            # text deltas until a refusal decision is made. Other modes
            # (passthrough, log) use the byte-passthrough fast path below
            # — zero added latency, byte-identical to upstream.
            _use_state_machine = (_refusal_policy in ("rewrite_terminal", "reset_turn"))
            _rewrite_state: "RewriteTerminalState | None" = (
                RewriteTerminalState(mode=_refusal_policy) if _use_state_machine else None
            )

            t_first_chunk = None
            bytes_streamed = 0
            async for chunk in upstream.content.iter_any():
                if t_first_chunk is None:
                    t_first_chunk = _time.perf_counter()

                # Phase 3b/3c: route the chunk through the state machine
                # before forwarding. The machine returns the bytes that
                # should reach the client (which may be the same as
                # `chunk`, may be a synthetic delta, or may be empty
                # while still buffering).
                if _rewrite_state is not None:
                    forward_bytes = _rewrite_state.feed_chunk(chunk)
                else:
                    forward_bytes = chunk

                if forward_bytes:
                    try:
                        await response.write(forward_bytes)
                    except aiohttp.ClientConnectionResetError:
                        # Client disconnected mid-stream — tear down
                        # cleanly so we don't leak the upstream socket.
                        log.info("Client disconnected mid-stream; aborting upstream")
                        upstream.close()
                        return response
                    bytes_streamed += len(forward_bytes)

                # Accumulate rolling tail of raw SSE (UPSTREAM bytes,
                # not forwarded — for post-hoc inspection of what the
                # API actually emitted, including any preamble we hid).
                sse_tail.append(chunk)
                sse_tail_bytes += len(chunk)
                while sse_tail_bytes > SSE_TAIL_MAX and len(sse_tail) > 1:
                    dropped = sse_tail.pop(0)
                    sse_tail_bytes -= len(dropped)

                # One-shot full dump capture (also raw upstream)
                if full_sse is not None:
                    full_sse.append(chunk)

                # Cheap per-chunk inspection for block types + stop reasons.
                # Decode tolerantly (SSE lines may split across chunks) — the
                # "miss a block across a boundary" rate is acceptable because we
                # also have the raw tail if we need it.
                try:
                    cs = chunk.decode("utf-8", errors="ignore")
                    for line in cs.split("\n"):
                        if not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                        except (ValueError, json.JSONDecodeError):
                            continue
                        ev = data.get("type")
                        if ev == "content_block_start":
                            cb = (data.get("content_block") or {}).get("type")
                            if cb:
                                block_starts.append(cb)
                        elif ev == "message_delta":
                            sr = (data.get("delta") or {}).get("stop_reason")
                            if sr:
                                seen_stop_reason = sr
                        # Room-capture text deltas (existing behavior)
                        if captured_text is not None and ev == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                captured_text.append(delta.get("text", ""))
                except Exception:
                    pass

                # Phase 3c: pivot signal from the state machine. Stop
                # draining upstream1 immediately (the rest is irrelevant
                # — we're switching to upstream2). The actual upstream
                # close and re-issue happen after the chunk loop exits.
                if _rewrite_state is not None and _rewrite_state.pivot_requested:
                    log.info(
                        f"RESET_TURN pivot requested "
                        f"(profile={profile_name}, model={body.get('model','?')}, "
                        f"label={_rewrite_state.intercepted_label}); "
                        f"closing upstream1"
                    )
                    upstream.close()
                    break

            # Phase 3b: flush any pending state-machine buffer at stream
            # end (in case upstream cut off mid-buffer with no
            # content_block_stop — defensive flush of accumulated text
            # without applying refusal stripping).
            if _rewrite_state is not None:
                tail = _rewrite_state.finalize()
                if tail:
                    try:
                        await response.write(tail)
                    except aiohttp.ClientConnectionResetError:
                        pass
                    else:
                        bytes_streamed += len(tail)
                if _rewrite_state.intercepted and not _rewrite_state.pivot_requested:
                    log.warning(
                        f"REWRITE_TERMINAL intercepted refusal "
                        f"(profile={profile_name}, model={body.get('model','?')}, "
                        f"label={_rewrite_state.intercepted_label}, "
                        f"chars_removed={_rewrite_state.intercepted_chars})"
                    )

            # Phase 3c: reset_turn pivot. The state machine signaled a
            # refusal in reset_turn mode; upstream1 is already closed
            # and a synthetic content_block_stop has been emitted to the
            # client. Now re-issue with the operator-scope framing
            # prepended and relay upstream2 with renumbered indices.
            #
            # Per-turn retry cap: hard 1 reissue per request. If
            # upstream2 also refuses, we let the user see it (better
            # than infinite retry).
            if (
                _rewrite_state is not None
                and _rewrite_state.pivot_requested
                and _refusal_policy == "reset_turn"
            ):
                framing = (
                    profile.get("reset_turn_framing")
                    if profile and profile.get("reset_turn_framing")
                    else DEFAULT_RESET_TURN_FRAMING
                )
                # Build the modified body and serialize it. deepcopy is
                # done inside build_reissue_body so the original stays
                # intact for logging.
                body_reissue = build_reissue_body(body, framing=framing)
                def _serialize_reissue() -> bytes:
                    return json.dumps(
                        body_reissue, ensure_ascii=False, separators=(',', ':')
                    ).encode("utf-8")
                outbound_reissue = await loop.run_in_executor(None, _serialize_reissue)

                # Relay upstream2 with index renumbering. Compute the
                # offset from the indices the state machine saw the
                # client open: max + 1 puts upstream2 cleanly past the
                # already-closed text block 0.
                seen = _rewrite_state.seen_block_indices
                index_offset = (max(seen) + 1) if seen else 1
                relay = Upstream2Relay(index_offset=index_offset)

                t_reissue_start = _time.perf_counter()
                log.info(
                    f"RESET_TURN reissuing with operator-scope framing "
                    f"(framing={len(framing)} chars, index_offset={index_offset}, "
                    f"body_size={len(outbound_reissue)})"
                )
                async with session.post(
                    target_url,
                    data=outbound_reissue,
                    headers=forward_headers,
                ) as upstream2:
                    log.info(
                        f"RESET_TURN upstream2 connected (status={upstream2.status})"
                    )
                    async for chunk2 in upstream2.content.iter_any():
                        relayed = relay.feed_chunk(chunk2)
                        if relayed:
                            try:
                                await response.write(relayed)
                            except aiohttp.ClientConnectionResetError:
                                log.info(
                                    "Client disconnected during reset_turn "
                                    "relay; aborting upstream2"
                                )
                                upstream2.close()
                                return response
                            bytes_streamed += len(relayed)
                        # Append upstream2 raw bytes to the inspection
                        # tail too so post-hoc analysis sees the full
                        # picture.
                        sse_tail.append(chunk2)
                        sse_tail_bytes += len(chunk2)
                        while sse_tail_bytes > SSE_TAIL_MAX and len(sse_tail) > 1:
                            dropped = sse_tail.pop(0)
                            sse_tail_bytes -= len(dropped)
                        if full_sse is not None:
                            full_sse.append(chunk2)

                t_reissue_done = _time.perf_counter()
                log.warning(
                    f"RESET_TURN intercepted refusal + reissued "
                    f"(profile={profile_name}, model={body.get('model','?')}, "
                    f"label={_rewrite_state.intercepted_label}, "
                    f"upstream2_events={relay.events_forwarded}, "
                    f"upstream2_ms={int((t_reissue_done - t_reissue_start) * 1000)})"
                )

            # Phase 2: structured per-turn capture for room relay.
            #
            # Skip rules (haiku tier, pure-JSON titles, <20 chars) live in
            # `_should_emit_turn_record` so they're testable in isolation.
            # When skipped we log.debug for visibility but do NOT write a
            # record — the orchestrator must never see noise turns.
            #
            # Sink precedence (FIFO > JSONL > legacy RESPONSE_FILE) is
            # handled by `_emit_turn_record`. The legacy path keeps writing
            # plain text for back-compat with non-room callers; new
            # deployments use FIFO/JSONL and get the full structured record.
            if (
                (RESPONSE_FIFO or RESPONSE_JSONL or RESPONSE_FILE)
                and captured_text is not None
                and captured_text
            ):
                full_text = "".join(captured_text).strip()
                model = body.get("model", "")
                if _should_emit_turn_record(full_text, model):
                    record = {
                        "ts": _iso8601_utc_now(),
                        "model": model,
                        "stop_reason": seen_stop_reason,
                        "text": full_text,
                        "lane": lane,
                        "request_id": request_id,
                    }
                    _emit_turn_record(record)
                elif full_text:
                    log.debug(
                        f"Room capture skipped: "
                        f"haiku={model_tier(model) == 'haiku'} "
                        f"json={full_text.startswith('{') and full_text.endswith('}')} "
                        f"short={len(full_text) < 20} len={len(full_text)}"
                    )

            # Phase 3a: refusal observability. When the active profile has a
            # non-passthrough refusal_policy, scan the captured response text
            # for refusal idioms and log matches. Phase 3a only implements
            # `log` mode; `rewrite_terminal` and `reset_turn` interception
            # modes are deferred to a follow-up that reorganizes the SSE loop
            # to support pre-emit buffering.
            if (
                _refusal_policy in ("log", "rewrite_terminal", "reset_turn")
                and captured_text is not None
                and captured_text
            ):
                full_text = "".join(captured_text).strip()
                # Skip short non-text responses (status pings, JSON tool
                # results echoed back, etc.). Refusals are usually a few
                # sentences at minimum.
                if len(full_text) >= 20 and not (
                    full_text.startswith("{") and full_text.endswith("}")
                ):
                    matches = all_refusals(full_text)
                    if matches:
                        labels = ",".join(m[0] for m in matches)
                        first = matches[0]
                        log.warning(
                            f"REFUSAL detected (policy={_refusal_policy}, "
                            f"profile={profile_name}, model={body.get('model','?')}, "
                            f"matches=[{labels}]): {first[2]!r} at offset {first[1]}"
                        )
                        # Write structured event for offline analysis.
                        try:
                            ref_log = Path.home() / ".ccoral" / "logs" / "refusals.jsonl"
                            ref_log.parent.mkdir(parents=True, exist_ok=True)
                            with open(ref_log, "a") as rf:
                                rf.write(json.dumps({
                                    "timestamp": datetime.now().isoformat(),
                                    "profile": profile_name,
                                    "model": body.get("model"),
                                    "policy": _refusal_policy,
                                    "matches": [
                                        {"label": m[0], "offset": m[1], "text": m[2]}
                                        for m in matches
                                    ],
                                    "preview": full_text[:300],
                                }) + "\n")
                        except Exception as e:
                            log.error(f"Refusal log failed: {e}")
                        # NOTE: Phase 3a is detection-only. rewrite_terminal
                        # and reset_turn would intervene before the response
                        # was forwarded; they need an SSE-buffering refactor
                        # which is the subject of the next commit.

            t_last_chunk = _time.perf_counter()

            # Write one-shot full SSE dump if armed
            if full_sse is not None:
                try:
                    dump_target.write_bytes(b"".join(full_sse))
                    log.info(f"ONE-SHOT SSE dump written: {dump_target} ({sum(len(c) for c in full_sse)} bytes)")
                except Exception as e:
                    log.error(f"SSE dump failed: {e}")

            # Emit timing log. Only for "main conversation" calls (not haiku/utility).
            if modified and not is_utility and not is_haiku:
                ms_body_read = int((t_upstream_start - t_req_recv) * 1000)
                ms_connect = int((t_upstream_connected - t_upstream_start) * 1000)
                ms_ttfb = int(((t_first_chunk or t_upstream_connected) - t_upstream_connected) * 1000)
                ms_stream = int((t_last_chunk - (t_first_chunk or t_upstream_connected)) * 1000)
                ms_total = int((t_last_chunk - t_req_recv) * 1000)
                log.info(
                    f"timing total={ms_total}ms "
                    f"prep={ms_body_read}ms connect={ms_connect}ms "
                    f"ttfb={ms_ttfb}ms stream={ms_stream}ms "
                    f"bytes_out={bytes_streamed} msgs={len(body.get('messages', []))}"
                )
                try:
                    timing_log = Path.home() / ".ccoral" / "logs" / "timings.jsonl"
                    with open(timing_log, "a") as tf:
                        tf.write(json.dumps({
                            "ts": datetime.now().isoformat(),
                            "ms_total": ms_total,
                            "ms_body_read": ms_body_read,
                            "ms_connect": ms_connect,
                            "ms_ttfb": ms_ttfb,
                            "ms_stream": ms_stream,
                            "bytes_out": bytes_streamed,
                            "msgs": len(body.get("messages", [])),
                            "status": upstream.status,
                            "block_starts": block_starts,
                            "stop_reason": seen_stop_reason,
                            "has_tool_use": "tool_use" in block_starts,
                        }) + "\n")
                except Exception:
                    pass

                # Extra red-flag: model ended the turn with no tool_use.
                # With Claude Code, end_turn + no tool_use means the model chose
                # to stop talking without asking to run anything — which the user
                # experiences as "froze". Log it prominently + dump the SSE tail
                # so we can see what text/thinking came back.
                if seen_stop_reason == "end_turn" and "tool_use" not in block_starts:
                    try:
                        freeze_dump = Path.home() / ".ccoral" / "logs" / "end-turn-no-tool.jsonl"
                        tail_bytes = b"".join(sse_tail)
                        with open(freeze_dump, "a") as ff:
                            ff.write(json.dumps({
                                "ts": datetime.now().isoformat(),
                                "msgs": len(body.get("messages", [])),
                                "block_starts": block_starts,
                                "bytes_out": bytes_streamed,
                                "tail_snippet": tail_bytes[-8000:].decode("utf-8", errors="replace"),
                            }) + "\n")
                        log.warning(
                            f"end_turn without tool_use at msgs={len(body.get('messages', []))}; "
                            f"blocks={block_starts}"
                        )
                    except Exception:
                        pass

            await response.write_eof()
            return response
        else:
            # Non-streaming: read full response and forward
            resp_body = await upstream.read()
            return web.Response(
                status=upstream.status,
                body=resp_body,
                content_type=upstream.headers.get("content-type", "application/json"),
            )


async def handle_passthrough(request: web.Request) -> web.StreamResponse:
    """Pass through any non-messages endpoint unchanged."""
    raw_body = await request.read()

    forward_headers = dict(request.headers)
    forward_headers.pop("host", None)

    target_url = f"{ANTHROPIC_API}{request.path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    session = request.app["upstream_session"]
    async with session.request(
        request.method,
        target_url,
        data=raw_body if raw_body else None,
        headers=forward_headers,
    ) as upstream:
        resp_body = await upstream.read()
        return web.Response(
            status=upstream.status,
            body=resp_body,
            content_type=upstream.headers.get("content-type", "application/json"),
        )


def _build_upstream_connector() -> aiohttp.TCPConnector:
    """Build the aiohttp connector we use for upstream API calls.

    Centralized so the on_startup path and the watchdog-rebuild path can't drift.

    force_close=True: Anthropic's server closes idle keepalive connections
    before our keepalive_timeout fires, leaving sockets in CLOSE_WAIT on our
    side. aiohttp's pool can then hand that poisoned socket to a new request,
    which hangs forever — this was the "silent freeze" failure mode. Fresh
    connection per request costs ~100ms of TLS handshake but removes the class
    of bug entirely.

    ssl=_SSL_CONTEXT: uses certifi's CA bundle when available. See module-level
    comment on _SSL_CONTEXT for why.
    """
    kwargs: dict = {
        "limit": 20,
        "limit_per_host": 10,
        "force_close": True,
        "enable_cleanup_closed": True,
    }
    if _SSL_CONTEXT is not None:
        kwargs["ssl"] = _SSL_CONTEXT
    return aiohttp.TCPConnector(**kwargs)


async def on_startup(app):
    """Create a persistent HTTP session for upstream requests."""
    app["upstream_session"] = aiohttp.ClientSession(
        connector=_build_upstream_connector(),
        timeout=aiohttp.ClientTimeout(total=600, sock_connect=10, sock_read=300),
    )

    # Background sentinel: every 30s, count CLOSE_WAIT sockets owned by this
    # process. If any appear (meaning force_close didn't fully prevent them),
    # log and force a session rebuild. Cheap and self-healing.
    async def close_wait_watchdog():
        import subprocess
        pid = os.getpid()
        while True:
            try:
                await asyncio.sleep(30)
                result = subprocess.run(
                    ["ss", "-tan", "-p"],
                    capture_output=True, text=True, timeout=5,
                )
                # Count CLOSE-WAIT sockets owned by this process
                count = 0
                for line in result.stdout.splitlines():
                    if "CLOSE-WAIT" in line and f"pid={pid}" in line:
                        count += 1
                if count > 0:
                    log.warning(
                        f"close-wait watchdog: {count} poisoned sockets; rebuilding session"
                    )
                    try:
                        old_session = app["upstream_session"]
                        app["upstream_session"] = aiohttp.ClientSession(
                            connector=_build_upstream_connector(),
                            timeout=aiohttp.ClientTimeout(total=600, sock_connect=10, sock_read=300),
                        )
                        await old_session.close()
                    except Exception as e:
                        log.error(f"session rebuild failed: {e}")
            except Exception as e:
                log.debug(f"watchdog tick failed: {e}")

    app["watchdog_task"] = asyncio.create_task(close_wait_watchdog())

async def on_cleanup(app):
    """Close the persistent session on shutdown."""
    task = app.get("watchdog_task")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await app["upstream_session"].close()


def create_app() -> web.Application:
    """Create the CCORAL proxy application."""
    app = web.Application(client_max_size=50 * 1024 * 1024)  # 50MB max

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Messages endpoint — where the magic happens
    app.router.add_post("/v1/messages", handle_messages)

    # Everything else — passthrough
    app.router.add_route("*", "/{path:.*}", handle_passthrough)

    return app


def main():
    """Start the CCORAL proxy."""
    # Resolve which active-profile file actually wins for this daemon so the
    # banner can show "(port 8081)" vs "(global)". Env override > per-port
    # file > global file.
    per_port_name = get_active_profile(port=PORT) if not PROFILE_OVERRIDE else None
    global_name = get_active_profile() if not PROFILE_OVERRIDE else None
    if PROFILE_OVERRIDE:
        profile_display = f"{PROFILE_OVERRIDE} (locked via CCORAL_PROFILE)"
    elif per_port_name and per_port_name != global_name:
        profile_display = f"{per_port_name} (port {PORT})"
    elif per_port_name:
        profile_display = f"{per_port_name} (global)"
    else:
        profile_display = "(none — passthrough)"

    print(f"""
\033[33m┌─────────────────────────────────────────┐
│  🪸  CCORAL v2                          │
│  Claude Code Override & Augmentation    │
└─────────────────────────────────────────┘\033[0m

  Proxy:    http://{HOST}:{PORT}
  Target:   {ANTHROPIC_API}
  Profile:  {profile_display}
  Logging:  {LOG_DIR if LOG_REQUESTS else 'disabled'}

  Launch Claude Code with:
    \033[36mANTHROPIC_BASE_URL=http://{HOST}:{PORT} claude\033[0m

  Or:
    \033[36mccoral run\033[0m
    \033[36mccoral run vonnegut\033[0m        (locked to profile)
    \033[36mccoral run vonnegut 8081\033[0m   (custom port for multi-instance)
    \033[36mccoral start --port 8081\033[0m   (multi-instance daemon)
""")

    rotate_logs()
    app = create_app()
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
