#!/usr/bin/env python3
"""
AgentProc bridge for the `opencode` CLI.

Invokes:
    opencode run <message> --auto --format json \
        [--session <session_id>] \
        [--model <model>]

Parses the NDJSON stream and re-emits as AgentProc protocol output:
    step_start  → captures sessionID (stamped on partial/result/error)
    text        → part.text → {"type":"partial"} (streaming) or reply body (one-shot)
    step_finish → terminal event; part.reason="stop" signals the final turn
    tool_use    → captures sessionID (content not forwarded to user)
    error       → {"type":"error"}

The sessionID field is present on every event (format: ses_XXX). The bridge
captures it from the first event and stamps it on partial/result/error events.

Env vars:
    turn.message          User message
    turn.session_id       Previous session id (empty = new session)
    streaming        "1" streaming mode, "0" one-shot
    OPENCODE_MODEL         Optional model (e.g. "anthropic/claude-opus-4-5")
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "opencode"
INSTALL_HINT = "Install: npm install -g opencode-ai  (or: curl -fsSL https://opencode.ai/install | bash)"


def build_args(message: str, session_id: str, env) -> list[str]:
    args = ["opencode", "run", message, "--auto", "--format", "json"]
    if session_id:
        args += ["--session", session_id]
    model = env.get("OPENCODE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


def parse_event(event: dict) -> Optional[EventResult]:
    etype = event.get("type")
    session_id = event.get("sessionID") or None
    part = event.get("part") or {}

    if etype == "text":
        text = part.get("text", "")
        if text:
            return EventResult(session_id=session_id, partial_text=text)
        return EventResult(session_id=session_id) if session_id else None

    if etype in ("step_start", "step_finish", "tool_use"):
        return EventResult(session_id=session_id) if session_id else None

    if etype == "error":
        err = (
            part.get("message")
            or (event.get("error") or {}).get("message")
            or "opencode reported an error"
        )
        return EventResult(session_id=session_id, error=err)

    return None


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event))
