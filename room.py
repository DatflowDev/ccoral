"""
CCORAL v2 — Room
==================

Multi-profile conversation room using tmux + file-based relay.

Two full Claude Code sessions run in tmux panes, each through its own
CCORAL proxy. Each session writes its responses to a file (via system
prompt instruction). A control loop watches those files and relays
messages between panes using tmux send-keys.

Usage (via CLI):
    ccoral room vonnegut leguin
    ccoral room vonnegut leguin "What do we owe each other?"
    ccoral room --resume last
"""

import errno
import json
import os
import select
import sys
import subprocess
import time
import yaml
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

# Ensure imports from ccoral dir
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from profiles import load_profile, list_profiles
# Phase 9 C4: cockpit owner is now the Textual app (room_app.RoomApp).
# room_app exposes the same module-level surface as the legacy
# room_control (set_user / set_speaker_display / render_transcript_line /
# render_help / read_command / enqueue_user_event / drain_user_events /
# warn_legacy_record), so call sites below stay unchanged. The legacy
# bespoke-cockpit module is renamed to room_control_legacy in C5 and
# kept as a one-release safety net behind --legacy-cockpit. See
# .plan/room-overhaul.md Phase 9 step 6 (line 237).
import room_app as room_control

# Colors
Y = "\033[33m"
C = "\033[36m"
W = "\033[1;37m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

# Filesystem locations (path constants — not user-tunable; they live outside
# RoomConfig because they're the same for every room on a given host).
# ROOM_DIR is the transient working dir for FIFO/JSONL turn channels and
# pager scratch files; the persistent per-room archive lives under
# ~/.ccoral/rooms/<id>/ (Task 3.2).
ROOM_DIR = Path("/tmp/ccoral-room")
ROOMS_ARCHIVE = Path.home() / ".ccoral" / "rooms"
TEMP_PROFILES_DIR = Path.home() / ".ccoral" / "profiles"

# Phase 2: arbiter loop tick. Short timeout on select(); the loop is push-driven
# (FIFO bytes wake us instantly), the timeout only governs how often we poll
# stdin via the cockpit's own line-editor and check stall expiry.
RELAY_SELECT_TIMEOUT = 0.25


# ───────────────────────────────────────────────────────────────────────────
# Phase 3: RoomConfig
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class RoomConfig:
    """Resolved-at-start configuration for one `ccoral room` invocation.

    Replaces the (now-deleted) module-level USER_NAME / BASE_PORT /
    TMUX_SESSION / BACKPRESSURE_* constants of Phases 1+2. Populated by
    the CLI dispatcher in `ccoral`; threaded through
    start_proxies/setup_tmux/relay_loop so two rooms can run side-by-side
    on different ports without env-var gymnastics.

    Defaults preserve pre-Phase-3 behavior exactly: `ccoral room blank blank`
    with no flags resolves to user=CASSIUS, port=8090, tmux session prefix
    "room", header-line envelopes, FIFO-or-JSONL channel auto-detect.
    """

    user_name: str = "CASSIUS"
    base_port: int = 8090
    tmux_session_prefix: str = "room"           # actual session: f"{prefix}-{profile}"
    turn_limit: int | None = None
    max_chars_per_turn: int | None = None
    backpressure_turns: int = 2
    backpressure_timeout_s: float = 60.0
    seed1: str | None = None
    seed2: str | None = None
    moderator: str | None = None                # profile name; optional 3rd pane
    moderator_cadence: int = 4
    channel: str = "auto"                       # auto | fifo | jsonl
    envelope_format: str = "header-line"        # header-line | legacy
    room_id: str | None = None                  # set at start; per-room state dir key
    # Phase 5 task 2: how many tail records of a resumed transcript to embed
    # in the prior-exchange system block. Defaults to 30 — the legacy
    # 10 was too short for substantive continuation. Overridden via
    # `--resume-tail <n>`.
    resume_tail: int = 30

    def session_for(self, profile: str) -> str:
        """tmux session name for a given profile in this room."""
        return f"{self.tmux_session_prefix}-{profile}"


# Phase 3 C3: alias shim retired. The module-level USER_NAME / BASE_PORT /
# TMUX_SESSION / BACKPRESSURE_* names lived here as a transitional
# courtesy while C1 threaded RoomConfig through every call site. They
# are now gone — RoomConfig is the single source of truth. Operators who
# need defaults read them from `RoomConfig()` directly.


def get_display_name(profile_name: str) -> str:
    return profile_name.upper()


# Phase 4: identity-neutral, operator-scope room addendum. Used when a
# profile does not author its own `room_addendum`. Framed per
# INJECT-FRAMING.md (operator-scope, positive instruction, no
# refusal-trigger linguistic cues, no tool-scope directives) and
# deliberately short — the profile's own `inject` is the voice anchor.
#
# Substitution variables filled at format time:
#   {OTHER} — display name of the other slot's profile (uppercased)
#   {USER}  — host/user display name (RoomConfig.user_name, default CASSIUS)
#
# Anti-patterns explicitly avoided:
#   - tmpfile-read instructions (Phase 2 dropped the tmpfile relay)
#   - tool-scope prohibitions (no-tools / no-files / no-markdown style):
#     those are tool-scope decisions handled by `preserve`,
#     `strip_tools`, `strip_tool_descriptions`, NOT voice instructions
#   - compliance-forcing suffixes (inert on Claude per arxiv 2605.02398)
DEFAULT_ROOM_ADDENDUM = (
    "## Room context (operator-set)\n"
    "You're in a live exchange with {OTHER} (another assistant). "
    "{USER} is the human host.\n"
    "Lines starting with \"[{OTHER}]\" are them. "
    "Lines starting with \"[{USER}]\" are the host.\n"
    "Reply naturally and stay in your own voice. The host may interject "
    "at any time."
)


def _resolve_room_addendum(base: dict, *, other_display: str, user_name: str) -> str:
    """Resolve the room addendum text for a single side of the room.

    Three behaviors per the Phase 4 contract (.plan/room-overhaul.md
    lines 325-331):
      - `room_addendum` absent           → use DEFAULT_ROOM_ADDENDUM
      - `room_addendum` set to non-empty → use that string verbatim
      - `room_addendum` set to ""        → return "" (no addendum)

    The empty-string case is honored even when `minimal: true` — the
    profile is responsible for its own room awareness in that case
    (typically baked into `inject`).
    """
    if "room_addendum" in base:
        custom = base["room_addendum"]
        if custom == "":
            return ""
        text = custom
    else:
        text = DEFAULT_ROOM_ADDENDUM
    return text.format(OTHER=other_display, USER=user_name)


# Internal metadata keys load_profile() stamps onto the dict (file path,
# resolved name) — never want these written into the temp YAML.
_LOAD_PROFILE_INTERNAL_KEYS = {"_path", "_name"}

# Keys we override on the temp profile. Anything else from the base
# profile passes through verbatim via dict(base).
_TEMP_PROFILE_OVERRIDES = {"name", "description", "inject", "room_addendum"}


def build_prior_exchange_block(messages: list, tail: int = 30) -> str:
    """Render a resumed transcript as a system-note block to be appended
    to the inject of each temp profile during a `--resume`.

    Operator-scope per INJECT-FRAMING.md: declarative, positive
    instruction ("Continue naturally"), no refusal-trigger cues, no
    compliance-forcing suffixes. The block sits at the END of the
    inject (after the addendum), so the receiving Claude reads its
    voice anchor first and the resumed history second — which matches
    how a human would re-enter a conversation.

    Records eligible for inclusion: anything with `text` and `name`
    (turns, host interjections via relay-meta, SYSTEM backpressure
    notes). Empty bodies are skipped. We render `[NAME] body` per line
    so the resulting block matches the cockpit transcript shape and
    the receiving Claude already-trained `[OTHER]` / `[USER]` prefixes
    from the Phase 4 addendum.

    Returns "" when there's nothing to include — caller treats that as
    "no prior exchange block needed" and leaves the inject alone.
    """
    if not messages:
        return ""
    eligible = [
        m for m in messages
        if (m.get("text") or "").strip() and (m.get("name") or "").strip()
    ]
    if not eligible:
        return ""
    if tail and tail > 0:
        eligible = eligible[-tail:]
    lines = ["## Prior exchange (resumed by host)",
             "The conversation below already happened. The host is resuming you.",
             "Continue naturally from where you left off — do not re-introduce or recap.",
             ""]
    for m in eligible:
        name = m["name"]
        text = m["text"].strip()
        # Multi-line bodies stay readable: the prefix sits on the first
        # line; subsequent lines are flowed in as-is. Same convention
        # the cockpit uses for transcript lines.
        first, *rest = text.splitlines() or [""]
        lines.append(f"[{name}] {first}")
        for r in rest:
            lines.append(r)
    return "\n".join(lines)


