#!/usr/bin/env python3
"""
AgentProc bridge for the `agy` CLI (wire 0.3).

agy's --print mode returns the full reply as plain text — no streaming, no
exposed session id. The bridge forwards the text as the reply body (a single
{"type":"text"} event).

Per-CLI config (read from the process env the runner injects):
    AGY_MODEL   Optional model override
    AGY_DANGEROUSLY_SKIP_PERMISSIONS  "1" (default) adds the flag
    AGY_TIMEOUT Optional timeout in seconds (default 300)
"""

from __future__ import annotations

import os
import sys

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import run_plain_cli  # noqa: E402

CLI_NAME = "agy"
INSTALL_HINT = "See the agy project for installation instructions."


def build_args(message: str) -> list[str]:
    args = [CLI_NAME, "--print", message]
    if os.environ.get("AGY_DANGEROUSLY_SKIP_PERMISSIONS", "1") == "1":
        args.append("--dangerously-skip-permissions")
    model = os.environ.get("AGY_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


if __name__ == "__main__":
    sys.exit(run_plain_cli(CLI_NAME, INSTALL_HINT, build_args,
                           timeout_env="AGY_TIMEOUT", default_timeout=300))
