#!/usr/bin/env python3
"""
AgentProc bridge for the `gemini` CLI (Google Gemini CLI).

Invokes:
    gemini -p <message> --output-format stream-json --yolo \\
        [--resume <session_id>] \\
        [--model <model>]

Parses the NDJSON stream and re-emits as AgentProc protocol output:
    init    → captures session_id (forwarded as AGENT_SESSION:)
    message → text delta or full text → AGENT_PARTIAL:
    error   → AGENT_ERROR:
    result  → terminal event (status=error → AGENT_ERROR:)

Gemini emits session_id up-front in `init`, so the bridge forwards it
immediately. The "last wins" rule also tolerates a later session_id.

Env vars:
    AGENT_MESSAGE          User message
    AGENT_SESSION_ID       Previous session id (empty = new session)
    AGENT_STREAMING        "1" streaming mode, "0" one-shot
    GEMINI_MODEL           Optional model override (e.g. "gemini-2.5-pro", "flash")
    GEMINI_SANDBOX         Optional: "false" disables --sandbox; default keeps gemini's default
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "gemini"
INSTALL_HINT = "Install: npm install -g @google/gemini-cli"


def build_args(message: str, session_id: str, env) -> list[str]:
    args = [
        CLI_NAME, "-p", message,
        "--output-format", "stream-json",
        "--yolo",  # auto-approve tool calls (headless equivalent of claude's --dangerously-skip-permissions)
    ]
    if env.get("GEMINI_SANDBOX", "").strip().lower() == "false":
        args += ["--sandbox", "false"]
    model = env.get("GEMINI_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    return args


def parse_event(event: dict) -> Optional[EventResult]:
    etype = event.get("type")
    if etype == "init":
        return EventResult(session_id=event.get("session_id"))
    if etype == "message":
        if event.get("role") != "assistant":
            return None
        text = event.get("content", "")
        if not text:
            return None
        # delta=true means streaming chunk; delta=false or absent means the full
        # message text. We treat both as partial — the dedup logic in the shared
        # runner handles the streaming case, and the final_text fallback handles
        # the one-shot case.
        if event.get("delta"):
            return EventResult(partial_text=text)
        return EventResult(final_text=text)
    if etype == "error":
        # severity 'warning' is recoverable; only 'error' aborts.
        if event.get("severity") == "error":
            return EventResult(error=event.get("message", "gemini reported an error"))
        return None
    if etype == "result":
        if event.get("status") == "error":
            err = event.get("error") or {}
            return EventResult(error=err.get("message") or "gemini turn failed")
        return None
    return None


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event))
