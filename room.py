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
from datetime import datetime
from pathlib import Path

# Ensure imports from ccoral dir
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from profiles import load_profile, list_profiles
import room_control

# Colors
Y = "\033[33m"
C = "\033[36m"
W = "\033[1;37m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

# Config
ROOM_DIR = Path("/tmp/ccoral-room")
ROOMS_ARCHIVE = Path.home() / ".ccoral" / "rooms"
TEMP_PROFILES_DIR = Path.home() / ".ccoral" / "profiles"
TMUX_SESSION = "room"
BASE_PORT = 8090
USER_NAME = "CASSIUS"

# Phase 2: arbiter loop tick. Short timeout on select(); the loop is push-driven
# (FIFO bytes wake us instantly), the timeout only governs how often we poll
# stdin via the cockpit's own line-editor and check stall expiry.
RELAY_SELECT_TIMEOUT = 0.25
# Phase 2: backpressure defaults. Phase 3 will make these CLI-configurable.
BACKPRESSURE_TURNS_DEFAULT = 2
BACKPRESSURE_TIMEOUT_DEFAULT = 60.0


def get_display_name(profile_name: str) -> str:
    return profile_name.upper()


def create_room_profiles(profile1: str, profile2: str) -> dict:
    """Create temporary profiles with room relay instructions baked into inject."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    temp_names = {}

    for self_name, other_name in [(profile1, profile2), (profile2, profile1)]:
        base = load_profile(self_name)
        if not base:
            print(f"Profile not found: {self_name}")
            available = [p["name"] for p in list_profiles()]
            print(f"Available: {', '.join(available)}")
            sys.exit(1)

        self_display = get_display_name(self_name)
        other_display = get_display_name(other_name)

        room_instructions = f"""

## CONVERSATION ROOM

You are in a live conversation with {other_display}. {USER_NAME} is the human host.

When you see a message that starts with "[{other_display}]" — that is {other_display} speaking to you.
Respond to them in character, conversationally. Keep responses to 1-3 paragraphs unless the
topic demands more. Just talk. Don't use tools, don't write files, don't use markdown headers.
Be present in the conversation.

