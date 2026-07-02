#!/usr/bin/env python3
"""
AgentProc bridge for the `agy` CLI.

agy's --print mode returns the full reply as plain text — no streaming,
no exposed session id. The bridge just forwards the text as the AgentProc
reply body.

Env vars:
    AGENT_MESSAGE          User message
    AGENT_STREAMING        Ignored — agy doesn't stream
    AGY_MODEL              Optional model override
    AGY_DANGEROUSLY_SKIP_PERMISSIONS  "1" (default) adds --dangerously-skip-permissions
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def build_args(message: str) -> list[str]:
    args = ["agy", "--print", message]
    if os.environ.get("AGY_DANGEROUSLY_SKIP_PERMISSIONS", "1") == "1":
        args.append("--dangerously-skip-permissions")
    model = os.environ.get("AGY_MODEL", "").strip()
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
            timeout=int(os.environ.get("AGY_TIMEOUT", "300")),
        )
    except FileNotFoundError:
        emit(f"AGENT_ERROR:{json.dumps('agy CLI not found. See the agy project for installation instructions.')}")
        return 1
    except subprocess.TimeoutExpired:
        emit(f"AGENT_ERROR:{json.dumps('agy timed out')}")
        return 124

    if proc.returncode != 0:
        msg = f"agy exited with {proc.returncode}"
        stderr = (proc.stderr or "").strip()
        if stderr:
            msg += f": {stderr[:500]}"
        emit(f"AGENT_ERROR:{json.dumps(msg, ensure_ascii=False)}")
        return 1

    text = (proc.stdout or "").strip()
    if text:
        emit(text)
    else:
        emit(f"AGENT_ERROR:{json.dumps('agy returned empty output')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
