#!/usr/bin/env python3
"""
AgentProc bridge for the `qwen` CLI (Alibaba Qwen Code).

Qwen Code is a fork of gemini-cli, so its `--output-format stream-json` schema
matches gemini's (init/message/error/result events). This bridge is a thin
variant of the gemini-cli bridge:

    - command name: qwen (instead of gemini)
    - env var prefix: QWEN_* (instead of GEMINI_*)
    - install: npm install -g @qwen-code/qwen-code

Invokes:
    qwen -p <message> --output-format stream-json --yolo \\
        [--resume <session_id>] \\
        [--model <model>]
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "qwen"
INSTALL_HINT = "Install: npm install -g @qwen-code/qwen-code"


def build_args(message: str, session_id: str, env) -> list[str]:
    args = [
        CLI_NAME, "-p", message,
        "--output-format", "stream-json",
        "--yolo",
    ]
    if env.get("QWEN_SANDBOX", "").strip().lower() == "false":
        args += ["--sandbox", "false"]
    model = env.get("QWEN_MODEL", "").strip()
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
        if event.get("delta"):
            return EventResult(partial_text=text)
        return EventResult(final_text=text)
    if etype == "error":
        if event.get("severity") == "error":
            return EventResult(error=event.get("message", "qwen reported an error"))
        return None
    if etype == "result":
        if event.get("status") == "error":
            err = event.get("error") or {}
            return EventResult(error=err.get("message") or "qwen turn failed")
        return None
    return None


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event))