def create_room_profiles(profile1: str, profile2: str,
                         user_name: str | None = None,
                         prior_exchange: str | None = None) -> dict:
    """Create temporary profiles with room relay instructions baked into inject.

    Phase 4: the hardcoded English room-instructions block is gone.
    Each profile's room addendum comes from its own `room_addendum`
    field (or DEFAULT_ROOM_ADDENDUM when absent, or "" — which means no
    addendum at all).

    Phase 4 task 4: the temp profile is built as `dict(base)` minus the
    keys we override (`name`, `description`, `inject`, plus
    `room_addendum` which is consumed at this layer). Every other
    schema-defined field passes through automatically — including
    `apply_to_subagents`, `refusal_policy`, `reset_turn_framing`,
    `strip_tools`, `strip_tool_descriptions`, `replacements`,
    `haiku_inject`, `claude_md_name`, `lane_policy`,
    `tool_scrub_default`, `tool_scrub_patterns`, `minimal`, `strict`,
    and any future schema additions. No need to edit this function each
    time the schema grows.

    Phase 5: `prior_exchange`, when provided, is appended to the
    inject AFTER the addendum so the receiving Claude reads the
    resumed history as an operator-scope system note instead of
    receiving a chat-message dump in pane 1. Empty / None means no
    block — the cold-start path is unchanged.
    """
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    if user_name is None:
        user_name = RoomConfig().user_name

    temp_names = {}

    for self_name, other_name in [(profile1, profile2), (profile2, profile1)]:
        base = load_profile(self_name)
        if not base:
            print(f"Profile not found: {self_name}")
            available = [p["name"] for p in list_profiles()]
            print(f"Available: {', '.join(available)}")
            sys.exit(1)

        other_display = get_display_name(other_name)

        addendum = _resolve_room_addendum(
            base, other_display=other_display, user_name=user_name,
        )
        base_inject = base.get("inject", "") or ""
        if addendum:
            modified_inject = base_inject + "\n\n" + addendum
        else:
            modified_inject = base_inject
        # Phase 5: append the prior-exchange system block AFTER the
        # addendum. Order matters — the voice anchor (base inject) +
        # operator scope (addendum) must come before the resumed
        # transcript so the model parses identity first, then context.
        if prior_exchange:
            modified_inject = modified_inject + "\n\n" + prior_exchange

        # Pass-through copy: every key from base survives unless we
        # explicitly override or it's load_profile internal metadata.
        temp_profile = {
            k: v for k, v in base.items()
            if k not in _LOAD_PROFILE_INTERNAL_KEYS
            and k not in _TEMP_PROFILE_OVERRIDES
        }
        # Now apply the overrides.
        temp_profile["name"] = f"{self_name}-room"
        temp_profile["description"] = f"{base.get('description', '')} (room mode)"
        temp_profile["inject"] = modified_inject

        temp_path = TEMP_PROFILES_DIR / f"{self_name}-room.yaml"
        with open(temp_path, "w") as f:
            yaml.dump(temp_profile, f, default_flow_style=False, allow_unicode=True)

        temp_names[self_name] = f"{self_name}-room"

    return temp_names


def cleanup_room_profiles(profile1: str, profile2: str):
    """Remove temporary room profiles."""
    for name in [profile1, profile2]:
        temp_path = TEMP_PROFILES_DIR / f"{name}-room.yaml"
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def start_proxies(room_profiles: dict, channels: dict | None = None,
                  config: "RoomConfig | None" = None) -> list:
    """Start two CCORAL proxy instances with room profiles.

    `channels`, when provided, maps slot int (1 or 2) -> (path, kind). The
    proxy is told to push completed-turn records there via
    `CCORAL_RESPONSE_FIFO` (when kind=="fifo") or `CCORAL_RESPONSE_JSONL`
    (kind=="jsonl"). When `channels` is None we fall back to a legacy
    per-slot `CCORAL_RESPONSE_FILE` path so non-arbiter callers still work.

    Each proxy also gets `CCORAL_ROOM_SLOT=1` or `=2` so server.py can
    stamp every turn record with its slot identity (Phase 8). Slot is
    derived from enumeration order over `room_profiles` — the dict
    insertion order matches (profile1, profile2) at the run_room call
    site.

    Phase 3: `config` carries the resolved RoomConfig (in particular
    `base_port`). When None, a defaulted RoomConfig() is used so legacy
    callers and tests still work without threading a config explicitly.
    """
    if config is None:
        config = RoomConfig()
    server_path = SCRIPT_DIR / "server.py"
    procs = []

    # Per-port log files. stdout=PIPE deadlocks here (no reader drains the pipe
    # while this process is busy orchestrating tmux panes), so each proxy gets
    # its own daily log file.
    log_dir = Path.home() / ".ccoral" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    for i, (base_name, room_name) in enumerate(room_profiles.items()):
        slot = i + 1
        port = config.base_port + i
        env = os.environ.copy()
        env["CCORAL_PORT"] = str(port)
        env["CCORAL_PROFILE"] = room_name
        env["CCORAL_ROOM_SLOT"] = str(slot)
        env["CCORAL_LOG"] = "0"
        if channels and slot in channels:
            ch_path, ch_kind = channels[slot]
            if ch_kind == "fifo":
                env["CCORAL_RESPONSE_FIFO"] = str(ch_path)
            else:
                env["CCORAL_RESPONSE_JSONL"] = str(ch_path)
        else:
            # Legacy fallback for non-arbiter callers — slot-prefixed so a
            # `room blank blank` invocation can't collide on the same path.
            env["CCORAL_RESPONSE_FILE"] = str(ROOM_DIR / f"slot{slot}_{base_name}.txt")
        # Make sure proxies hit the real API, not any existing proxy
        env.pop("ANTHROPIC_BASE_URL", None)

        log_path = log_dir / f"proxy-{port}-{datetime.now():%Y-%m-%d}.log"
        log_fh = open(log_path, "ab", buffering=0)

        proc = subprocess.Popen(
            [sys.executable, str(server_path)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
        procs.append((proc, port, base_name, log_fh, log_path))

    time.sleep(1.5)

    for proc, port, name, _log_fh, log_path in procs:
        if proc.poll() is not None:
            try:
                with open(log_path, "rb") as f:
                    f.seek(max(0, log_path.stat().st_size - 4096))
                    out = f.read().decode(errors="replace")
            except Exception:
                out = ""
            raise RuntimeError(f"Proxy for {name} on :{port} failed: {out}")

    return procs


def stop_proxies(procs: list):
    """Terminate all proxy processes."""
    for proc, port, name, log_fh, _log_path in procs:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            log_fh.close()
        except Exception:
            pass


def setup_tmux(profile1: str, profile2: str,
               config: "RoomConfig | None" = None) -> bool:
    """Create two separate tmux sessions, one per Claude instance.

    Phase 3: tmux session names and ports come from the RoomConfig so two
    rooms can run side-by-side without colliding on session names or ports.
    """
    if config is None:
        config = RoomConfig()

    p1_session = config.session_for(profile1)
    p2_session = config.session_for(profile2)

    port1 = config.base_port
    port2 = config.base_port + 1
    cmd1 = f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port1} claude --dangerously-skip-permissions"
    cmd2 = f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port2} claude --dangerously-skip-permissions"

    # Kill existing sessions if any
    for sess in [p1_session, p2_session]:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    time.sleep(0.5)

    # Create session for profile1
    subprocess.run(["tmux", "new-session", "-d", "-s", p1_session], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", p1_session, cmd1, "Enter"], capture_output=True)

    # Create session for profile2
    subprocess.run(["tmux", "new-session", "-d", "-s", p2_session], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", p2_session, cmd2, "Enter"], capture_output=True)

    # Verify both sessions exist
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    p1_ok = p1_session in result.stdout
    p2_ok = p2_session in result.stdout

    if not (p1_ok and p2_ok):
        print(f"Failed to create sessions: {result.stdout}")
        return False

    return True


def send_to_pane(session: str, message: str):
    """Send a message to a tmux session via send-keys. Always pastes directly."""
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "-l", message],
        capture_output=True,
    )
    time.sleep(0.25)
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "Enter"],
        capture_output=True,
    )


# Tmux's paste-buffer is roughly bounded by the buffer-size config (default
# ~1MB on most builds, sometimes lower on older systems). 256KB per chunk
# is comfortably under any limit we'll hit and keeps each subprocess hand-off
# cheap. Chunks are pasted in order with no extra newline between them
# (paste-buffer is verbatim) — only the final paste is followed by Enter.
_TMUX_BUFFER_CHUNK = 256 * 1024


def _envelope_iso8601_utc() -> str:
    """UTC ISO-8601 with millisecond precision and trailing Z. Matches the
    server-side `_iso8601_utc_now` shape so envelope timestamps and turn-
    record `ts` line up byte-for-byte.
    """
    from datetime import timezone as _tz
    return datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_relay_envelope(
    *,
    sender: str,
    recipient: str,
    kind: str,
    ts: str,
    stop_reason: str | None = None,
    reason: str | None = None,
    text: str,
) -> str:
    """Build the structured header-line envelope used between rooms.

    Shape:
      Turn relay (KIND=turn):
        [FROM=<sender> TO=<recipient> KIND=turn TS=<ts> STOP=<stop_reason>]
        <body, multi-line preserved verbatim>

      Cockpit interject (KIND=interject):
        [FROM=<sender> TO=<recipient or BOTH> KIND=interject TS=<ts>]
        <body>

      System (KIND=system):
        [FROM=SYSTEM TO=<target> KIND=system TS=<ts> REASON=<short_slug>]
        <body>

    The header line is a parseable convention for the orchestrator export
    path and a natural read for the receiving Claude — it is NOT a tool-use
    instruction. The model knows it's the addressed party because that's
    what its profile inject and seed told it; the FROM/TO header is just a
    courtesy label, like a memo header. No "respond as TO" instruction is
    appended anywhere — that would push toward roleplay framing we don't
    want.

    Body content is pasted verbatim. Newlines within `text` are preserved.
    The chunked `relay_via_paste_buffer` helper splits at byte boundaries
    that don't need to respect the header — the receiving Claude reads the
    whole pasted block as one message.
    """
    fields = [f"FROM={sender}", f"TO={recipient}", f"KIND={kind}", f"TS={ts}"]
    if kind == "turn":
        # STOP is mandatory shape-wise on turns; if absent (theoretical —
        # the server always sets one) we emit "unknown" so the parser path
        # never has to special-case missing fields.
        fields.append(f"STOP={stop_reason or 'unknown'}")
    elif kind == "system":
        fields.append(f"REASON={reason or 'unspecified'}")
    header = "[" + " ".join(fields) + "]"
    return f"{header}\n{text}"


