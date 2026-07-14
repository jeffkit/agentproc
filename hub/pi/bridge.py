#!/usr/bin/env python3
"""
AgentProc bridge for the `pi` coding agent CLI (wire 0.3).

pi's --print mode returns the full reply as plain text — no streaming, no
exposed session id. The bridge forwards the text as the reply body (a single
{"type":"result"} event).

Per-CLI config (read from the process env the runner injects):
    PI_MODEL         Optional model override (e.g. "anthropic/claude-opus-4-5")
    PI_NO_EXTENSIONS "1" (default) adds --no-extensions to prevent hanging
    PI_TIMEOUT       Process timeout in seconds (default 600)
"""

from __future__ import annotations

import os
import sys

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import run_plain_cli  # noqa: E402

CLI_NAME = "pi"
INSTALL_HINT = "Install: npm install -g @earendil-works/pi-coding-agent"


def build_args(message: str) -> list[str]:
    args = [CLI_NAME, "-p", message, "--approve"]
    if os.environ.get("PI_NO_EXTENSIONS", "1") != "0":
        args.append("--no-extensions")
    model = os.environ.get("PI_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


if __name__ == "__main__":
    sys.exit(run_plain_cli(CLI_NAME, INSTALL_HINT, build_args,
                           timeout_env="PI_TIMEOUT", default_timeout=600))
