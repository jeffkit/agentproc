#!/usr/bin/env python3
"""
AgentProc bridge for the DeepSeek TUI CLI (wire 0.3).

Uses `deepseek exec -p <message> [--model <model>]` for non-interactive output.
deepseek exec returns plain text — no streaming, no session continuity. The
bridge forwards the text as the reply body (a single {"type":"text"} event).

Per-CLI config (read from the process env the runner injects):
    DEEPSEEK_MODEL   Optional model override (default: deepseek-v4-pro)
    DEEPSEEK_API_KEY Optional API key (alternative to `deepseek login`)
    DEEPSEEK_TIMEOUT Process timeout in seconds (default: 300)
"""

from __future__ import annotations

import os
import sys

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import run_plain_cli  # noqa: E402

CLI_NAME = "deepseek"
INSTALL_HINT = "Install from https://deepseek.com/downloads or: brew install deepseek"


def build_args(message: str) -> list[str]:
    args = [CLI_NAME, "exec", "-p", message]
    model = os.environ.get("DEEPSEEK_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


if __name__ == "__main__":
    sys.exit(run_plain_cli(CLI_NAME, INSTALL_HINT, build_args,
                           timeout_env="DEEPSEEK_TIMEOUT", default_timeout=300))