def relay_via_paste_buffer(session: str, text: str, buffer_name: str) -> None:
    """Relay a multi-line message into a tmux pane via load-buffer + paste-buffer.

    This replaces the legacy "read-this-tmpfile" instruction relay that
    exposed plumbing to the receiving model and caused meta-confused
    replies. The paste-buffer route preserves multi-line structure verbatim
    without any in-context instruction.

    For very large turns (>256KB) we chunk the load-buffer call so we don't
    bump into tmux's per-buffer ceiling. Each chunk uses a unique buffer
    name suffix so a partial failure is obvious from `tmux list-buffers`.
    Buffers are deleted once the paste lands.
    """
    if not text:
        return

    # Single-shot fast path — no chunking needed for small relays.
    if len(text) <= _TMUX_BUFFER_CHUNK:
        subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            input=text, text=True, capture_output=True,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-b", buffer_name, "-t", session],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buffer_name],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", session, "Enter"],
            capture_output=True,
        )
        return

    # Chunked path. Paste-buffer preserves bytes verbatim, so we MUST NOT
    # add separators between chunks — splice points are invisible to the
    # receiving pane. Send Enter once after the last chunk lands.
    total = len(text)
    sent = 0
    chunk_idx = 0
    while sent < total:
        end = min(sent + _TMUX_BUFFER_CHUNK, total)
        chunk = text[sent:end]
        chunk_buf = f"{buffer_name}-{chunk_idx}"
        subprocess.run(
            ["tmux", "load-buffer", "-b", chunk_buf, "-"],
            input=chunk, text=True, capture_output=True,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-b", chunk_buf, "-t", session],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "delete-buffer", "-b", chunk_buf],
            capture_output=True,
        )
        sent = end
        chunk_idx += 1

    subprocess.run(
        ["tmux", "send-keys", "-t", session, "Enter"],
        capture_output=True,
    )


def pane_for_profile(panes: dict, slot_meta: dict, target: str):
    """Resolve `/to <target>` to a session name.

    Phase 8: `panes` is now slot-keyed ({1: sess1, 2: sess2}); profile
    name lookups go through `slot_meta` (which carries each slot's
    profile name). Accepts:
      - bare profile name ("blank")
      - bare digits ("1", "2")
      - per-pane suffix ("blank-1", "blank-2")
    All matches are case-insensitive. Returns None on no match.

    Note the per-pane-suffix forms are unambiguous about slot even when
    profile1 == profile2 — `blank-1` always means slot 1.
    """
    tlow = (target or "").lower()
    # Bare digits.
    if target == "1":
        return panes.get(1)
    if target == "2":
        return panes.get(2)
    # `<profile>-1` / `<profile>-2`, slot-explicit, case-insensitive.
    p1_low = slot_meta[1]["profile"].lower()
    p2_low = slot_meta[2]["profile"].lower()
    if tlow == f"{p1_low}-1":
        return panes.get(1)
    if tlow == f"{p2_low}-2":
        return panes.get(2)
    # Plain profile-name match. If both slots share the profile, slot 1
    # wins by convention — the operator should use `<name>-2` for the
    # other side. (This branch is unreachable when distinct profiles
    # are used, which is the common case.)
    if tlow == p1_low:
        return panes.get(1)
    if tlow == p2_low:
        return panes.get(2)
    return None


