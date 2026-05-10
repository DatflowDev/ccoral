"""
CCORAL Room — Serve sidecar (Phase 6)
======================================

Minimal aiohttp single-file webpage at ``http://127.0.0.1:<port>/`` that
renders a room's transcript with the cockpit palette, SSE-streams new
turns as they land in ``transcript.jsonl``, and exposes a single text
input that POSTs to ``/say`` — which writes a ``{"kind": "say", "text":
...}`` line to the per-room control FIFO. The orchestrator's Phase 6
ControlFifo consumer (room.py) routes that into the cockpit's input
channel as if the operator had typed it.

Loopback only by default (binds to 127.0.0.1). The plan's anti-pattern
guard (room-overhaul.md line 516) explicitly bans 0.0.0.0 / host="" /
host=None defaults — over-LAN viewing is the ``--auth <token>`` stretch
goal; not shipped in this commit (TODO note below).

Pattern reference:
  - server.py:788 — aiohttp StreamResponse SSE shape (the proxy uses
    the same primitives to forward Anthropic's event stream).
  - room_app.tcss — color palette mirrored as inline CSS.
  - room.py ControlFifo / write_control_event — the producer-side
    helper that translates POST /say to a sidecar event.

References (verified against installed aiohttp==3.13.5):
  - web.Application / web.AppRunner:
      https://docs.aiohttp.org/en/stable/web_quickstart.html
  - web.StreamResponse + prepare/write/write_eof:
      https://docs.aiohttp.org/en/stable/web_reference.html#aiohttp.web.StreamResponse
  - asyncio.Queue + run_in_executor for the JSONL tailer:
      https://docs.python.org/3/library/asyncio-queue.html
"""

from __future__ import annotations

import asyncio
import html
import json
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web


# ---------------------------------------------------------------------------
# Color palette — mirrors room_app.tcss so the web view matches the
# Textual cockpit. Inline CSS so we ship a single self-contained file.
# ---------------------------------------------------------------------------

INLINE_CSS = """
body {
  background: #0a0a0a;
  color: #ddd;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  margin: 0;
  padding: 1rem;
}
#header { color: #888; margin-bottom: 0.5rem; }
#transcript {
  height: calc(100vh - 7rem);
  overflow-y: auto;
  border: 1px solid #333;
  border-radius: 4px;
  padding: 0.5rem 0.75rem;
  background: #050505;
  white-space: pre-wrap;
  word-break: break-word;
}
.line { margin: 0; padding: 2px 0; }
.speaker-1 { color: #d7d700; }     /* yellow — slot 1 */
.speaker-2 { color: #00cdcd; }     /* cyan — slot 2 */
.system    { color: #888; }
.host      { color: #fff; font-weight: bold; }
.warn      { color: #ff8800; }
#say-form {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.5rem;
}
#say-input {
  flex: 1;
  background: #111;
  border: 1px solid #444;
  color: #eee;
  padding: 0.4rem 0.6rem;
  border-radius: 4px;
  font-family: inherit;
}
#say-input:focus { outline: none; border-color: #00cdcd; }
#say-button {
  background: #222;
  color: #eee;
  border: 1px solid #444;
  padding: 0 1rem;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
}
#say-button:hover { background: #333; }
"""


# Inline JS — the only client-side logic: append SSE events to the
# transcript pane and POST the input form to /say. Vanilla JS so we
# ship without an external CDN dep.
INLINE_JS = """
(function() {
  const log = document.getElementById('transcript');
  const form = document.getElementById('say-form');
  const input = document.getElementById('say-input');

  function appendLine(rec) {
    const cls = rec.css || 'system';
    const div = document.createElement('div');
    div.className = 'line ' + cls;
    div.textContent = '  ' + (rec.name || '?') + ': ' + (rec.text || '');
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  // SSE: server pushes one JSON event per turn record.
  const es = new EventSource('/sse');
  es.onmessage = function(e) {
    if (!e.data) return;
    try { appendLine(JSON.parse(e.data)); }
    catch (_) {}
  };

  form.addEventListener('submit', function(ev) {
    ev.preventDefault();
    const text = input.value;
    if (!text) return;
    fetch('/say', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: text})
    }).then(function(r) {
      if (r.ok) input.value = '';
    });
  });
})();
"""


def _classify(rec: dict, *, user_name: str) -> str:
    """Per-record CSS class. Mirrors room_watch._classify."""
    slot = rec.get("slot")
    if isinstance(slot, int) and slot in (1, 2):
        return f"speaker-{slot}"
    name = (rec.get("name") or "").upper()
    if name in ("SYSTEM", "ROOM"):
        return "system"
    if name == user_name.upper():
        return "host"
    return "system"


