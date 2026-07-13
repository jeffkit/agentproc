"""Cross-implementation conformance tests for the SDK entry point
(``create_profile``).

Drives the shared ``spec/conformance/sdk.json`` fixture through the Python SDK
entry by spawning ``_sdk_harness.py`` with a controlled ``AGENT_*`` env per
scenario, then asserts the exact stdout lines and exit code. The Node SDK runs
the same fixture through ``sdk/node/src/sdk.test.js`` + ``sdk_harness.js``.

Together with the existing line-classifier (``test_conformance.py``) and
runner-scenario (``test_scenarios.py``) suites, this guards the user-facing
SDK contract — return types, ``send_partial`` / ``send_error`` semantics,
``ProtocolError`` mapping — against cross-language drift.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CASES_PATH = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "sdk.json"
HARNESS = Path(__file__).resolve().parent / "_sdk_harness.py"
SRC_DIR = Path(__file__).resolve().parents[1] / "src"

DATA = json.loads(CASES_PATH.read_text(encoding="utf-8"))
SCENARIOS = DATA["scenarios"]


def _run_scenario(scenario: dict) -> subprocess.CompletedProcess:
    # Start from a minimal infra env (no inherited AGENT_*). Wire 0.3 carries
    # the turn on stdin, not env — so the scenario's `turn` object is written as
    # a single NDJSON line to the harness's stdin.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", ""),
        "TERM": os.environ.get("TERM", ""),
        "PYTHONPATH": str(SRC_DIR),
    }
    turn_line = json.dumps(scenario["turn"]) + "\n"
    # Scenarios that exercise the optional permission channel follow the turn
    # with one or more {"type":"permission_response",...} frames the harness
    # reads via ctx.read_permission_response().
    extra = "".join(line + "\n" for line in scenario.get("stdin_after_turn", []))
    return subprocess.run(
        [sys.executable, str(HARNESS), scenario["handler"]],
        env=env,
        input=turn_line + extra,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "scenario",
    SCENARIOS,
    ids=[s["name"] for s in SCENARIOS],
)
def test_sdk_scenario(scenario: dict) -> None:
    r = _run_scenario(scenario)
    lines = r.stdout.rstrip("\n").split("\n") if r.stdout else []
    assert r.returncode == scenario["expect"]["exit"], (
        f"{scenario['name']}: exit {r.returncode} != {scenario['expect']['exit']}\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert lines == scenario["expect"]["stdout_lines"], (
        f"{scenario['name']}: stdout lines mismatch\n"
        f"expected: {scenario['expect']['stdout_lines']!r}\n"
        f"actual:   {lines!r}\nraw: {r.stdout!r}"
    )
