#!/usr/bin/env python3
"""
AgentProc bridge for the `aider` AI coding assistant (wire 0.3).

Invokes:
    aider --message <message> --yes-always --no-show-release-notes --no-stream
          [--model <model>]

aider modifies files in the working directory and may make git commits.
The stdout output (a human-readable summary of what was done) is forwarded
as the AgentProc reply body (a single {"type":"result"} event). No session id
is emitted — aider uses git history for context continuity, not an explicit
session id.

Per-CLI config (read from the process env the runner injects):
    AIDER_MODEL   Optional model override (e.g. "claude-opus-4-5")
    AIDER_TIMEOUT Process timeout in seconds (default 600)
"""

from __future__ import annotations

import os
import sys

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import run_plain_cli  # noqa: E402

CLI_NAME = "aider"
INSTALL_HINT = "Install: pip install aider-chat"


def build_args(message: str) -> list[str]:
    args = [
        CLI_NAME,
        "--message", message,
        "--yes-always",
        "--no-show-release-notes",
        "--no-stream",
    ]
    model = os.environ.get("AIDER_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


if __name__ == "__main__":
    sys.exit(run_plain_cli(CLI_NAME, INSTALL_HINT, build_args,
                           timeout_env="AIDER_TIMEOUT", default_timeout=600))