def _render_record_payload(rec: dict, *, user_name: str) -> dict:
    """Strip a transcript record down to the SSE-payload shape the
    browser-side JS expects: {name, text, css}.
    """
    return {
        "name": rec.get("name") or "?",
        "text": rec.get("text") or "",
        "css": _classify(rec, user_name=user_name),
    }


# ---------------------------------------------------------------------------
# HTTP handlers.
# ---------------------------------------------------------------------------


async def index_handler(request: web.Request) -> web.Response:
    """Render the single-page UI with the existing transcript baked in
    so a fresh page-load shows backlog before the SSE stream starts.
    """
    state: ServerState = request.app["state"]
    rows = []
    for rec in state.snapshot_records():
        payload = _render_record_payload(rec, user_name=state.user_name)
        rows.append(
            f'<div class="line {payload["css"]}">  '
            f'{html.escape(payload["name"])}: '
            f'{html.escape(payload["text"])}</div>'
        )
    body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>ccoral room — {html.escape(state.room_id)}</title>
  <style>{INLINE_CSS}</style>
</head>
<body>
  <div id="header">
    ccoral room serve — <strong>{html.escape(state.room_id)}</strong>
    (loopback :{state.port})
  </div>
  <div id="transcript">{''.join(rows)}</div>
  <form id="say-form">
    <input id="say-input" type="text" autocomplete="off"
           placeholder="message (sent as {html.escape(state.user_name)})" />
    <button id="say-button" type="submit">say</button>
  </form>
  <script>{INLINE_JS}</script>
</body>
</html>
"""
    return web.Response(text=body, content_type="text/html")


async def sse_handler(request: web.Request) -> web.StreamResponse:
    """SSE stream of new transcript records.

    Re-uses the StreamResponse shape from server.py:788 — same headers,
    same prepare-then-write loop. We push one ``data:`` event per turn
    record. The client (vanilla EventSource) auto-reconnects on drop.
    """
    state: ServerState = request.app["state"]
    response = web.StreamResponse(
        status=200,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",  # disable proxy buffering, harmless on direct
        },
    )
    await response.prepare(request)

    queue = await state.subscribe()
    try:
        # Heartbeat every 15s so a dormant connection doesn't get
        # killed by an intermediary; harmless when the stream is busy.
        while True:
            try:
                rec = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                # SSE comment line — spec-allowed keepalive that the
                # browser ignores.
                await response.write(b": keepalive\n\n")
                continue
            payload = _render_record_payload(rec, user_name=state.user_name)
            data = json.dumps(payload, ensure_ascii=False)
            await response.write(f"data: {data}\n\n".encode("utf-8"))
    except (ConnectionResetError, asyncio.CancelledError):
        # Client disconnect — clean up.
        pass
    finally:
        state.unsubscribe(queue)
    return response


async def say_handler(request: web.Request) -> web.Response:
    """POST /say {"text": "..."} → write {"kind": "say", "text": ...}
    to the room's control FIFO. The orchestrator's ControlFifo consumer
    (room.py) routes the line through room_app's input channel exactly
    like a key press.
    """
    state: ServerState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    text = (body or {}).get("text") or ""
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "text required"}, status=400)
    # Late import — avoid pulling room.py at module import time.
    from room import write_control_event
    try:
        write_control_event(state.room_id, {"kind": "say", "text": text})
    except OSError as e:
        # No FIFO reader — the room isn't running. Surface a 503 so the
        # client can show the operator a useful error.
        return web.json_response(
            {"error": f"control sink unreachable: {e}"}, status=503,
        )
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# ServerState — shared between handlers + the JSONL tailer task.
# ---------------------------------------------------------------------------


class ServerState:
    """Per-server bookkeeping: SSE subscribers + last-seen byte cursor.

    The JSONL tailer task runs as an asyncio task that polls the file
    via run_in_executor (pathlib.Path.stat is blocking, but the cost is
    negligible at our poll cadence). Each new complete line is parsed
    and pushed to every subscribed queue.
    """

    def __init__(self, room_id: str, transcript_path: Path,
                 *, user_name: str = "CASSIUS", port: int = 8095) -> None:
        self.room_id = room_id
        self.transcript_path = Path(transcript_path)
        self.user_name = user_name
        self.port = port
        self._subscribers: list[asyncio.Queue] = []
        self._cursor = 0
        self._buf = b""
        self._snapshot: list[dict] = []
        # Bound the snapshot so a long-running room doesn't keep the
        # whole transcript in RAM. The browser only renders the most
        # recent slice on page load anyway.
        self._snapshot_max = 1000

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def snapshot_records(self) -> list[dict]:
        return list(self._snapshot)

    async def fan_out(self, rec: dict) -> None:
        self._snapshot.append(rec)
        if len(self._snapshot) > self._snapshot_max:
            del self._snapshot[: len(self._snapshot) - self._snapshot_max]
        for q in list(self._subscribers):
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                # Drop on slow consumer rather than block the tailer.
                pass

    async def prime_snapshot(self) -> None:
        """Read the existing transcript once on startup so the snapshot
        list is populated before the first SSE subscriber arrives.
        """
        if not self.transcript_path.exists():
            return
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            None, self.transcript_path.read_bytes,
        )
        self._cursor = len(text)
        for raw in text.split(b"\n"):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._snapshot.append(rec)
        if len(self._snapshot) > self._snapshot_max:
            del self._snapshot[: len(self._snapshot) - self._snapshot_max]

    async def tail_forever(self, *, poll_interval: float = 0.25) -> None:
        """Async tailer — polls the file, parses new lines, fans out
        each record to every subscriber.

        Runs as an asyncio task spawned from on_startup. Uses
        run_in_executor for the blocking stat / read calls so the
        event loop keeps servicing requests while we wait for I/O.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                size = await loop.run_in_executor(
                    None, self._stat_size,
                )
            except FileNotFoundError:
                await asyncio.sleep(poll_interval)
                continue
            if size < self._cursor:
                # Truncated / rotated.
                self._cursor = 0
                self._buf = b""
            if size > self._cursor:
                chunk = await loop.run_in_executor(
                    None, self._read_chunk, self._cursor, size - self._cursor,
                )
                self._cursor = size
                self._buf += chunk
                lines = self._buf.split(b"\n")
                self._buf = lines[-1]
                for raw in lines[:-1]:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    await self.fan_out(rec)
            else:
                await asyncio.sleep(poll_interval)

    def _stat_size(self) -> int:
        return self.transcript_path.stat().st_size

    def _read_chunk(self, offset: int, count: int) -> bytes:
        with open(self.transcript_path, "rb") as f:
            f.seek(offset)
            return f.read(count)