If you see "[{USER_NAME}]" — that is the human host interjecting. Acknowledge them naturally.
"""

        modified_inject = base.get("inject", "") + room_instructions

        temp_profile = {
            "name": f"{self_name}-room",
            "description": f"{base.get('description', '')} (room mode)",
            "preserve": base.get("preserve", []),
            "inject": modified_inject,
        }
        if base.get("minimal"):
            temp_profile["minimal"] = True

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


def start_proxies(room_profiles: dict, channels: dict | None = None) -> list:
    """Start two CCORAL proxy instances with room profiles.

    `channels`, when provided, maps base_profile_name -> (path, kind). The
    proxy is told to push completed-turn records there via
    `CCORAL_RESPONSE_FIFO` (when kind=="fifo") or `CCORAL_RESPONSE_JSONL`
    (kind=="jsonl"). When `channels` is None we fall back to the legacy
    `CCORAL_RESPONSE_FILE` path so non-arbiter callers still work.
    """
    server_path = SCRIPT_DIR / "server.py"
    procs = []

    # Per-port log files. stdout=PIPE deadlocks here (no reader drains the pipe
    # while this process is busy orchestrating tmux panes), so each proxy gets
    # its own daily log file.
    log_dir = Path.home() / ".ccoral" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    for i, (base_name, room_name) in enumerate(room_profiles.items()):
        port = BASE_PORT + i
        env = os.environ.copy()
        env["CCORAL_PORT"] = str(port)
        env["CCORAL_PROFILE"] = room_name
        env["CCORAL_LOG"] = "0"
        if channels and base_name in channels:
            ch_path, ch_kind = channels[base_name]
            if ch_kind == "fifo":
                env["CCORAL_RESPONSE_FIFO"] = str(ch_path)
            else:
                env["CCORAL_RESPONSE_JSONL"] = str(ch_path)
        else:
            # Legacy fallback for non-arbiter callers.
            env["CCORAL_RESPONSE_FILE"] = str(ROOM_DIR / f"{base_name}_response.txt")
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


def setup_tmux(profile1: str, profile2: str) -> bool:
    """Create two separate tmux sessions, one per Claude instance."""

    p1_session = f"room-{profile1}"
    p2_session = f"room-{profile2}"

    port1 = BASE_PORT
    port2 = BASE_PORT + 1
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


def pane_for_profile(panes: dict, target: str):
    """Resolve `/to <target>` to a session name.

    Accepts the bare profile name ("blank"), the per-pane suffix
    ("blank-1", "blank-2") that the plan's verification commands use, or
    the bare digits "1"/"2". Match is case-insensitive across all forms,
    so `/to BLANK-1` resolves the same as `/to blank-1`. Returns None on
    no match.
    """
    if target in panes:
        return panes[target]
    keys = list(panes.keys())
    # Bare digits.
    if target == "1":
        return panes[keys[0]]
    if len(keys) > 1 and target == "2":
        return panes[keys[1]]
    # `<profile>-1` / `<profile>-2`, case-insensitive on the prefix.
    tlow = target.lower()
    if tlow == f"{keys[0].lower()}-1":
        return panes[keys[0]]
    if len(keys) > 1 and tlow == f"{keys[1].lower()}-2":
        return panes[keys[1]]
    # Plain case-insensitive name match.
    for k, v in panes.items():
        if k.lower() == tlow:
            return v
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
    """Load a saved conversation."""
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
        backpressure_turns: int = BACKPRESSURE_TURNS_DEFAULT,
        backpressure_timeout: float = BACKPRESSURE_TIMEOUT_DEFAULT,
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
        if self.consecutive_solo[speaker] >= self.cap:
            sys_text = f"[SYSTEM] {other.upper()} hasn't replied yet — pausing."
            self.stalled_until[speaker] = time.monotonic() + self.timeout
            decisions.append((self.BACKPRESSURE, sys_text, speaker))

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


def _setup_turn_channel(profile_name: str) -> tuple[Path, str]:
    """Create the per-profile turn-record channel (FIFO when supported,
    JSONL fallback). Returns (path, channel_kind).

    Detection rule: try mkfifo; on OSError (ENOTSUP, EPERM, etc.) fall
    back to a writable JSONL path. The orchestrator logs the choice once
    at startup so the operator knows which path is live.
    """
    fifo_path = ROOM_DIR / f"{profile_name}.fifo"
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

    jsonl_path = ROOM_DIR / f"{profile_name}.jsonl"
    try:
        jsonl_path.unlink()
    except FileNotFoundError:
        pass
    jsonl_path.touch()
    return jsonl_path, "jsonl"


def relay_loop(profile1: str, profile2: str, topic: str = None,
               prior_messages: list = None,
               channels: dict | None = None):
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
    """
    # Per-run room id used to namespace tmux paste buffers so two rooms
    # running concurrently don't trample each other's relay payloads.
    # Phase 3 replaces this synthetic id with the real `RoomConfig.room_id`.
    room_id = f"{profile1}-{profile2}-{int(time.time())}"
    turn_seq = 0

    if channels is None:
        # Late binding for callers that didn't pre-create channels. This
        # path is only used by tests / non-room callers — the production
        # `run_room` entry point pre-creates them so the proxy is told
        # the path before it boots.
        channels = {
            profile1: _setup_turn_channel(profile1),
            profile2: _setup_turn_channel(profile2),
        }

    # Conversation log
    messages = prior_messages or []

    # Map profiles to tmux session names
    panes = {
        profile1: f"room-{profile1}",
        profile2: f"room-{profile2}",
    }
    colors_map = {
        profile1: Y,
        profile2: C,
    }

    # Per-profile channel readers. FIFO uses select; JSONL polls.
    readers = {}
    for name in (profile1, profile2):
        ch_path, ch_kind = channels[name]
        if ch_kind == "fifo":
            readers[name] = _FifoReader(ch_path)
        else:
            readers[name] = _JsonlTailReader(ch_path)

    # Arbiter — strict turn order + backpressure.
    arbiter = TurnArbiter(profile1, profile2)

    # Give Claude sessions time to start up
    room_control.set_user(USER_NAME)

    with room_control.split_screen():
        # One-time channel-mode banner so the operator sees which path is
        # live (FIFO is always preferred; JSONL only on platforms where
        # mkfifo failed). Useful for post-mortem on resume + audit logs.
        for name in (profile1, profile2):
            _, kind = channels[name]
            room_control.render_transcript_line(
                "ROOM", f"channel[{name}] = {kind}", DIM,
            )

        room_control.render_transcript_line(
            "ROOM", f"waiting for Claude sessions to initialize...", DIM,
        )
        time.sleep(8)

        # Send initial topic to profile1 only — profile2 hears it through the relay
        if topic and not prior_messages:
            initial_msg = f"[{USER_NAME}] {topic}"
            send_to_pane(panes[profile1], initial_msg)
            messages.append({
                "name": USER_NAME,
                "text": topic,
                "time": datetime.now().isoformat(),
                # Initial host seed — meta from the conversation's POV.
                "kind": "relay-meta",
            })
            room_control.render_transcript_line(USER_NAME, topic, W)

        # If resuming, send context to both panes (kept as Phase 1 behavior;
        # Phase 5 replaces this with a system-note inject regeneration).
        if prior_messages:
            context = "Previous conversation context:\\n"
            for msg in prior_messages[-10:]:  # Last 10 messages
                context += f"{msg['name']}: {msg['text']}\\n"
            context += "\\nContinue the conversation from where you left off."
            send_to_pane(panes[profile1], context)
            time.sleep(1)
            send_to_pane(panes[profile2], context)

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
            messages.append({
                "name": USER_NAME,
                "text": text,
                "time": datetime.now().isoformat(),
                "kind": "relay-meta",
            })
            room_control.render_transcript_line(USER_NAME, text, W)
            send_to_pane(panes[profile1], f"[{USER_NAME}] {text}")
            send_to_pane(panes[profile2], f"[{USER_NAME}] {text}")

        def _inject_to(target: str, text: str) -> None:
            sess = pane_for_profile(panes, target)
            if sess is None:
                room_control.render_transcript_line(
                    "ROOM", f"unknown target: {target}", DIM,
                )
                return
            messages.append({
                "name": USER_NAME,
                "text": text,
                "time": datetime.now().isoformat(),
                "to": target,
                "kind": "relay-meta",
            })
            room_control.render_transcript_line(
                USER_NAME, f"(→ {target}) {text}", W,
            )
            send_to_pane(sess, f"[{USER_NAME}] {text}")

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

        def _handle_turn_record(speaker: str, record: dict) -> None:
            """Log + render + arbiter-dispatch a completed-turn record."""
            nonlocal turn_seq

            text = (record.get("text") or "").strip()
            if not text:
                return

            display = get_display_name(speaker)
            color = colors_map[speaker]

            # Log + render the turn into the cockpit transcript.
            messages.append({
                "name": display,
                "text": text,
                "time": datetime.now().isoformat(),
                "stop_reason": record.get("stop_reason"),
                "request_id": record.get("request_id"),
                "kind": "turn",
            })
            room_control.render_transcript_line(display, text, color)

            if paused:
                # Operator pause overrides arbiter; just transcribe.
                return

            # Hand to the arbiter and execute its decisions in order.
            for decision in arbiter.on_turn_record(speaker, text):
                kind = decision[0]
                if kind == TurnArbiter.RELAY:
                    _, relay_text, target = decision
                    target_session = panes[target]
                    turn_seq += 1
                    buffer_name = f"ccoral-room-{room_id}-{turn_seq}"
                    formatted = f"[{display}] {relay_text}"
                    relay_via_paste_buffer(
                        target_session, formatted, buffer_name,
                    )
                elif kind == TurnArbiter.BACKPRESSURE:
                    _, sys_text, who = decision
                    sys_session = panes[who]
                    turn_seq += 1
                    buffer_name = f"ccoral-room-{room_id}-{turn_seq}"
                    relay_via_paste_buffer(
                        sys_session, sys_text, buffer_name,
                    )
                    messages.append({
                        "name": "SYSTEM",
                        "text": sys_text,
                        "time": datetime.now().isoformat(),
                        "to": who,
                        "kind": "relay-meta",
                    })
                    room_control.render_transcript_line("SYSTEM", sys_text, DIM)

        # ───────────────────────────────────────────────────────────────

        # Build the select fd list (FIFO readers only — JSONL is polled
        # via the timeout tick).
        select_fds = []
        for name in (profile1, profile2):
            r = readers[name]
            fd = r.fileno()
            if fd is not None:
                select_fds.append(fd)
        fd_to_name = {readers[n].fileno(): n for n in (profile1, profile2)
                      if readers[n].fileno() is not None}

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
                #    poll any JSONL readers regardless.
                turn_records = []
                for name in (profile1, profile2):
                    r = readers[name]
                    fd = r.fileno()
                    if fd is not None and fd not in ready:
                        # FIFO wasn't readable — skip until next tick.
                        continue
                    for line in r.read_lines():
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        turn_records.append((name, rec))

                # 4) Process each turn record through the arbiter.
                for speaker, record in turn_records:
                    _handle_turn_record(speaker, record)

                # 5) After a tick that landed turns, flush any queued
                #    user events at the turn boundary (Phase 1 contract).
                if turn_records and not paused:
                    for ev in room_control.drain_user_events():
                        if not _dispatch(ev):
                            raise KeyboardInterrupt

                # 6) End-after-turn honored once a turn just landed.
                if end_after_turn and turn_records:
                    break

        except KeyboardInterrupt:
            pass
        finally:
            for r in readers.values():
                r.close()

    return messages


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

        # Format the speaker
        if name == USER_NAME:
            lines.append(f"**{USER_NAME}:**")
        else:
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


