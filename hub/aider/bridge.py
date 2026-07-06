#!/usr/bin/env python3
"""
AgentProc bridge for the `aider` AI coding assistant.

Invokes:
    aider --message <message> --yes-always --no-show-release-notes --no-stream
          [--model <model>]

aider modifies files in the working directory and may make git commits.
The stdout output (a human-readable summary of what was done) is forwarded
as the AgentProc reply body. No AGENT_SESSION: line is emitted — aider uses
git history for context continuity, not an explicit session id.

Env vars:
    AGENT_MESSAGE          User message
    AGENT_STREAMING        Ignored — aider --no-stream returns full text
    AIDER_MODEL            Optional model override (e.g. "claude-opus-4-5")
    AIDER_TIMEOUT          Process timeout in seconds (default 600)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def build_args(message: str) -> list[str]:
    args = [
        "aider",
        "--message", message,
        "--yes-always",
        "--no-show-release-notes",
        "--no-stream",
    ]
    model = os.environ.get("AIDER_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main() -> int:
    message = os.environ.get("AGENT_MESSAGE", "")
    if not message:
        emit(f"AGENT_ERROR:{json.dumps('AGENT_MESSAGE env var is required')}")
        return 1
    args = build_args(message)
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(os.environ.get("AIDER_TIMEOUT", "600")),
        )
    except FileNotFoundError:
        emit(f"AGENT_ERROR:{json.dumps('aider not found. Install: pip install aider-chat')}")
        return 1
    except subprocess.TimeoutExpired:
        emit(f"AGENT_ERROR:{json.dumps('aider timed out')}")
        return 124

    if proc.returncode != 0:
        msg = f"aider exited with {proc.returncode}"
        stderr = (proc.stderr or "").strip()
        if stderr:
            msg += f": {stderr[:500]}"
        emit(f"AGENT_ERROR:{json.dumps(msg, ensure_ascii=False)}")
        return 1

    text = (proc.stdout or "").strip()
    if text:
        emit(text)
    else:
        emit(f"AGENT_ERROR:{json.dumps('aider returned empty output')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
