#!/usr/bin/env python3
"""
AgentProc bridge for the `pi` coding agent CLI.

pi's --print mode returns the full reply as plain text — no streaming,
no exposed session id. The bridge forwards the text as the AgentProc
reply body.

Env vars:
    AGENT_MESSAGE          User message
    AGENT_STREAMING        Ignored — pi -p doesn't stream
    PI_MODEL               Optional model override (e.g. "anthropic/claude-opus-4-5")
    PI_NO_EXTENSIONS       "1" (default) adds --no-extensions to prevent hanging
    PI_TIMEOUT             Process timeout in seconds (default 600)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def build_args(message: str) -> list[str]:
    args = ["pi", "-p", message, "--approve"]
    if os.environ.get("PI_NO_EXTENSIONS", "1") != "0":
        args.append("--no-extensions")
    model = os.environ.get("PI_MODEL", "").strip()
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
            timeout=int(os.environ.get("PI_TIMEOUT", "600")),
        )
    except FileNotFoundError:
        emit(f"AGENT_ERROR:{json.dumps('pi CLI not found. Install: npm install -g @earendil-works/pi-coding-agent')}")
        return 1
    except subprocess.TimeoutExpired:
        emit(f"AGENT_ERROR:{json.dumps('pi timed out')}")
        return 124

    if proc.returncode != 0:
        msg = f"pi exited with {proc.returncode}"
        stderr = (proc.stderr or "").strip()
        if stderr:
            msg += f": {stderr[:500]}"
        emit(f"AGENT_ERROR:{json.dumps(msg, ensure_ascii=False)}")
        return 1

    text = (proc.stdout or "").strip()
    if text:
        emit(text)
    else:
        emit(f"AGENT_ERROR:{json.dumps('pi returned empty output')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
