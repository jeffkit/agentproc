#!/usr/bin/env python3
"""
AgentProc bridge for the `grok` CLI (xAI Grok Build).

Invokes:
    grok -p <message> --output-format streaming-json \\
        --always-approve --no-auto-update \\
        [-r <session_id>] [-m <model>]

Parses the NDJSON stream and re-emits as AgentProc protocol output:
    text     → coalesced into block-sized {"type":"partial"} (not per-token)
    thought  → ignored (reasoning tokens)
    end      → sessionId + accumulated final_text (for streaming:false)
    error    → {"type":"error"}

Grok's streaming-json emits near token-sized ``text`` events. Claude Code
emits larger assistant content blocks — we coalesce here so IM clients see
block-shaped streaming instead of character drip.

Schema verified against grok 0.2.101.

Env vars:
    turn.message          User message
    turn.session_id       Previous session id (empty = new session)
    streaming             "1" streaming mode, "0" one-shot
    XAI_API_KEY           Optional auth (alternative to `grok login`)
    GROK_MODEL            Optional model override (e.g. "grok-4.5")
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "grok"
INSTALL_HINT = "Install: curl -fsSL https://x.ai/cli/install.sh | bash"

# Coalesce token-sized grok ``text`` events into Claude-like blocks.
# Soft: flush on sentence/paragraph boundary once we have a bit of text.
# Hard: flush anyway so a long run without punctuation still streams.
_SOFT_CHARS = 40
_HARD_CHARS = 80
_BOUNDARY = frozenset("\n。！？；.!?;")


def _should_flush(buf: str) -> bool:
    if not buf:
        return False
    if len(buf) >= _HARD_CHARS:
        return True
    if buf[-1] in _BOUNDARY and len(buf) >= _SOFT_CHARS:
        return True
    # Newlines are strong block boundaries even for short lines.
    if buf[-1] == "\n":
        return True
    return False


def build_args(message: str, session_id: str, env) -> list[str]:
    args = [
        CLI_NAME, "-p", message,
        "--output-format", "streaming-json",
        "--always-approve",
        "--no-auto-update",
    ]
    model = env.get("GROK_MODEL", "").strip()
    if model:
        args += ["-m", model]
    if session_id:
        args += ["-r", session_id]
    return args


def make_parse_event():
    """Coalesce token text into blocks; keep full text for streaming:false."""
    full: list[str] = []
    pending = ""

    def _flush_pending() -> Optional[str]:
        nonlocal pending
        if not pending:
            return None
        chunk = pending
        pending = ""
        return chunk

    def parse_event(event: dict) -> Optional[EventResult]:
        nonlocal pending
        etype = event.get("type")
        if etype == "text":
            data = event.get("data") or ""
            if not data:
                return None
            full.append(data)
            pending += data
            if _should_flush(pending):
                return EventResult(partial_text=_flush_pending())
            return None
        if etype == "thought":
            # Reasoning tokens — not part of the user-facing reply.
            return None
        if etype == "end":
            sid = event.get("sessionId")
            # Drain any leftover tokens as a final partial block.
            leftover = _flush_pending()
            return EventResult(
                session_id=sid if isinstance(sid, str) and sid else None,
                partial_text=leftover,
                final_text="".join(full),
            )
        if etype == "error":
            sid = event.get("sessionId")
            _flush_pending()  # discard unsent buffer; turn failed
            return EventResult(
                session_id=sid if isinstance(sid, str) and sid else None,
                error=event.get("message") or "grok reported an error",
            )
        return None

    return parse_event


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, make_parse_event()))