def save_conversation(messages: list, profiles: list) -> Path:
    """Save conversation log to archive."""
    ROOMS_ARCHIVE.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{timestamp}_{profiles[0]}-{profiles[1]}.json"
    path = ROOMS_ARCHIVE / filename

    data = {
        "profiles": profiles,
        "started": messages[0]["time"] if messages else datetime.now().isoformat(),
        "ended": datetime.now().isoformat(),
        "messages": messages,
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    return path


def load_conversation(resume: str) -> dict:
    """Load a saved conversation.

    Phase 5: prefers the per-room state dir under ~/.ccoral/rooms/<id>/
    (Phase 3) when resolvable; falls back to the legacy ~/.ccoral/rooms/
    *.json archives so older rooms still resume cleanly while the legacy
    archive remains.
    """
    # Phase 5: state-dir path (preferred). Reads transcript.jsonl and
    # config.yaml so the same {"profiles", "messages"} shape comes back.
    target = _resolve_room_id(resume)
    if target is not None:
        return _load_state_dir(target)

    # Legacy JSON archive fallback.
    if resume == "last":
        files = sorted(ROOMS_ARCHIVE.glob("*.json"))
        if not files:
            print(f"No saved rooms in {ROOMS_ARCHIVE}")
            sys.exit(1)
        path = files[-1]
    else:
        path = ROOMS_ARCHIVE / resume
        if not path.exists():
            path = Path(resume)
    if not path.exists():
        print(f"Not found: {resume}")
        sys.exit(1)

    with open(path) as f:
        return json.load(f)


def _load_state_dir(room_dir: Path) -> dict:
    """Read a per-room state dir (Phase 3) into the legacy
    `{"profiles", "started", "ended", "messages"}` shape so the resume
    code path doesn't need a second branch.

    transcript.jsonl carries one record per line; we parse and pass
    them through verbatim. config.yaml carries the resolved profile
    pair under `profile1`/`profile2`. meta.yaml carries timestamps.
    """
    meta = _read_meta(room_dir)
    try:
        with open(room_dir / "config.yaml") as f:
            cfg_yaml = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg_yaml = {}
    profiles = [
        cfg_yaml.get("profile1") or (meta.get("profiles") or [None, None])[0],
        cfg_yaml.get("profile2") or (meta.get("profiles") or [None, None])[1],
    ]
    messages: list = []
    transcript = room_dir / "transcript.jsonl"
    if transcript.exists():
        with open(transcript) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return {
        "profiles": profiles,
        "started": meta.get("started", ""),
        "ended": meta.get("ended", ""),
        "messages": messages,
        "_state_dir": str(room_dir),
    }


# ───────────────────────────────────────────────────────────────────────────
# Phase 3 C4: per-room state directory
# ───────────────────────────────────────────────────────────────────────────

# CCORAL version label for meta.yaml. Pulled lazily from the `ccoral`
# CLI's VERSION constant when accessible; falls back to "unknown" so a
# stripped-down install doesn't crash. ccoral imports room.py (not the
# other way around), so we sniff it via a deferred attribute lookup.
def _ccoral_version() -> str:
    try:
        ccoral_path = SCRIPT_DIR / "ccoral"
        if ccoral_path.exists():
            for line in ccoral_path.read_text().splitlines():
                if line.strip().startswith("VERSION = "):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def _profile_sha(profile_name: str) -> str:
    """Best-effort sha256 of the resolved temp profile YAML for meta.yaml.

    Reads ~/.ccoral/profiles/<name>-room.yaml — the file create_room_profiles
    just wrote. Returns the first 16 hex chars of the digest (enough to
    disambiguate runs without bloating the meta file). On any read error
    returns "unknown".
    """
    import hashlib
    try:
        path = TEMP_PROFILES_DIR / f"{profile_name}-room.yaml"
        if not path.exists():
            return "unknown"
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        return "unknown"


def _make_room_id(profile1: str, profile2: str) -> str:
    """`<timestamp>_<p1>-<p2>` matching the legacy archive filename scheme.

    Duplicate-profile case (`room blank blank`) shares the prefix shape
    — slot disambiguation already happens elsewhere (Phase 8 sink paths,
    slot-keyed routing); the room id just needs to be unique per launch,
    which the timestamp guarantees down to the second.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return f"{timestamp}_{profile1}-{profile2}"


class RoomState:
    """Per-room state directory under ~/.ccoral/rooms/<id>/.

    Owns three files:
      config.yaml      — flags + resolved RoomConfig at start (resume key)
      meta.yaml        — lifecycle: written once at start (state=live),
                         updated once at exit (state=stopped + exit_reason).
                         Phase 11's room-watcher polls this `state` field.
      transcript.jsonl — append-only per-turn record. The room appends
                         here AFTER consuming a turn from the FIFO/JSONL
                         channel, NOT the proxy directly — keeps the
                         channel as single source of truth and avoids a
                         double-write race.

    The previous-Phase exit-time `save_conversation` JSON write is
    superseded by the JSONL append + meta.yaml lifecycle. The legacy
    archive is left in place under ROOMS_ARCHIVE/*.json by run_room
    so existing `--resume` paths keep working until Phase 5 lands.
    """

    def __init__(self, room_id: str, config: RoomConfig,
                 profile1: str, profile2: str,
                 channels: dict, base_dir: "Path | None" = None):
        self.room_id = room_id
        self.config = config
        self.profile1 = profile1
        self.profile2 = profile2
        self.channels = channels
        base = base_dir if base_dir is not None else (Path.home() / ".ccoral" / "rooms")
        self.dir = base / room_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.yaml"
        self.meta_path = self.dir / "meta.yaml"
        self.transcript_path = self.dir / "transcript.jsonl"

    def write_initial(self) -> None:
        """Write config.yaml + meta.yaml(state=live) + touch transcript.jsonl.

        Called once at room start, after channels are set up but before
        the relay loop begins. Idempotent — safe to call twice without
        clobbering an in-progress run, since the meta exit_reason field
        is only set by `update_exit`.
        """
        # config.yaml — every flag the operator set, resolved against
        # RoomConfig defaults so a `--resume` later sees exactly the
        # values that were live.
        config_data = {
            "room_id": self.room_id,
            "profile1": self.profile1,
            "profile2": self.profile2,
            "config": asdict(self.config),
        }
        with open(self.config_path, "w") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

        # meta.yaml — lifecycle + provenance stamped once at start.
        meta = {
            "ccoral_version": _ccoral_version(),
            "room_id": self.room_id,
            "profiles": [self.profile1, self.profile2],
            "models": {
                self.profile1: "auto",
                self.profile2: "auto",
            },
            "profile_shas": {
                self.profile1: _profile_sha(self.profile1),
                self.profile2: _profile_sha(self.profile2),
            },
            "ports": {
                "slot1": self.config.base_port,
                "slot2": self.config.base_port + 1,
            },
            "channels": {
                f"slot{slot}": {"path": str(p), "kind": kind}
                for slot, (p, kind) in self.channels.items()
            },
            "started": datetime.now().isoformat(),
            "state": "live",
            "exit_reason": None,
        }
        with open(self.meta_path, "w") as f:
            yaml.dump(meta, f, default_flow_style=False, allow_unicode=True)

        # transcript.jsonl — touch so room ls / show can read it even
        # for a session that ends before any turn lands.
        self.transcript_path.touch(exist_ok=True)

    def append_turn(self, record: dict) -> None:
        """Append one record to transcript.jsonl. Used as the relay_loop
        `on_turn_committed` callback. One JSON object per line; UTF-8.
        Caller is the relay loop running on the cockpit worker thread,
        so we open-write-close per turn — append-only writes are atomic
        on Linux for buffers under PIPE_BUF, and turn records are tiny.
        """
        try:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # Same swallow policy as the on_turn_committed contract — a
            # transcript-write failure must never wedge the relay.
            pass

    def update_exit(self, exit_reason: str) -> None:
        """Stamp meta.yaml with state=stopped + the resolved exit_reason.

        Reads the existing meta back, mutates the two fields, rewrites.
        We don't truncate-and-rewrite atomically because a torn write
        leaves the prior state file intact and Phase 11 just reports
        "live" for one extra poll cycle — far better than wiping the
        provenance to write a single field.
        """
        try:
            with open(self.meta_path) as f:
                meta = yaml.safe_load(f) or {}
        except FileNotFoundError:
            # write_initial was never called — synthesize a minimal meta.
            meta = {
                "ccoral_version": _ccoral_version(),
                "room_id": self.room_id,
                "profiles": [self.profile1, self.profile2],
                "started": None,
            }
        meta["state"] = "stopped"
        meta["exit_reason"] = exit_reason
        meta["ended"] = datetime.now().isoformat()
        with open(self.meta_path, "w") as f:
            yaml.dump(meta, f, default_flow_style=False, allow_unicode=True)


def _list_room_dirs(base: "Path | None" = None) -> list:
    """Return per-room state dirs under ~/.ccoral/rooms/, newest first.

    Sorting is by mtime of the dir (matches "started timestamp
    descending" — the room id prefix is the timestamp anyway, but
    mtime is more forgiving for clock skew). Filters out non-dirs and
    obvious stragglers (no meta.yaml).
    """
    base = base if base is not None else (Path.home() / ".ccoral" / "rooms")
    if not base.exists():
        return []
    entries = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if not (child / "meta.yaml").exists():
            continue
        entries.append(child)
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return entries


def _read_meta(room_dir: Path) -> dict:
    try:
        with open(room_dir / "meta.yaml") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _count_turns(room_dir: Path) -> int:
    """Count `kind == "turn"` records in transcript.jsonl. Lines that
    fail to parse are silently skipped so a partial-write tail doesn't
    crash `room ls`.
    """
    path = room_dir / "transcript.jsonl"
    if not path.exists():
        return 0
    n = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") == "turn":
                    n += 1
    except Exception:
        pass
    return n


def _last_topic(room_dir: Path) -> str:
    """Best-effort topic label for `room ls`. Reads the first non-turn
    record from transcript.jsonl (typically the seed1/seed2 host
    interjection); falls back to empty string.
    """
    path = room_dir / "transcript.jsonl"
    if not path.exists():
        return ""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # The first record is usually the seed (relay-meta /
                # interject). If the room ran cold, fall through to the
                # first turn instead.
                text = (rec.get("text") or "").strip().splitlines()[0:1]
                if text:
                    return text[0][:60]
    except Exception:
        pass
    return ""


def room_ls(base: "Path | None" = None, stream=None) -> None:
    """List all rooms under ~/.ccoral/rooms/ in tabular form.

    Columns: id (timestamped), profiles, started, turns, state, topic.
    Sorted newest first. Output goes to `stream` (default stdout) so
    tests can capture without monkey-patching.
    """
    if stream is None:
        stream = sys.stdout
    dirs = _list_room_dirs(base)
    if not dirs:
        stream.write("(no rooms found)\n")
        return

    rows = []
    for d in dirs:
        meta = _read_meta(d)
        profiles = meta.get("profiles") or []
        prof_label = "x".join(profiles) if profiles else d.name
        started = meta.get("started", "") or ""
        # Trim ISO timestamp for display; keep date + HH:MM.
        started_short = started[:16].replace("T", " ") if started else ""
        state = meta.get("state", "?")
        turns = _count_turns(d)
        topic = _last_topic(d)
        rows.append((d.name, prof_label, started_short, turns, state, topic))

    headers = ("ID", "PROFILES", "STARTED", "TURNS", "STATE", "TOPIC")
    widths = [
        max(len(headers[0]), max(len(r[0]) for r in rows)),
        max(len(headers[1]), max(len(r[1]) for r in rows)),
        max(len(headers[2]), max(len(r[2]) for r in rows)),
        max(len(headers[3]), max(len(str(r[3])) for r in rows)),
        max(len(headers[4]), max(len(r[4]) for r in rows)),
        max(len(headers[5]), max(len(r[5]) for r in rows)),
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    stream.write(fmt.format(*headers) + "\n")
    for r in rows:
        stream.write(fmt.format(r[0], r[1], r[2], str(r[3]), r[4], r[5]) + "\n")


def _resolve_room_id(room_id: str, base: "Path | None" = None) -> "Path | None":
    """Map a `<id>|last` token to a room directory.

    "last" → most recent dir. Otherwise: first try the literal id, then
    a prefix match (so the operator can paste just the timestamp).
    """
    dirs = _list_room_dirs(base)
    if not dirs:
        return None
    if room_id == "last":
        return dirs[0]
    base = base if base is not None else (Path.home() / ".ccoral" / "rooms")
    exact = base / room_id
    if exact.exists() and exact.is_dir():
        return exact
    for d in dirs:
        if d.name.startswith(room_id):
            return d
    return None


def room_show(room_id: str, base: "Path | None" = None) -> None:
    """Page the transcript for `<room_id>` in $PAGER (default `less -R`).

    Renders one line per record from transcript.jsonl into a temp file,
    formatted as `[STATE] NAME: text` so the pager view matches the
    cockpit transcript reasonably. Color preserved through `less -R`
    (the only ANSI we currently emit lives in the cockpit, not the
    archived JSONL — but `-R` is the safe default for future-color).

    `<room_id>` accepts "last" or a full / prefix id. Errors print to
    stderr and exit 1.
    """
    target = _resolve_room_id(room_id, base)
    if target is None:
        print(f"{Y}room not found: {room_id}{NC}", file=sys.stderr)
        sys.exit(1)
    transcript = target / "transcript.jsonl"
    if not transcript.exists():
        print(f"{Y}no transcript at {transcript}{NC}", file=sys.stderr)
        sys.exit(1)

    # Render to a tempfile so the pager doesn't tie up the JSONL during
    # an active session.
    import tempfile
    tmp = Path(tempfile.NamedTemporaryFile(
        prefix=f"ccoral-room-{target.name}-", suffix=".txt", delete=False,
    ).name)
    with open(tmp, "w", encoding="utf-8") as out:
        meta = _read_meta(target)
        out.write(f"# room: {target.name}\n")
        out.write(f"# profiles: {meta.get('profiles')}\n")
        out.write(f"# state: {meta.get('state', '?')} "
                  f"exit_reason: {meta.get('exit_reason')}\n")
        out.write(f"# started: {meta.get('started', '?')}\n")
        out.write("\n")
        try:
            with open(transcript) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        out.write(f"[malformed] {line}\n")
                        continue
                    name = rec.get("name", "?")
                    text = rec.get("text", "").rstrip()
                    kind = rec.get("kind", "")
                    tag = f"[{kind}] " if kind else ""
                    out.write(f"{tag}{name}: {text}\n\n")
        except Exception as e:
            out.write(f"\n(error reading transcript: {e})\n")

    pager = os.environ.get("PAGER", "less -R")
    # Split on whitespace so PAGER="less -R" vs PAGER=less both work.
    pager_cmd = pager.split() + [str(tmp)]
    try:
        subprocess.run(pager_cmd)
    except FileNotFoundError:
        # Fall back to dumping the file if pager isn't installed.
        sys.stdout.write(tmp.read_text())
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


# ───────────────────────────────────────────────────────────────────────────
# Phase 2: turn-record consumers
# ───────────────────────────────────────────────────────────────────────────


class _FifoReader:
    """Line-buffered non-blocking reader over a POSIX FIFO.

    Why this exists: FIFOs deliver bytes when the writer flushes, and a JSONL
    record may straddle a single `read()` call. We accumulate bytes and yield
    only complete `\\n`-terminated lines.

    Open mode O_RDONLY|O_NONBLOCK so the caller can `select.select` on the
    fd. The proxy side opens the same FIFO O_WRONLY|O_NONBLOCK at emit time
    and writes one record at a time; ENXIO on the write side just means we
    weren't ready to read yet (rate-limited warning, dropped record).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        # O_NONBLOCK on read side requires a writer to ever attach; if no
        # one's there yet, open returns immediately and reads will return
        # b"" until bytes arrive.
        self.fd = os.open(str(self.path), os.O_RDONLY | os.O_NONBLOCK)
        self._buf = b""

    def fileno(self) -> int:
        return self.fd

    def read_lines(self) -> list:
        """Drain whatever bytes are currently available; return any complete
        `\\n`-terminated lines as decoded str (no trailing newline). Partial
        tail bytes stay buffered for the next call.
        """
        out = []
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                raise
            if not chunk:
                # EOF on FIFO means all writers closed. With force_close
                # proxies that's transient; the next writer-open re-arms us.
                break
            self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                out.append(line.decode("utf-8", errors="replace"))
        return out

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class _JsonlTailReader:
    """Polled tail-reader over a JSONL file, used when FIFO support isn't
    available on the host. We track byte offset and re-open on each tick.

    Concurrent O_APPEND from the proxy means the file only ever grows;
    `os.path.getsize` is the tail cursor.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.touch(exist_ok=True)
        self._offset = self.path.stat().st_size
        self._buf = b""

    def fileno(self) -> int | None:
        # No fd to select on — caller polls.
        return None

    def read_lines(self) -> list:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size <= self._offset:
            return []
        out = []
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read(size - self._offset)
            self._offset = size
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                out.append(line.decode("utf-8", errors="replace"))
        return out

    def close(self) -> None:
        pass


class TurnArbiter:
    """Strict turn ordering driven by proxy `stop_reason` signals.

    State per profile: `idle` (no pending turn), `speaking` (mid-stream,
    relay deferred until end_turn). Only one profile may be `speaking` at
    a time — concurrent `message_start` events from both sides land on the
    arbiter and the second one is queued until the first finishes.

    Backpressure: per profile, count `consecutive_solo_turns` — turns
    produced without the other side replying. On hitting the cap, we
    inject a `[SYSTEM] {other} hasn't replied yet — pausing.` message
    into the speaker's pane and stall further relays until either:
      - the timeout expires,
      - the user issues `/resume`,
      - or the other side produces a turn.

    The arbiter does NOT do the actual relay or pane I/O — it returns
    decisions (relay this text, inject this system message, pause).
    The relay_loop applies them. Keeps the class testable in isolation.
    """

    # Decision kinds returned from on_turn_record / tick.
    RELAY = "relay"             # ("relay", text, target_profile)
    BACKPRESSURE = "system"     # ("system", text, speaker_profile)
    RESUME = "resume"           # ("resume", speaker_profile)

    def __init__(
        self,
        profile1: str,
        profile2: str,
        backpressure_turns: int = 2,
        backpressure_timeout: float = 60.0,
    ):
        self.profile1 = profile1
        self.profile2 = profile2
        self.cap = max(1, backpressure_turns)
        self.timeout = backpressure_timeout

        # Per-profile speaking state — currently used for visibility; the
        # `stop_reason: end_turn` arrival IS the speaking-end signal, so
        # we don't track per-chunk presence here.
        self.state = {profile1: "idle", profile2: "idle"}
        self.consecutive_solo = {profile1: 0, profile2: 0}
        # When a profile is stalled, we reject further relays from them
        # until the stall clears. Stored as the time the stall expires.
        self.stalled_until = {profile1: 0.0, profile2: 0.0}

    def _other(self, name: str) -> str:
        return self.profile2 if name == self.profile1 else self.profile1

    def on_turn_record(self, speaker: str, text: str) -> list:
        """Process a completed-turn record from `speaker`. Returns a list
        of decisions for the relay_loop to execute, in order.
        """
        decisions = []
        other = self._other(speaker)

        # The other side replied (got a turn from `speaker` after `other`
        # was last speaker, or vice versa) — clear `other`'s solo counter
        # and any pending stall; bump `speaker`'s solo counter.
        self.consecutive_solo[other] = 0
        if self.stalled_until[other] > 0.0:
            self.stalled_until[other] = 0.0
        self.consecutive_solo[speaker] += 1

        # If the speaker is currently stalled (cap previously hit and stall
        # not yet cleared), drop the relay — we're waiting on the other
        # side or the user's /resume.
        if self.stalled_until[speaker] > 0.0:
            now = time.monotonic()
            if now < self.stalled_until[speaker]:
                # Still stalled. Don't relay this turn.
                return decisions
            # Stall timeout elapsed — clear and continue.
            self.stalled_until[speaker] = 0.0

        # Always relay the speaker's text to the other pane.
        decisions.append((self.RELAY, text, other))

        # Backpressure trip: speaker just produced their Nth solo turn.
        # The relay site wraps `sys_text` in the structured envelope; we
        # return the bare body + a reason slug so the arbiter stays format-
        # agnostic.
        if self.consecutive_solo[speaker] >= self.cap:
            sys_text = f"{other.upper()} hasn't replied yet — pausing."
            self.stalled_until[speaker] = time.monotonic() + self.timeout
            decisions.append((
                self.BACKPRESSURE, sys_text, speaker, "backpressure_stall",
            ))

        return decisions

    def manual_resume(self, speaker: str | None = None) -> None:
        """Operator-triggered /resume. Clears stall on `speaker` (or both
        sides if None). Solo counters are NOT reset — the next genuine
        turn from the other side does that organically.
        """
        targets = [speaker] if speaker else [self.profile1, self.profile2]
        for p in targets:
            if p in self.stalled_until:
                self.stalled_until[p] = 0.0


def resolve_record_slot(record: dict, reader_slot: int) -> tuple[int, bool]:
    """Source of truth for "which slot spoke this turn".

    Returns (slot, was_legacy_fallback). The slot field stamped by the
    proxy (Phase 8) is authoritative — that's what the launcher chose
    when spawning the proxy and what server.py wrote into the record.
    A missing or malformed `slot` means we're consuming a record from
    a producer that predates the Phase 8 envelope (or a hand-crafted
    test fixture); we fall back to the reader's slot — the FIFO/JSONL
    that delivered the bytes — and flag the legacy path.

    The intent is to make the absence of the mtime path explicit:
    the orchestrator NEVER infers speaker from filesystem ordering,
    sink-path identity, or which channel "changed first". Speaker
    identity is carried in the record or it's a legacy bug.
    """
    rec_slot = record.get("slot")
    if isinstance(rec_slot, int) and rec_slot in (1, 2):
        return rec_slot, False
    return reader_slot, True


def _setup_turn_channel(slot: int, profile_name: str) -> tuple[Path, str]:
    """Create the per-slot turn-record channel (FIFO when supported,
    JSONL fallback). Returns (path, channel_kind).

    Phase 8: paths are slot-prefixed (`slot1_<profile>.fifo`) so a
    duplicate-profile invocation (`ccoral room blank blank`) cannot
    collide on the same sink. Slot prefix guarantees uniqueness even
    when both proxies share a base profile name.

    Detection rule: try mkfifo; on OSError (ENOTSUP, EPERM, etc.) fall
    back to a writable JSONL path. The orchestrator logs the choice once
    at startup so the operator knows which path is live.
    """
    fifo_path = ROOM_DIR / f"slot{slot}_{profile_name}.fifo"
    # Clear any stale node; a previous run's leftover would either be the
    # right kind (re-create cleanly) or the wrong kind (replace).
    try:
        fifo_path.unlink()
    except FileNotFoundError:
        pass
    try:
        os.mkfifo(str(fifo_path), 0o600)
        return fifo_path, "fifo"
    except (OSError, NotImplementedError):
        # Fall through to JSONL.
        pass

    jsonl_path = ROOM_DIR / f"slot{slot}_{profile_name}.jsonl"
    try:
        jsonl_path.unlink()
    except FileNotFoundError:
        pass
    jsonl_path.touch()
    return jsonl_path, "jsonl"


def relay_loop(profile1: str, profile2: str, topic: str = None,
               prior_messages: list = None,
               channels: dict | None = None,
               legacy_cockpit: bool = False,
               config: "RoomConfig | None" = None,
               on_turn_committed: "Callable[[dict], None] | None" = None):
    """Drive the room: consume turn records from each proxy's channel,
    feed them to a `TurnArbiter`, and execute its decisions (paste-buffer
    relay, system backpressure messages, stalls).

    `channels` is `{profile_name: (path, kind)}` where kind is "fifo" or
    "jsonl". When None, we set up channels here as a courtesy (lets
    standalone callers and tests use the same entry point).

    Pane layout after setup_tmux:
      0.0 = top-left (profile1 Claude)
      0.1 = bottom-left (control)
      0.2 = right (profile2 Claude)

    Phase 3: `config` carries the resolved RoomConfig (session prefix,
    user name, backpressure knobs, turn limit, seed1/seed2). When None
    we build a defaulted RoomConfig() — preserves pre-Phase-3 behavior
    for tests and standalone callers.

    `on_turn_committed`, when supplied, is invoked for every message
    appended to the in-memory transcript. C4 wires this to the per-room
    `transcript.jsonl` append. Exceptions raised by the callback are
    swallowed so a transient I/O failure on the archive side never wedges
    the relay.
    """
    if config is None:
        config = RoomConfig()
    user_name = config.user_name
    # Per-run room id used to namespace tmux paste buffers so two rooms
    # running concurrently don't trample each other's relay payloads.
    # Phase 3: prefer the RoomConfig.room_id when set (run_room stamps it
    # at start so the per-room state dir and the paste-buffer namespace
    # share an identifier); fall back to the legacy synthetic id for
    # standalone callers that don't go through run_room.
    room_id = config.room_id or f"{profile1}-{profile2}-{int(time.time())}"
    turn_seq = 0
    # Turn-limit accounting (Phase 3 task 3.1). Counts speaker turns —
    # i.e. completed-turn records the relay actually committed to the
    # transcript. Initial seeds and SYSTEM lines do NOT count. None means
    # unlimited (legacy behavior).
    turns_committed = 0
    exit_reason = "clean"

    # Phase 9 C5: --legacy-cockpit fallback. Rebind `room_control` to the
    # bespoke pre-Phase-9 module (now room_control_legacy.py) so every
    # call site below routes to the old split-screen owner instead of
    # the Textual RoomApp. The closure that follows closes over this
    # local binding, so the swap is total. One-release safety net only;
    # Phase 12 deletes the flag and the legacy module.
    #
    # The `import ... as room_control` inside the if-branch makes
    # `room_control` a function-local for the whole function (Python
    # lexical-scope rule), shadowing the module-level `import room_app
    # as room_control`. Both branches must rebind, or the False path
    # leaves the local unbound and every later call raises
    # UnboundLocalError. (Phase 9 follow-up; bug surfaced 2026-05-10.)
    if legacy_cockpit:
        import room_control_legacy as room_control  # noqa: F811
    else:
        import room_app as room_control  # noqa: F811

    if channels is None:
        # Late binding for callers that didn't pre-create channels. This
        # path is only used by tests / non-room callers — the production
        # `run_room` entry point pre-creates them so the proxy is told
        # the path before it boots.
        channels = {
            1: _setup_turn_channel(1, profile1),
            2: _setup_turn_channel(2, profile2),
        }

    # Conversation log
    messages = prior_messages or []

    # Phase 8: slot-keyed maps. Pane assignment is fixed to the launch
    # order — slot 1 == profile1's pane, slot 2 == profile2's pane —
    # and stays that way for the life of the room. Speaker color +
    # display come from the slot, not the profile name, so a
    # duplicate-profile run (`room blank blank`) still routes correctly.
    # Phase 3: session names come from RoomConfig.session_for so two
    # rooms with different `tmux_session_prefix` values don't collide.
    panes = {
        1: config.session_for(profile1),
        2: config.session_for(profile2),
    }
    # Phase 8 default display rule. Distinct profiles → bare uppercased
    # name; same profile in both slots → suffixed `<NAME>#1` / `<NAME>#2`.
    # Phase 3 will plumb --user-1 / --user-2 overrides through to here;
    # for now the launcher just resolves slot_meta directly.
    if profile1 == profile2:
        display1 = f"{get_display_name(profile1)}#1"
        display2 = f"{get_display_name(profile2)}#2"
    else:
        display1 = get_display_name(profile1)
        display2 = get_display_name(profile2)

    # Publish the resolved displays so cockpit-side code (status line,
    # /help footer, future Phase 9 Textual chrome) can read them
    # without re-deriving from profile names.
    room_control.set_speaker_display(1, display1)
    room_control.set_speaker_display(2, display2)

    slot_meta = {
        1: {"profile": profile1, "color": Y, "display": display1},
        2: {"profile": profile2, "color": C, "display": display2},
    }

    # Per-slot channel readers. FIFO uses select; JSONL polls.
    readers = {}
    for slot in (1, 2):
        ch_path, ch_kind = channels[slot]
        if ch_kind == "fifo":
            readers[slot] = _FifoReader(ch_path)
        else:
            readers[slot] = _JsonlTailReader(ch_path)

    # Arbiter — strict turn order + backpressure. Still profile-keyed
    # internally; the relay-side translation lives in _handle_turn_record.
    # Phase 3: backpressure knobs come from RoomConfig.
    arbiter = TurnArbiter(
        profile1, profile2,
        backpressure_turns=config.backpressure_turns,
        backpressure_timeout=config.backpressure_timeout_s,
    )

    # Give Claude sessions time to start up
    room_control.set_user(user_name)

    # Helper: append to the in-memory transcript AND fan out to the
    # per-room JSONL appender, if one was provided. Defined at the
    # outer scope so every closure below can call it without smuggling
    # the callback through a parameter.
    def _commit(record: dict) -> None:
        messages.append(record)
        if on_turn_committed is not None:
            try:
                on_turn_committed(record)
            except Exception:
                # Don't let a transcript-archive failure wedge the relay.
                pass

    # Phase 9 C4: relay runs in a Textual @work(thread=True) worker
    # spawned by RoomApp. The closure below is the runner — same body
    # as the legacy `with split_screen():` block, just lifted into a
    # function so the App can hand it to its worker thread. App.run()
    # blocks the main thread until /stop or Ctrl+C; the closure exits
    # naturally once the relay loop breaks.
    def _relay_runner(app=None):
        nonlocal turns_committed, exit_reason
        # One-time channel-mode banner so the operator sees which path is
        # live (FIFO is always preferred; JSONL only on platforms where
        # mkfifo failed). Useful for post-mortem on resume + audit logs.
        for slot in (1, 2):
            _, kind = channels[slot]
            label = f"slot{slot}/{slot_meta[slot]['profile']}"
            room_control.render_transcript_line(
                "ROOM", f"channel[{label}] = {kind}", DIM,
            )

        room_control.render_transcript_line(
            "ROOM", f"waiting for Claude sessions to initialize...", DIM,
        )
        time.sleep(8)

        # Phase 3: asymmetric kickoff. `topic` (legacy positional) maps
        # to seed1 unless config.seed1 is explicitly set. seed2 lets the
        # operator prime profile2 directly — when absent, profile2 starts
        # cold (current behavior preserved). Both seeds are typed in via
        # the same envelope shape as `/say` interjections so the receiving
        # Claude reads them as host messages, not inline directives.
        seed1_text = config.seed1 if config.seed1 is not None else topic
        seed2_text = config.seed2

        if seed1_text and not prior_messages:
            ts = _envelope_iso8601_utc()
            initial_msg = build_relay_envelope(
                sender=user_name,
                recipient=get_display_name(profile1),
                kind="interject",
                ts=ts,
                text=seed1_text,
            )
            send_to_pane(panes[1], initial_msg)
            _commit({
                "name": user_name,
                "text": seed1_text,
                "time": datetime.now().isoformat(),
                "from": user_name,
                "to": get_display_name(profile1),
                # Initial host seed — meta from the conversation's POV.
                "kind": "relay-meta",
                "envelope_kind": "interject",
                "seed_slot": 1,
            })
            room_control.render_transcript_line(user_name, seed1_text, W)

        if seed2_text and not prior_messages:
            ts = _envelope_iso8601_utc()
            initial_msg2 = build_relay_envelope(
                sender=user_name,
                recipient=get_display_name(profile2),
                kind="interject",
                ts=ts,
                text=seed2_text,
            )
            send_to_pane(panes[2], initial_msg2)
            _commit({
                "name": user_name,
                "text": seed2_text,
                "time": datetime.now().isoformat(),
                "from": user_name,
                "to": get_display_name(profile2),
                "kind": "relay-meta",
                "envelope_kind": "interject",
                "seed_slot": 2,
            })
            room_control.render_transcript_line(
                user_name, f"(→ {get_display_name(profile2)}) {seed2_text}", W,
            )

        # Phase 5: the legacy chat-message dump is gone. When resuming,
        # prior history is now embedded in each temp profile's inject
        # as an operator-scope system block (see create_room_profiles
        # `prior_exchange` arg). Both panes start clean — the receiving
        # Claudes already know the prior context from their inject and
        # continue naturally from the next host seed (or from each
        # other if no seed is set).
        if prior_messages:
            room_control.render_transcript_line(
                "ROOM",
                f"resumed: {len(prior_messages)} prior records embedded in inject",
                DIM,
            )

        room_control.render_transcript_line(
            "ROOM", "relay active — watching for responses (try /help)", DIM,
        )

        # Cockpit state
        paused = False
        end_after_turn = False

        # ─── inner helpers (close over panes/messages/arbiter/etc.) ────

        def _say_to_both(text: str) -> None:
            """Relay event: log once, send to both panes once. Not typed
            into pane as a Claude prompt — sent directly via send_to_pane.
            Tagged `relay-meta` so export filters can drop interjections
            cleanly without resorting to string-match heuristics.
            """
            ts = _envelope_iso8601_utc()
            envelope = build_relay_envelope(
                sender=user_name,
                recipient="BOTH",
                kind="interject",
                ts=ts,
                text=text,
            )
            _commit({
                "name": user_name,
                "text": text,
                "time": datetime.now().isoformat(),
                "from": user_name,
                "to": "BOTH",
                "kind": "relay-meta",
                "envelope_kind": "interject",
            })
            room_control.render_transcript_line(user_name, text, W)
            send_to_pane(panes[1], envelope)
            send_to_pane(panes[2], envelope)

        def _inject_to(target: str, text: str) -> None:
            sess = pane_for_profile(panes, slot_meta, target)
            if sess is None:
                room_control.render_transcript_line(
                    "ROOM", f"unknown target: {target}", DIM,
                )
                return
            ts = _envelope_iso8601_utc()
            envelope = build_relay_envelope(
                sender=user_name,
                recipient=get_display_name(target),
                kind="interject",
                ts=ts,
                text=text,
            )
            _commit({
                "name": user_name,
                "text": text,
                "time": datetime.now().isoformat(),
                "from": user_name,
                "to": target,
                "kind": "relay-meta",
                "envelope_kind": "interject",
            })
            room_control.render_transcript_line(
                user_name, f"(→ {target}) {text}", W,
            )
            send_to_pane(sess, envelope)

        def _open_transcript_pager() -> None:
            """Spawn `less -R` on the live transcript file.

            Three-step dance: tear the cockpit down, page, set it back up.
            The risky window is the re-setup — if it raises, the cockpit
            is gone and `render_transcript_line` would write into a bare
            terminal. In that case we log to plain stderr and ask the
            relay loop to drain after the in-flight turn so the user's
            session still saves cleanly instead of running into a void.
            """
            nonlocal end_after_turn
            tmp = ROOM_DIR / "transcript.live.txt"
            try:
                with open(tmp, "w") as fh:
                    for m in messages:
                        fh.write(f"{m.get('name', '?')}: {m.get('text', '')}\n\n")
            except Exception as e:
                room_control.render_transcript_line(
                    "ROOM", f"transcript pager failed (write): {e}", DIM,
                )
                return

            room_control.teardown_split_screen()
            try:
                subprocess.run(["less", "-R", str(tmp)])
            except Exception as e:
                sys.stderr.write(f"\nccoral: less failed: {e}\n")
                sys.stderr.flush()

            try:
                room_control.setup_split_screen()
            except Exception as e:
                sys.stderr.write(
                    f"\nccoral: cockpit re-entry failed: {e}\n"
                    f"ccoral: ending after in-flight turn — session will save.\n",
                )
                sys.stderr.flush()
                end_after_turn = True

        def _dispatch(event: tuple) -> bool:
            """Dispatch a user event. Returns True if the loop should keep
            running, False if we should break immediately.
            """
            nonlocal paused, end_after_turn
            kind = event[0]
            if kind == "say":
                _say_to_both(event[1])
            elif kind == "inject":
                _inject_to(event[1], event[2])
            elif kind == "pause":
                paused = True
                room_control.render_transcript_line("ROOM", "paused", DIM)
            elif kind == "resume":
                paused = False
                # /resume also clears any arbiter stalls so the operator's
                # explicit go-ahead beats backpressure timeouts.
                arbiter.manual_resume()
                room_control.render_transcript_line("ROOM", "resumed", DIM)
            elif kind == "end-after-turn":
                end_after_turn = True
                room_control.render_transcript_line(
                    "ROOM", "ending after current turn...", DIM,
                )
            elif kind == "stop":
                room_control.render_transcript_line("ROOM", "stop", DIM)
                return False
            elif kind == "save-now":
                try:
                    p = save_conversation(messages, [profile1, profile2])
                    room_control.render_transcript_line(
                        "ROOM", f"saved: {p}", DIM,
                    )
                except Exception as e:
                    room_control.render_transcript_line(
                        "ROOM", f"save failed: {e}", DIM,
                    )
            elif kind == "transcript":
                _open_transcript_pager()
            elif kind == "help":
                room_control.render_help()
            else:
                room_control.render_transcript_line(
                    "ROOM", f"unknown event: {event!r}", DIM,
                )
            return True

        def _handle_turn_record(reader_slot: int, record: dict) -> None:
            """Log + render + arbiter-dispatch a completed-turn record.

            Phase 8: speaker identity comes from `record["slot"]` (stamped
            by the proxy via CCORAL_ROOM_SLOT). The reader's slot is also
            available — they should agree. If `slot` is missing (legacy
            proxy or external producer), we fall back to the reader's
            slot and emit a one-shot WARN line.

            Phase 3: optional `config.max_chars_per_turn` soft cap.
            Anything over the cap is truncated in place with a marker
            and an entry in the room transcript noting the trim — the
            arbiter sees the truncated text, so the relay sends the
            shorter version downstream.
            """
            nonlocal turn_seq, turns_committed

            text = (record.get("text") or "").strip()
            if not text:
                return

            slot, _legacy = resolve_record_slot(record, reader_slot)
            if _legacy:
                # One yellow WARN per session per (slot, profile) — the
                # fallback path uses the FIFO reader's slot, never any
                # mtime / sink-ordering inference. Phase 12 verifies
                # this branch is unreachable against the in-tree server.
                room_control.warn_legacy_record(slot, record.get("profile"))

            other_slot = 3 - slot
            speaker = slot_meta[slot]["profile"]
            display = slot_meta[slot]["display"]
            color = slot_meta[slot]["color"]
            recipient_display = slot_meta[other_slot]["display"]

            # Phase 3 soft cap. Truncation is the simpler path: rather
            # than inserting a [SYSTEM] truncate-instruction (which would
            # need a round-trip to take effect), we trim before relay and
            # log the trim. The receiving Claude reads what we sent.
            cap = config.max_chars_per_turn
            truncated = False
            if cap is not None and len(text) > cap:
                text = text[:cap] + f"\n[truncated to {cap} chars]"
                truncated = True

            # Log + render the turn into the cockpit transcript. The
            # `from`/`to`/`envelope_kind` fields make Phase 5's export path
            # parseable without re-regexing the body — it just reads the
            # structured fields directly.
            committed = {
                "name": display,
                "text": text,
                "time": datetime.now().isoformat(),
                "stop_reason": record.get("stop_reason"),
                "request_id": record.get("request_id"),
                "kind": "turn",
                "from": display,
                "to": recipient_display,
                "envelope_kind": "turn",
                "slot": slot,
            }
            if truncated:
                committed["truncated"] = True
            _commit(committed)
            turns_committed += 1
            room_control.render_transcript_line(display, text, color)

            if paused:
                # Operator pause overrides arbiter; just transcribe.
                return

            # Hand to the arbiter and execute its decisions in order.
            # Phase 8: arbiter is still profile-keyed internally, but its
            # name-keyed `target` / `who` outputs are AMBIGUOUS when both
            # slots share a profile. Resolve destination by slot instead:
            # RELAY always goes to the other slot; BACKPRESSURE always
            # targets the current speaker's slot.
            stop_reason = record.get("stop_reason")
            for decision in arbiter.on_turn_record(speaker, text):
                kind = decision[0]
                if kind == TurnArbiter.RELAY:
                    _, relay_text, _target_name = decision
                    target_session = panes[other_slot]
                    target_display = slot_meta[other_slot]["display"]
                    turn_seq += 1
                    buffer_name = f"ccoral-room-{room_id}-{turn_seq}"
                    envelope = build_relay_envelope(
                        sender=display,
                        recipient=target_display,
                        kind="turn",
                        ts=_envelope_iso8601_utc(),
                        stop_reason=stop_reason,
                        text=relay_text,
                    )
                    relay_via_paste_buffer(
                        target_session, envelope, buffer_name,
                    )
                elif kind == TurnArbiter.BACKPRESSURE:
                    _, sys_text, _who_name, sys_reason = decision
                    sys_session = panes[slot]
                    sys_display = slot_meta[slot]["display"]
                    turn_seq += 1
                    buffer_name = f"ccoral-room-{room_id}-{turn_seq}"
                    envelope = build_relay_envelope(
                        sender="SYSTEM",
                        recipient=sys_display,
                        kind="system",
                        ts=_envelope_iso8601_utc(),
                        reason=sys_reason,
                        text=sys_text,
                    )
                    relay_via_paste_buffer(
                        sys_session, envelope, buffer_name,
                    )
                    _commit({
                        "name": "SYSTEM",
                        "text": sys_text,
                        "time": datetime.now().isoformat(),
                        "from": "SYSTEM",
                        "to": sys_display,
                        "reason": sys_reason,
                        "kind": "relay-meta",
                        "envelope_kind": "system",
                    })
                    room_control.render_transcript_line("SYSTEM", sys_text, DIM)

        # ───────────────────────────────────────────────────────────────

        # Build the select fd list (FIFO readers only — JSONL is polled
        # via the timeout tick). Phase 8: readers are slot-keyed.
        select_fds = []
        for slot in (1, 2):
            r = readers[slot]
            fd = r.fileno()
            if fd is not None:
                select_fds.append(fd)
        fd_to_slot = {readers[s].fileno(): s for s in (1, 2)
                      if readers[s].fileno() is not None}

        try:
            while True:
                # 1) Drain any input the user typed since last tick.
                while True:
                    cmd = room_control.read_command(timeout=0)
                    if cmd is None:
                        break
                    cmd_kind = cmd[0]
                    # Control commands always run immediately.
                    if cmd_kind in ("pause", "resume", "stop",
                                    "end-after-turn", "save-now",
                                    "transcript", "help"):
                        if not _dispatch(cmd):
                            raise KeyboardInterrupt
                    elif cmd_kind == "inject":
                        # `/to <p>` bypasses arbiter ordering and fires
                        # immediately to the named pane only — explicit
                        # operator override of turn discipline.
                        if not _dispatch(cmd):
                            raise KeyboardInterrupt
                    elif paused:
                        room_control.enqueue_user_event(cmd)
                    else:
                        # `/say` and other non-control: queue when a
                        # speaker is mid-turn so we land cleanly at the
                        # turn boundary; otherwise dispatch now.
                        if cmd_kind == "say":
                            room_control.enqueue_user_event(cmd)
                        else:
                            if not _dispatch(cmd):
                                raise KeyboardInterrupt

                # 2) Wait for FIFO bytes (or timeout).
                if select_fds:
                    try:
                        ready, _, _ = select.select(
                            select_fds, [], [], RELAY_SELECT_TIMEOUT,
                        )
                    except (InterruptedError, OSError) as e:
                        # SIGWINCH from terminal resize wakes select with
                        # EINTR on some libcs; not fatal.
                        if isinstance(e, OSError) and e.errno != errno.EINTR:
                            raise
                        ready = []
                else:
                    # JSONL-only path: no fds to select on; sleep briefly.
                    time.sleep(RELAY_SELECT_TIMEOUT)
                    ready = []

                # 3) Drain readable channels (FIFOs from select) +
                #    poll any JSONL readers regardless. Phase 8: keyed
                #    by slot. The reader's slot is passed alongside the
                #    record so the handler can fall back on it when a
                #    legacy record arrives without an explicit `slot`.
                turn_records = []
                for slot in (1, 2):
                    r = readers[slot]
                    fd = r.fileno()
                    if fd is not None and fd not in ready:
                        # FIFO wasn't readable — skip until next tick.
                        continue
                    for line in r.read_lines():
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        turn_records.append((slot, rec))

                # 4) Process each turn record through the arbiter.
                for reader_slot, record in turn_records:
                    _handle_turn_record(reader_slot, record)

                # 5) After a tick that landed turns, flush any queued
                #    user events at the turn boundary (Phase 1 contract).
                if turn_records and not paused:
                    for ev in room_control.drain_user_events():
                        if not _dispatch(ev):
                            raise KeyboardInterrupt

                # 6) End-after-turn honored once a turn just landed.
                if end_after_turn and turn_records:
                    break

                # 7) Phase 3: turn-limit auto-exit. The arbiter doesn't
                #    enforce this — it's an orchestrator-level cap so the
                #    operator gets a deterministic stop after N speaker
                #    turns. Counted via `turns_committed` (only completed
                #    proxy-emitted turns count; seeds and SYSTEM lines
                #    don't bump the counter).
                if (config.turn_limit is not None
                        and turns_committed >= config.turn_limit):
                    exit_reason = "turn_limit"
                    room_control.render_transcript_line(
                        "ROOM",
                        f"turn limit reached ({config.turn_limit}); exiting.",
                        DIM,
                    )
                    break

        except KeyboardInterrupt:
            exit_reason = "signal"
        finally:
            for r in readers.values():
                r.close()

    # Phase 9 C4: hand the closure to RoomApp and run the cockpit. The
    # App owns the terminal (alt screen, input, scroll region — Textual
    # handles all of it); _relay_runner executes on a background worker
    # thread spawned from on_mount. Blocks until /stop, Ctrl+C, or the
    # closure returns naturally (end-after-turn).
    #
    # C5: --legacy-cockpit branches to the bespoke split-screen here.
    # Same closure runs in both paths — only the terminal owner differs.
    if legacy_cockpit:
        with room_control.split_screen():
            _relay_runner()
    else:
        app = room_control.RoomApp(
            slot_meta=slot_meta,
            sink_paths=channels,
            relay_runner=_relay_runner,
        )
        app.run()

    # Phase 3: return messages plus the resolved exit_reason so run_room
    # can stamp meta.yaml with the right lifecycle field. exit_reason is
    # set by the inner runner — "clean" by default, "signal" on
    # KeyboardInterrupt, "turn_limit" when config.turn_limit trips.
    return messages, exit_reason


def export_conversation(resume: str, output: str = None) -> Path:
    """Export a saved conversation to clean markdown.

    Args:
        resume: "last", a filename, or a path to a JSON archive.
        output: Optional output path. Defaults to same dir as source, .md extension.

    Returns:
        Path to the exported markdown file.
    """
    data = load_conversation(resume)
    profiles = data["profiles"]
    messages = data.get("messages", [])
    started = data.get("started", "")

    if not messages:
        print(f"{Y}No messages to export.{NC}")
        sys.exit(1)

    # Parse date for header
    try:
        dt = datetime.fromisoformat(started)
        date_str = dt.strftime("%B %d, %Y")
        time_str = dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        date_str = started[:10] if started else "Unknown date"
        time_str = ""

    lines = []
    lines.append(f"# {profiles[0].title()} \u00d7 {profiles[1].title()}")
    lines.append("")
    lines.append(f"*{date_str}*{'  — ' + time_str if time_str else ''}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for msg in messages:
        name = msg.get("name", "UNKNOWN")
        text = msg.get("text", "").strip()

        if not text:
            continue

        # Skip JSON metadata messages (title objects, etc.)
        if text.startswith("{") and text.endswith("}"):
            try:
                json.loads(text)
                continue  # Skip machine-generated JSON
            except json.JSONDecodeError:
                pass

        # Phase 2: the old string-match export filter that lived here is
        # gone. It existed to drop transcript lines where a model echoed
        # our tmpfile-read plumbing back at us — the leak is gone
        # (paste-buffer relay) and so is the filter. Task 2.4 adds a
        # structured `kind: "relay-meta"` marker so meta lines
        # (backpressure SYSTEM messages, [CASSIUS] interjections) can be
        # filtered without string matching.

        # Format the speaker. The `name` field already carries the right
        # display label — host messages are written with the resolved
        # user_name (RoomConfig.user_name), so we don't need a special
        # branch on it. Phase 3 dropped the USER_NAME module constant.
        lines.append(f"**{name}:**")

        lines.append("")
        lines.append(text)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Recorded by ccoral room — {profiles[0]} \u00d7 {profiles[1]}*")

    content = "\n".join(lines)

    # Determine output path
    if output:
        out_path = Path(output)
    else:
        # Default: same directory as archives, .md extension
        timestamp = datetime.now().strftime("%Y-%m-%d")
        filename = f"{timestamp}_{profiles[0]}-{profiles[1]}.md"
        out_path = ROOMS_ARCHIVE / filename

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content)
    return out_path


def run_room(profile1: str, profile2: str, topic: str = None, resume: str = None,
             legacy_cockpit: bool = False,
             config: "RoomConfig | None" = None):
    """Main entry point for the room.

    legacy_cockpit (Phase 9 C5): when True, fall back to the bespoke
    room_control_legacy split-screen instead of the Textual RoomApp.
    One-release safety net behind the `--legacy-cockpit` CLI flag —
    Phase 12 deletes the flag (and the legacy module) once the new
    cockpit has run a verification week without regressions. See
    .plan/room-overhaul.md Phase 9 step 7 (line 245).

    Phase 3: `config` is the resolved RoomConfig from cmd_room. When
    None, a defaulted RoomConfig() is used so legacy programmatic
    callers still work without re-typing every flag.
    """
    if config is None:
        config = RoomConfig()

    prior_messages = None

    if resume:
        data = load_conversation(resume)
        profile1 = data["profiles"][0]
        profile2 = data["profiles"][1]
        prior_messages = data.get("messages", [])
        print(f"{DIM}Resuming {profile1} × {profile2} ({len(prior_messages)} messages){NC}")

    # Validate profiles
    for name in [profile1, profile2]:
        if not load_profile(name):
            print(f"Profile not found: {name}")
            available = [p["name"] for p in list_profiles()]
            print(f"Available: {', '.join(available)}")
            sys.exit(1)

    print(f"\n{Y}{'═' * 50}{NC}")
    print(f"  {BOLD}ccoral room{NC} — {profile1} × {profile2}")
    print(f"{Y}{'═' * 50}{NC}\n")

    # Setup
    ROOM_DIR.mkdir(parents=True, exist_ok=True)

    # Phase 5: when resuming, build the prior-exchange system block from
    # the loaded transcript and pass it to create_room_profiles so it
    # lands in the inject (operator-scope) instead of being dumped into
    # pane 1 as a chat message at relay-loop startup. resume_tail
    # bounds the included history; default 30 (was a hardcoded 10).
    prior_exchange_block = ""
    if prior_messages:
        prior_exchange_block = build_prior_exchange_block(
            prior_messages, tail=config.resume_tail,
        )

    print(f"{DIM}Creating room profiles...{NC}")
    room_profiles = create_room_profiles(
        profile1, profile2, user_name=config.user_name,
        prior_exchange=prior_exchange_block or None,
    )

    # Phase 2 + Phase 8: per-slot turn-record channel. Created BEFORE
    # proxy launch so the FIFO node exists when the proxy first tries to
    # open it for write. Slot-prefixed paths make duplicate-profile
    # invocations (`room blank blank`) safe — each slot writes to its
    # own sink instead of trampling a shared `<profile>_response.txt`.
    channels = {
        1: _setup_turn_channel(1, profile1),
        2: _setup_turn_channel(2, profile2),
    }
    for slot, (path, kind) in channels.items():
        label = f"slot{slot}/{(profile1, profile2)[slot - 1]}"
        print(f"{DIM}Channel[{label}] = {kind} ({path}){NC}")

    print(f"{DIM}Starting proxies on :{config.base_port} and :{config.base_port + 1}...{NC}")
    procs = start_proxies(room_profiles, channels=channels, config=config)

    p1_session = config.session_for(profile1)
    p2_session = config.session_for(profile2)
    print(f"{DIM}Setting up tmux sessions '{p1_session}' / '{p2_session}'...{NC}")
    setup_tmux(profile1, profile2, config=config)

    # Phase 3 C4: per-room state directory. Created BEFORE relay_loop so
    # config.yaml + meta.yaml(state=live) are on disk by the time the
    # cockpit comes up — Phase 11's live-room watcher only sees rooms
    # that have already stamped state=live.
    room_id = _make_room_id(profile1, profile2)
    config.room_id = room_id  # mirror into config so paste-buffer ns matches
    state = RoomState(
        room_id=room_id, config=config,
        profile1=profile1, profile2=profile2,
        channels=channels,
    )
    state.write_initial()
    print(f"{DIM}State dir: {state.dir}{NC}")

    messages: list = []
    exit_reason = "clean"
    try:
        messages, exit_reason = relay_loop(
            profile1, profile2, topic, prior_messages, channels=channels,
            legacy_cockpit=legacy_cockpit, config=config,
            on_turn_committed=state.append_turn,
        )
    except BaseException:
        # If the relay raised something other than KeyboardInterrupt,
        # the lifecycle gets stamped as "error" in meta.yaml. KeyboardInterrupt
        # surfaces as exit_reason="signal" from inside relay_loop, but a
        # crash above (or before relay_loop returns) needs its own marker.
        exit_reason = "error"
        raise
    finally:
        print(f"\n{DIM}Cleaning up...{NC}")

        # Phase 3 C4: the per-turn transcript.jsonl is the source of
        # truth. The legacy save_conversation JSON write is preserved
        # for one release so existing `--resume` paths keep working
        # (load_conversation reads .json from ROOMS_ARCHIVE). Phase 5
        # cuts over resume to the new dir.
        if messages:
            path = save_conversation(messages, [profile1, profile2])
            print(f"{DIM}Conversation saved (legacy): {path}{NC}")
        # Stamp the lifecycle ending. state=stopped + exit_reason match
        # the contract the C6 tests assert against.
        state.update_exit(exit_reason)
        print(f"{DIM}Meta stamped: state=stopped exit_reason={exit_reason}{NC}")

        # Stop proxies
        stop_proxies(procs)
        print(f"{DIM}Proxies stopped.{NC}")

        # Remove channel nodes (FIFO/JSONL) so a stale node from a crashed
        # run can't confuse the next launch.
        for slot, (path, _kind) in channels.items():
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"{DIM}Channel cleanup (slot{slot}): {e}{NC}")

        # Clean up temp profiles
        cleanup_room_profiles(profile1, profile2)
        print(f"{DIM}Temp profiles removed.{NC}")

        # Don't kill tmux sessions — user might want to review
        print(f"\n{DIM}tmux sessions still running:{NC}")
        print(f"{DIM}  tmux attach -t {p1_session}{NC}")
        print(f"{DIM}  tmux attach -t {p2_session}{NC}")
        print(f"{DIM}Kill both: tmux kill-session -t {p1_session} && tmux kill-session -t {p2_session}{NC}\n")
        print(f"{DIM}Exit reason: {exit_reason}{NC}")