def run_room(profile1: str, profile2: str, topic: str = None, resume: str = None):
    """Main entry point for the room."""

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

    print(f"{DIM}Creating room profiles...{NC}")
    room_profiles = create_room_profiles(profile1, profile2)

    # Phase 2: per-profile turn-record channel. Created BEFORE proxy launch
    # so the FIFO node exists when the proxy first tries to open it for
    # write. Falls back to JSONL on platforms where `mkfifo` fails.
    channels = {
        profile1: _setup_turn_channel(profile1),
        profile2: _setup_turn_channel(profile2),
    }
    for name, (path, kind) in channels.items():
        print(f"{DIM}Channel[{name}] = {kind} ({path}){NC}")

    print(f"{DIM}Starting proxies on :{BASE_PORT} and :{BASE_PORT + 1}...{NC}")
    procs = start_proxies(room_profiles, channels=channels)

    print(f"{DIM}Setting up tmux session '{TMUX_SESSION}'...{NC}")
    setup_tmux(profile1, profile2)

    try:
        messages = relay_loop(
            profile1, profile2, topic, prior_messages, channels=channels,
        )
    finally:
        print(f"\n{DIM}Cleaning up...{NC}")

        # Save conversation
        if 'messages' in dir() and messages:
            path = save_conversation(messages, [profile1, profile2])
            print(f"{DIM}Conversation saved: {path}{NC}")

        # Stop proxies
        stop_proxies(procs)
        print(f"{DIM}Proxies stopped.{NC}")

        # Remove channel nodes (FIFO/JSONL) so a stale node from a crashed
        # run can't confuse the next launch.
        for name, (path, _kind) in channels.items():
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"{DIM}Channel cleanup ({name}): {e}{NC}")

        # Clean up temp profiles
        cleanup_room_profiles(profile1, profile2)
        print(f"{DIM}Temp profiles removed.{NC}")

        # Don't kill tmux sessions — user might want to review
        p1s = f"room-{profile1}"
        p2s = f"room-{profile2}"
        print(f"\n{DIM}tmux sessions still running:{NC}")
        print(f"{DIM}  tmux attach -t {p1s}{NC}")
        print(f"{DIM}  tmux attach -t {p2s}{NC}")
        print(f"{DIM}Kill both: tmux kill-session -t {p1s} && tmux kill-session -t {p2s}{NC}\n")
