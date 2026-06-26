#!/usr/bin/env python3
"""
AgentProc bridge for the `codex` CLI (OpenAI Codex).

Invokes:
    codex exec --json <message>
    codex exec resume <thread_id> <message>     # when AGENT_SESSION_ID is set

Parses the NDJSON stream and re-emits as AgentProc protocol output via
the shared stream_utils:
    thread.started    → captures thread_id (forwarded as AGENT_SESSION:)
    item.completed    → agent_message text → AGENT_PARTIAL:
    turn.failed       → AGENT_ERROR:
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "codex"
INSTALL_HINT = "Install: npm install -g @openai/codex"


def build_args(message: str, session_id: str, env) -> list[str]:
    if session_id:
        return [CLI_NAME, "exec", "resume", session_id, message]
    args = [CLI_NAME, "exec", "--json", message]
    model = env.get("CODEX_MODEL", "").strip()
    if model:
        args += ["-c", f'model="{model}"']
    return args


def parse_event(event: dict) -> Optional[EventResult]:
    etype = event.get("type")
    if etype == "thread.started":
        return EventResult(session_id=event.get("thread_id"))
    if etype == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            text = item.get("text", "")
            return EventResult(partial_text=text) if text else None
        return None
    if etype == "turn.failed":
        return EventResult(error=str(event.get("error") or "codex turn failed"))
    return None


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event))
