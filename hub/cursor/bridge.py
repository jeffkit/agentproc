#!/usr/bin/env python3
"""
AgentProc bridge for the Cursor Agent CLI (`agent`).

The Cursor Agent binary is named `agent` (NOT `cursor`). Install via
`brew install cursor-agent` or from https://cursor.com/downloads.

Invokes:
    agent -p <message> --output-format stream-json \\
        --stream-partial-output \\
        --yolo \\
        [--resume <session_id>] \\
        [--model <model>]

Schema (verified against agent 2026.06.24):
    system/init     → session_id (forwarded as AGENT_SESSION:)
    assistant       → content[].text → AGENT_PARTIAL: (delta chunks)
    result/success  → terminal; session_id (last wins) + final result text
    result/error    → AGENT_ERROR:

Quirk: when --stream-partial-output is on, Cursor emits N delta chunks AND THEN
a final `assistant` event with the FULL assembled text — which would duplicate
what was already streamed. The bridge tracks the accumulated emitted text and
drops any `assistant` event whose text equals (or is a suffix of) the
accumulation. The terminal assembled text is still captured via the `result`
event's `result` field, used as final_text fallback in non-streaming mode.

Env vars:
    AGENT_MESSAGE          User message
    AGENT_SESSION_ID       Previous chat id (empty = new session)
    AGENT_STREAMING        "1" streaming mode, "0" one-shot
    CURSOR_API_KEY         Optional auth (alternative to `agent login`)
    CURSOR_MODEL           Optional model override (e.g. "gpt-5", "sonnet-4-thinking")
    CURSOR_FORCE           "1" (default) adds --yolo; "0" omits it
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import EventResult, main_entry  # noqa: E402


CLI_NAME = "agent"
INSTALL_HINT = "Install: brew install cursor-agent  (then run `agent login`)"


def build_args(message: str, session_id: str, env) -> list[str]:
    args = [
        CLI_NAME, "-p", message,
        "--output-format", "stream-json",
        "--stream-partial-output",
    ]
    if env.get("CURSOR_FORCE", "1") == "1":
        args.append("--yolo")
    model = env.get("CURSOR_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        # `agent --resume <chatId>` — Cursor calls sessions "chats"
        args += ["--resume", session_id]
    return args


def make_parse_event():
    """
    parse_event closure that accumulates streamed text and suppresses the
    duplicate "full text" assistant event Cursor emits at the end.
    """
    accumulated = []

    def parse_event(event: dict) -> Optional[EventResult]:
        nonlocal accumulated
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            return EventResult(session_id=event.get("session_id"))

        if etype == "assistant":
            msg = event.get("message") or {}
            text = "".join(
                b.get("text", "")
                for b in (msg.get("content") or [])
                if b.get("type") == "text"
            )
            if not text:
                return None
            # If text equals what we've already streamed, this is Cursor's
            # duplicate "full assembled" event — drop it.
            if text == "".join(accumulated):
                return None
            accumulated.append(text)
            return EventResult(partial_text=text)

        if etype == "result":
            session_id = event.get("session_id")
            if event.get("is_error") or event.get("subtype") == "error":
                return EventResult(
                    session_id=session_id,
                    error=event.get("result") or "cursor agent reported an error",
                )
            return EventResult(
                session_id=session_id,
                final_text=event.get("result", "") or None,
            )

        return None

    return parse_event


if __name__ == "__main__":
    sys.exit(main_entry(CLI_NAME, INSTALL_HINT, build_args, make_parse_event()))
