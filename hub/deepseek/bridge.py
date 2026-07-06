#!/usr/bin/env python3
"""
AgentProc bridge for the DeepSeek TUI CLI.

Uses `deepseek exec -p <message> [--model <model>]` for non-interactive output.
deepseek exec returns plain text. No streaming, no session continuity across
separate invocations.

Env vars:
    AGENT_MESSAGE          User message
    AGENT_STREAMING        Ignored — deepseek exec returns full text only
    DEEPSEEK_MODEL         Optional model override (default: deepseek-v4-pro)
    DEEPSEEK_API_KEY       Optional API key (alternative to `deepseek login`)
    DEEPSEEK_TIMEOUT       Process timeout in seconds (default: 300)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def build_args(message: str) -> list[str]:
    args = ["deepseek", "exec", "-p", message]
    model = os.environ.get("DEEPSEEK_MODEL", "").strip()
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
    env = os.environ.copy()
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key

    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(env.get("DEEPSEEK_TIMEOUT", "300")),
            env=env,
        )
    except FileNotFoundError:
        emit(f"AGENT_ERROR:{json.dumps('deepseek CLI not found. Install from https://deepseek.com/downloads or: brew install deepseek')}")
        return 1
    except subprocess.TimeoutExpired:
        emit(f"AGENT_ERROR:{json.dumps('deepseek timed out')}")
        return 124

    if proc.returncode != 0:
        msg = f"deepseek exited with {proc.returncode}"
        stderr = (proc.stderr or "").strip()
        if stderr:
            msg += f": {stderr[:500]}"
        emit(f"AGENT_ERROR:{json.dumps(msg, ensure_ascii=False)}")
        return 1

    text = (proc.stdout or "").strip()
    if text:
        emit(text)
    else:
        emit(f"AGENT_ERROR:{json.dumps('deepseek returned empty output')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
