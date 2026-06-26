#!/usr/bin/env python3
"""
AgentProc bridge for the `codebuddy` CLI (Tencent CodeBuddy).

CodeBuddy's stream-json output schema is compatible with claude's. Differences:
    - command name: codebuddy
    - resume flag: -r <sessionId>  (instead of --resume)
    - env var prefix: CODEBUDDY_*  (instead of CLAUDE_*)
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "codebuddy"
INSTALL_HINT = "See your internal CodeBuddy installation docs."


def build_args(message: str, session_id: str, env) -> list[str]:
    args = [
        CLI_NAME, "-p", message,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    disallow = env.get("CODEBUDDY_DISALLOW_TOOLS", "AskUserQuestion")
    if disallow.strip():
        args += ["--disallowedTools", disallow]
    model = env.get("CODEBUDDY_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["-r", session_id]
    return args


def parse_event(event: dict) -> Optional[EventResult]:
    etype = event.get("type")
    if etype == "assistant":
        text = "".join(
            b.get("text", "")
            for b in (event.get("message") or {}).get("content", [])
            if b.get("type") == "text"
        )
        return EventResult(partial_text=text) if text else None
    if etype == "result":
        session_id = event.get("session_id")
        if event.get("is_error"):
            return EventResult(
                session_id=session_id,
                error=event.get("result", "codebuddy reported an error"),
            )
        result_text = event.get("result", "")
        return EventResult(
            session_id=session_id,
            final_text=result_text if result_text else None,
        )
    return None


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event))
