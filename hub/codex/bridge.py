#!/usr/bin/env python3
"""
AgentProc bridge for the `codex` CLI (OpenAI Codex).

Invokes:
    codex exec --json <message>
    codex exec resume <thread_id> <message>     # when AGENT_SESSION_ID is set

Parses the NDJSON stream:
    thread.started    → captures thread_id (forwarded as AGENT_SESSION:)
    item.completed    → agent_message text → AGENT_PARTIAL:
    turn.completed    → end of turn
    turn.failed       → AGENT_ERROR:

Env vars:
    AGENT_MESSAGE          User message
    AGENT_SESSION_ID       Previous thread_id (empty = new session)
    AGENT_STREAMING        "1" streaming mode, "0" one-shot
    CODEX_MODEL            Optional model override (e.g. "gpt-5")
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def build_args(message: str, session_id: str) -> list[str]:
    model = os.environ.get("CODEX_MODEL", "").strip()
    if session_id:
        args = ["codex", "exec", "resume", session_id, message]
    else:
        args = ["codex", "exec", "--json", message]
        if model:
            args += ["-c", f'model="{model}"']
    return args


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main() -> int:
    message = os.environ["AGENT_MESSAGE"]
    session_id = os.environ.get("AGENT_SESSION_ID", "")
    streaming = os.environ.get("AGENT_STREAMING", "1") != "0"

    args = build_args(message, session_id)
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        emit(f"AGENT_ERROR:{json.dumps('codex CLI not found. Install: npm install -g @openai/codex')}")
        return 1

    thread_id: str | None = None
    final_text: str | None = None
    error_message: str | None = None

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type")
        if etype == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    if streaming:
                        emit(f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}")
                    final_text = text
        elif etype == "turn.completed":
            pass  # success end of turn
        elif etype == "turn.failed":
            error_message = event.get("error") or "codex turn failed"

    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    if error_message:
        emit(f"AGENT_ERROR:{json.dumps(str(error_message), ensure_ascii=False)}")
        return 1
    if proc.returncode != 0 and not thread_id:
        msg = f"codex exited with {proc.returncode}"
        if stderr_output.strip():
            msg += f": {stderr_output.strip()[:500]}"
        emit(f"AGENT_ERROR:{json.dumps(msg, ensure_ascii=False)}")
        return 1

    if thread_id:
        emit(f"AGENT_SESSION:{thread_id}")
    if final_text and not streaming:
        emit(final_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
