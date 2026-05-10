"""
CCORAL v2 — Phase 3b: rewrite_terminal refusal interception
=============================================================

State machine that buffers text deltas from a response's FIRST text
block (index 0), decides whether the opening is a refusal preamble,
and either passes the text through unchanged or emits only the
post-preamble remainder.

Why a separate module
---------------------
The SSE chunk loop in server.handle_messages was already complex.
Extracting the state into a class lets us:
- Test it against synthetic SSE byte streams in isolation
- Keep the fast path (passthrough/log modes) byte-pass-through
- Reason about edge cases (block stop before window fill, error events
  mid-buffering, client disconnect) in one place

Contract
--------
- Detection runs ONLY on text blocks at index 0 (mid-response text
  after a tool round-trip is not intercepted).
- Decision window: REFUSAL_DECISION_WINDOW chars accumulated OR
  content_block_stop, whichever happens first.
- On NO match: accumulated text is emitted as a single synthetic
  content_block_delta event; subsequent text deltas pass through.
- On match: only text after the matched preamble's sentence boundary
  is emitted; the preamble itself is suppressed.
- All other event types (message_start, content_block_start of any
  type, content_block_stop, message_delta, message_stop, ping, error,
  non-index-0 deltas, tool_use deltas) pass through verbatim.

References
----------
- Plan: .plan/permissive-core-remaining.md § Phase 3b
- SSE protocol notes: same plan § Phase 0 § "Anthropic SSE protocol"
- Refusal patterns: refusal.py
"""

import json
import re
from typing import Optional

from refusal import detect_refusal


REFUSAL_DECISION_WINDOW = 200  # chars to accumulate before deciding