# ---------------------------------------------------------------------------
# App factory + entry point.
# ---------------------------------------------------------------------------


def make_app(state: ServerState) -> web.Application:
    """Build the aiohttp app and wire routes + lifecycle.

    Exposed as a factory so tests can drive it via aiohttp's
    test_utils.AioHTTPTestCase / TestClient without touching the
    network stack.
    """
    app = web.Application()
    app["state"] = state
    app.router.add_get("/", index_handler)
    app.router.add_get("/sse", sse_handler)
    app.router.add_post("/say", say_handler)

    async def _on_startup(app: web.Application) -> None:
        await state.prime_snapshot()
        app["tailer_task"] = asyncio.create_task(state.tail_forever())

    async def _on_cleanup(app: web.Application) -> None:
        task = app.get("tailer_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


# Loopback bind — the plan (line 516) bans 0.0.0.0 / "" / None as
# defaults. The --auth stretch goal (over-LAN viewing) is intentionally
# NOT shipped in Phase 6; if/when it lands it gets its own opt-in flag
# (e.g. --bind 0.0.0.0 --auth <token>) and an explicit log line on boot.
# TODO(phase6-stretch): --auth <token> for over-LAN viewing.
LOOPBACK_HOST = "127.0.0.1"


def run_serve(room_id: str, *, port: int = 8095,
              user_name: str = "CASSIUS") -> int:
    """Resolve `<id|last>` and run the aiohttp server synchronously.

    Returns 0 on clean exit, 1 on resolution failure. KeyboardInterrupt
    is treated as a clean exit (operator hit Ctrl+C in the terminal).
    """
    from room import _resolve_room_id

    target = _resolve_room_id(room_id)
    if target is None:
        print(f"room not found: {room_id}", file=sys.stderr)
        return 1
    transcript = target / "transcript.jsonl"
    if not transcript.exists():
        # Match watch's behavior: touch so the tailer has something
        # to stat. Fresh rooms write their first record only after
        # the first turn lands.
        transcript.touch()

    state = ServerState(
        room_id=target.name,
        transcript_path=transcript,
        user_name=user_name,
        port=port,
    )
    app = make_app(state)
    print(
        f"ccoral room serve — http://{LOOPBACK_HOST}:{port}/ "
        f"(room {target.name})",
        file=sys.stderr,
    )
    try:
        web.run_app(
            app,
            host=LOOPBACK_HOST,
            port=port,
            print=lambda *a, **kw: None,
        )
    except KeyboardInterrupt:
        return 0
    return 0
