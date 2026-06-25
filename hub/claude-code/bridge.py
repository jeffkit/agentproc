#!/usr/bin/env python3
"""
AgentProc bridge for the `claude` CLI (Anthropic Claude Code).

Invokes:
    claude -p <message> \\
        --output-format stream-json \\
        --dangerously-skip-permissions \\
        --disallowed-tools AskUserQuestion \\
        [--resume <session_id>] \\
        [--model <model>]

Re-emits the stream as AgentProc protocol output:
    - assistant text blocks → AGENT_PARTIAL:<json-string>
    - result event's session_id → AGENT_SESSION:<id>
    - errors → AGENT_ERROR:<json-string>

Env vars:
    AGENT_MESSAGE          User message
    AGENT_SESSION_ID       Previous session ID (empty = new session)
    AGENT_STREAMING        "1" streaming mode, "0" one-shot
    CLAUDE_MODEL           Optional model override (e.g. "sonnet", "claude-sonnet-4-6")
    CLAUDE_DISALLOW_TOOLS  Optional comma-separated list of disallowed tools
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def build_args(message: str, session_id: str) -> list[str]:
    args = [
        "claude",
        "-p", message,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    disallow = os.environ.get("CLAUDE_DISALLOW_TOOLS", "AskUserQuestion")
    if disallow.strip():
        args += ["--disallowed-tools", disallow]
    model = os.environ.get("CLAUDE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
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
        emit(f"AGENT_ERROR:{json.dumps('claude CLI not found. Install: npm install -g @anthropic-ai/claude-code')}")
        return 1

    found_session_id: str | None = None
    last_partial: str | None = None
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
        if etype == "assistant":
            text = "".join(
                b.get("text", "")
                for b in (event.get("message") or {}).get("content", [])
                if b.get("type") == "text"
            )
            if text and streaming:
                emit(f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}")
                last_partial = text
        elif etype == "result":
            found_session_id = event.get("session_id") or found_session_id
            if event.get("is_error"):
                error_message = event.get("result", "claude reported an error")
            else:
                result_text = event.get("result", "")
                if result_text and result_text != last_partial:
                    if streaming:
                        emit(f"AGENT_PARTIAL:{json.dumps(result_text, ensure_ascii=False)}")
                    else:
                        if found_session_id:
                            emit(f"AGENT_SESSION:{found_session_id}")
                        emit(result_text)
                        return 0

    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    if error_message:
        emit(f"AGENT_ERROR:{json.dumps(error_message, ensure_ascii=False)}")
        return 1
    if proc.returncode != 0 and not found_session_id:
        msg = f"claude exited with {proc.returncode}"
        if stderr_output.strip():
            msg += f": {stderr_output.strip()[:500]}"
        emit(f"AGENT_ERROR:{json.dumps(msg, ensure_ascii=False)}")
        return 1

    if found_session_id:
        emit(f"AGENT_SESSION:{found_session_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