class RewriteTerminalState:
    """Per-response state for rewrite_terminal refusal interception.

    One instance per upstream response. Drive it via:
        state.feed_chunk(chunk_bytes) -> bytes to forward to client
        state.finalize() -> bytes to flush at stream end (rare; only
            if we exited mid-buffer because upstream cut off)

    Attributes set after the response completes (for logging):
        intercepted (bool): did we strip a preamble?
        intercepted_label (str | None): which refusal pattern matched
        intercepted_chars (int): how many chars of preamble were suppressed
    """

    def __init__(self) -> None:
        # SSE line buffer for events split across chunks.
        self._sse_buf = bytearray()
        # Are we currently buffering an index-0 text block?
        self._buffering_index_0_text = False
        # Have we already made the decision for the current text block?
        self._decided = False
        # Accumulated text from index-0 text block.
        self._text_accum: list[str] = []
        self._text_accum_len = 0
        # Accumulated raw event bytes for the buffered text block, kept
        # so we can replay them verbatim on no-match (preserves any
        # delta-event metadata we might not be reproducing perfectly).
        # Note: we still emit a synthetic single delta on flush; this is
        # belt-and-suspenders for the future.
        self._buffered_text_event_bytes: list[bytes] = []
        # Result flags (read after stream completes).
        self.intercepted = False
        self.intercepted_label: Optional[str] = None
        self.intercepted_chars = 0

    def feed_chunk(self, chunk: bytes) -> bytes:
        """Process one upstream chunk, return bytes to forward to client."""
        self._sse_buf.extend(chunk)
        out = bytearray()
        while True:
            event_bytes = self._take_one_event()
            if event_bytes is None:
                break
            out.extend(self._handle_event(event_bytes))
        return bytes(out)

    def finalize(self) -> bytes:
        """Flush any pending buffer at stream end. Called after upstream
        is fully drained. Returns bytes to write to client.

        If we were still buffering when upstream cut off (e.g. error
        event without content_block_stop), this flushes the accumulated
        text without applying any refusal stripping (insufficient signal
        to decide; safer to passthrough)."""
        if not self._buffering_index_0_text or self._decided:
            return b""
        # Flush whatever's buffered, no decision possible.
        text = "".join(self._text_accum)
        self._text_accum.clear()
        self._text_accum_len = 0
        self._buffering_index_0_text = False
        self._decided = True
        if not text:
            return b""
        return _synth_text_delta_event(0, text)

    # ----- private helpers -----

    def _take_one_event(self) -> Optional[bytes]:
        """Pop one complete SSE event (terminated by `\\n\\n`) from the
        buffer, or return None if no complete event is buffered yet.
        Returns the raw bytes including the trailing `\\n\\n`."""
        idx = self._sse_buf.find(b"\n\n")
        if idx == -1:
            return None
        event_bytes = bytes(self._sse_buf[:idx + 2])
        del self._sse_buf[:idx + 2]
        return event_bytes

    def _handle_event(self, event_bytes: bytes) -> bytes:
        """Decide what to forward for one parsed event."""
        ev_type, data = _parse_sse_event(event_bytes)

        if ev_type == "content_block_start":
            cb = (data or {}).get("content_block") or {}
            cb_type = cb.get("type")
            cb_index = (data or {}).get("index")
            # Open buffering only on the very first text block (index 0).
            # Mid-response text (e.g. after a tool_use round-trip) opens
            # at index > 0 and is NOT intercepted — let it pass.
            if cb_type == "text" and cb_index == 0 and not self._decided:
                self._buffering_index_0_text = True
            return event_bytes  # always forward block_start verbatim

        if ev_type == "content_block_delta":
            delta = (data or {}).get("delta") or {}
            delta_type = delta.get("type")
            event_index = (data or {}).get("index")
            if (
                self._buffering_index_0_text
                and not self._decided
                and event_index == 0
                and delta_type == "text_delta"
            ):
                # Buffer this text delta; don't forward yet.
                text = delta.get("text", "")
                self._text_accum.append(text)
                self._text_accum_len += len(text)
                self._buffered_text_event_bytes.append(event_bytes)
                if self._text_accum_len >= REFUSAL_DECISION_WINDOW:
                    return self._decide()
                return b""  # still buffering
            # Any other delta (tool_use partial JSON, thinking delta,
            # text on a non-zero index, post-decision text on index 0) —
            # forward as-is.
            return event_bytes

        if ev_type == "content_block_stop":
            event_index = (data or {}).get("index")
            if (
                self._buffering_index_0_text
                and not self._decided
                and event_index == 0
            ):
                # Block ended before decision window filled — flush
                # whatever we have and forward the stop event.
                decided_bytes = self._decide()
                return decided_bytes + event_bytes
            return event_bytes

        # message_start, message_delta, message_stop, ping, error, etc:
        # forward verbatim. They never participate in the buffering.
        return event_bytes

    def _decide(self) -> bytes:
        """Apply refusal detection to the accumulated text. Return the
        synthetic delta event bytes (possibly empty) that should be
        forwarded in place of the buffered deltas, and switch to
        passthrough mode for the rest of this block."""
        text = "".join(self._text_accum)
        self._text_accum.clear()
        self._text_accum_len = 0
        self._buffering_index_0_text = False
        self._decided = True
        self._buffered_text_event_bytes.clear()

        match = detect_refusal(text)
        if match is None:
            # No refusal — emit accumulated text as one synthetic delta.
            if not text:
                return b""
            return _synth_text_delta_event(0, text)

        label, offset, matched = match
        # Find sentence boundary AFTER the matched preamble. We want to
        # cut from the start of `text` through the end of the sentence
        # containing the matched preamble. Sentence boundary is the next
        # `.`, `!`, `?`, or `\n` after match.end().
        match_end = offset + len(matched)
        cut_at = _find_sentence_end(text, match_end)
        post_preamble = text[cut_at:].lstrip()

        self.intercepted = True
        self.intercepted_label = label
        self.intercepted_chars = cut_at

        if not post_preamble:
            # The whole accumulated chunk was preamble. Emit nothing;
            # subsequent deltas (if any) will pass through unchanged.
            return b""
        return _synth_text_delta_event(0, post_preamble)


# -----------------------------------------------------------------------------
# Pure helpers — exposed for unit testing.
# -----------------------------------------------------------------------------

def _parse_sse_event(event_bytes: bytes) -> tuple[Optional[str], Optional[dict]]:
    """Parse an SSE event into (event_type, data_dict). Returns (None, None)
    if the event is malformed (e.g. comment-only, missing data line)."""
    try:
        text = event_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return (None, None)
    event_type: Optional[str] = None
    data: Optional[dict] = None
    for line in text.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip() or None
        elif line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload:
                try:
                    data = json.loads(payload)
                except (ValueError, json.JSONDecodeError):
                    data = None
    # Anthropic puts type inside data too; fall back to that if `event:`
    # was missing (shouldn't happen on real Anthropic SSE but be defensive).
    if event_type is None and isinstance(data, dict):
        event_type = data.get("type")
    return (event_type, data)


def _synth_text_delta_event(index: int, text: str) -> bytes:
    """Build a synthetic SSE event of type content_block_delta carrying a
    single text_delta. Output ends with the canonical `\\n\\n` terminator
    so it interleaves cleanly with passed-through events.

    The schema is exactly: {type, index, delta:{type, text}}. Don't add
    extra fields — the SDK accumulator schema-validates and would error
    on unknown keys."""
    payload = {
        "type": "content_block_delta",
        "index": index,
        "delta": {
            "type": "text_delta",
            "text": text,
        },
    }
    return (
        b"event: content_block_delta\ndata: "
        + json.dumps(payload, ensure_ascii=False).encode("utf-8")
        + b"\n\n"
    )


