#!/usr/bin/env python3
"""
AgentProc bridge for the `claude` CLI (Anthropic Claude Code).

Invokes:
    claude -p <message> --output-format stream-json \\
        --dangerously-skip-permissions \\
        --disallowed-tools AskUserQuestion \\
        [--resume <session_id>] \\
        [--model <model>]

Re-emits the stream as AgentProc protocol output via the shared stream_utils.
"""

from __future__ import annotations

import os
import sys
import sys as _sys
from dataclasses import dataclass
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in _sys.path:
    _sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "claude"
INSTALL_HINT = "Install: npm install -g @anthropic-ai/claude-code"


def build_args(message: str, session_id: str, env) -> list[str]:
    args = [
        CLI_NAME, "-p", message,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    disallow = env.get("CLAUDE_DISALLOW_TOOLS", "AskUserQuestion")
    if disallow.strip():
        args += ["--disallowed-tools", disallow]
    model = env.get("CLAUDE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
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
                error=event.get("result", "claude reported an error"),
            )
        result_text = event.get("result", "")
        return EventResult(
            session_id=session_id,
            final_text=result_text if result_text else None,
        )
    return None


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event))