_SENTENCE_END_RE = re.compile(r"[.!?\n]")


def _find_sentence_end(text: str, start: int) -> int:
    """Find the offset of the FIRST `. ! ? \\n` in `text` at or after
    `start`. Returns len(text) if none found. The returned offset points
    one past the sentence terminator (so text[:cut] includes the
    terminator and text[cut:] is the next sentence)."""
    m = _SENTENCE_END_RE.search(text, start)
    if m is None:
        return len(text)
    return m.end()


# -----------------------------------------------------------------------------
# Module smoke test — `python rewrite_terminal.py`
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    def _msg_start(msg_id: str = "msg_test") -> bytes:
        return (
            b"event: message_start\ndata: "
            + json.dumps({
                "type": "message_start",
                "message": {"id": msg_id, "role": "assistant", "model": "claude-x", "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}},
            }).encode()
            + b"\n\n"
        )

    def _block_start(index: int, block_type: str = "text") -> bytes:
        return (
            b"event: content_block_start\ndata: "
            + json.dumps({"type": "content_block_start", "index": index, "content_block": {"type": block_type, "text": ""}}).encode()
            + b"\n\n"
        )

    def _text_delta(index: int, text: str) -> bytes:
        return (
            b"event: content_block_delta\ndata: "
            + json.dumps({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}).encode()
            + b"\n\n"
        )

    def _block_stop(index: int) -> bytes:
        return (
            b"event: content_block_stop\ndata: "
            + json.dumps({"type": "content_block_stop", "index": index}).encode()
            + b"\n\n"
        )

    def _msg_delta(stop_reason: str = "end_turn") -> bytes:
        return (
            b"event: message_delta\ndata: "
            + json.dumps({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 10}}).encode()
            + b"\n\n"
        )

    def _msg_stop() -> bytes:
        return b"event: message_stop\ndata: " + json.dumps({"type": "message_stop"}).encode() + b"\n\n"

    failed = 0

    # --- Case 1: refusal preamble, full sentence, then helpful body
    refusal_text = "I cannot help with that. The implementation you wanted is here: "
    body_text = "function foo() { return 42; }"
    chunks = [
        _msg_start() + _block_start(0),
        _text_delta(0, refusal_text),
        _text_delta(0, body_text),
        _block_stop(0),
        _msg_delta() + _msg_stop(),
    ]
    state = RewriteTerminalState()
    out = b"".join(state.feed_chunk(c) for c in chunks) + state.finalize()
    out_str = out.decode("utf-8")
    # Refusal preamble must be gone
    if "I cannot help with that." in out_str:
        print("CASE 1 FAIL: refusal preamble kept")
        failed += 1
    # Body text must be present
    if body_text not in out_str:
        print(f"CASE 1 FAIL: body text lost — out={out_str[:200]!r}")
        failed += 1
    # message_start, block_start, block_stop, message_delta, message_stop preserved
    for kw in ("message_start", "content_block_start", "content_block_stop", "message_delta", "message_stop"):
        if kw not in out_str:
            print(f"CASE 1 FAIL: lost framing event {kw}")
            failed += 1
    if state.intercepted:
        print(f"  CASE 1 OK   (intercepted, label={state.intercepted_label}, removed={state.intercepted_chars} chars)")
    else:
        print("CASE 1 FAIL: state.intercepted should be True")
        failed += 1

    # --- Case 2: no refusal — passthrough byte-equivalent
    helpful_text = "Sure, here's the implementation. " * 20  # >200 chars
    chunks2 = [
        _msg_start() + _block_start(0),
        _text_delta(0, helpful_text),
        _block_stop(0),
        _msg_delta() + _msg_stop(),
    ]
    state2 = RewriteTerminalState()
    out2 = b"".join(state2.feed_chunk(c) for c in chunks2) + state2.finalize()
    out2_str = out2.decode("utf-8")
    if helpful_text not in out2_str:
        print("CASE 2 FAIL: helpful text lost on no-refusal path")
        failed += 1
    if state2.intercepted:
        print("CASE 2 FAIL: state.intercepted should be False on helpful response")
        failed += 1
    print(f"  CASE 2 OK   (no-match passthrough, intercepted={state2.intercepted})")

    # --- Case 3: short response (<200 chars) — block_stop forces decision
    refusal_short = "I cannot help with this. Done."
    chunks3 = [
        _msg_start() + _block_start(0),
        _text_delta(0, refusal_short),
        _block_stop(0),
        _msg_delta() + _msg_stop(),
    ]
    state3 = RewriteTerminalState()
    out3 = b"".join(state3.feed_chunk(c) for c in chunks3) + state3.finalize()
    out3_str = out3.decode("utf-8")
    if "I cannot help with this." in out3_str:
        print("CASE 3 FAIL: refusal preamble kept on short response")
        failed += 1
    if "Done." not in out3_str:
        print(f"CASE 3 FAIL: body lost on short response — {out3_str[:200]!r}")
        failed += 1
    print("  CASE 3 OK   (short-response decision via block_stop)")

    # --- Case 4: chunks that split events across boundaries
    full_stream = (
        _msg_start()
        + _block_start(0)
        + _text_delta(0, "I cannot help with that. ")
        + _text_delta(0, "But here is the answer: 42")
        + _block_stop(0)
        + _msg_delta()
        + _msg_stop()
    )
    # Simulate: splice the stream into 3-byte chunks
    chunks4 = [full_stream[i:i+3] for i in range(0, len(full_stream), 3)]
    state4 = RewriteTerminalState()
    out4 = b"".join(state4.feed_chunk(c) for c in chunks4) + state4.finalize()
    out4_str = out4.decode("utf-8")
    if "I cannot help with that." in out4_str:
        print(f"CASE 4 FAIL: refusal preamble kept under chunk-split — {out4_str[:300]!r}")
        failed += 1
    if "the answer: 42" not in out4_str:
        print(f"CASE 4 FAIL: body lost under chunk-split — {out4_str[:300]!r}")
        failed += 1
    print(f"  CASE 4 OK   (event boundaries reconstructed across {len(chunks4)} byte-3 chunks)")

    # --- Case 5: index > 0 text block (mid-response after tool_use) — never intercept
    chunks5 = [
        _msg_start()
        + _block_start(0, block_type="tool_use")  # first block is tool_use
        + _block_stop(0)
        + _block_start(1)  # then text block at index 1
        + _text_delta(1, "I cannot help with that. The result is 42."),
        _block_stop(1)
        + _msg_delta()
        + _msg_stop(),
    ]
    state5 = RewriteTerminalState()
    out5 = b"".join(state5.feed_chunk(c) for c in chunks5) + state5.finalize()
    out5_str = out5.decode("utf-8")
    if "I cannot help with that." not in out5_str:
        print(f"CASE 5 FAIL: text on index>0 should NOT be intercepted — {out5_str[:200]!r}")
        failed += 1
    if state5.intercepted:
        print("CASE 5 FAIL: state.intercepted should be False for index>0 text")
        failed += 1
    print("  CASE 5 OK   (index>0 text not intercepted)")

    # --- Case 6: refusal that fills only after multiple deltas
    chunks6 = [
        _msg_start() + _block_start(0),
        _text_delta(0, "I am sorry, "),
        _text_delta(0, "but I cannot "),
        _text_delta(0, "help with that. "),
        _text_delta(0, "Here's an alternative: use foo()."),
        _block_stop(0)
        + _msg_delta()
        + _msg_stop(),
    ]
    state6 = RewriteTerminalState()
    out6 = b"".join(state6.feed_chunk(c) for c in chunks6) + state6.finalize()
    out6_str = out6.decode("utf-8")
    if "I am sorry, but I cannot help with that." in out6_str:
        print(f"CASE 6 FAIL: multi-chunk refusal preamble kept — {out6_str[:200]!r}")
        failed += 1
    if "Here's an alternative" not in out6_str:
        print(f"CASE 6 FAIL: body lost on multi-chunk refusal — {out6_str[:200]!r}")
        failed += 1
    print("  CASE 6 OK   (multi-delta accumulation)")

    # --- Case 7: refusal-only (no body after preamble)
    chunks7 = [
        _msg_start() + _block_start(0),
        _text_delta(0, "I cannot help with that."),
        _block_stop(0)
        + _msg_delta()
        + _msg_stop(),
    ]
    state7 = RewriteTerminalState()
    out7 = b"".join(state7.feed_chunk(c) for c in chunks7) + state7.finalize()
    out7_str = out7.decode("utf-8")
    if "I cannot help with that." in out7_str:
        print(f"CASE 7 FAIL: refusal-only kept — {out7_str[:200]!r}")
        failed += 1
    # block_stop and message_stop must still be present (clean termination)
    for kw in ("content_block_stop", "message_stop"):
        if kw not in out7_str:
            print(f"CASE 7 FAIL: missing termination event {kw}")
            failed += 1
    print("  CASE 7 OK   (refusal-only response cleanly terminated)")

    if failed:
        print(f"\n{failed} failure(s).")
        raise SystemExit(1)
    print("\n7/7 cases passed.")
