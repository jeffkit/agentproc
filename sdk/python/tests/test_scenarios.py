"""Cross-implementation multi-line scenario tests.

Drives the shared `spec/conformance/scenarios.json` fixture through the
Python runner's `run()` and asserts the observable outputs (reply,
session_id, error, exit_code, partials) match the expected values. The Node
SDK runs the same fixture in `sdk/node/src/scenarios.test.js`. Together they
guarantee the two reference implementations agree on multi-line interaction
semantics: last-wins, error-mid-stream, session-with-error, invalid-session
handling, streaming vs one-shot.

When a spec change touches multi-line behaviour, add a scenario here first;
both SDKs will fail until they agree.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import List

import pytest

from agentproc.runner import RunOptions, run

SCENARIOS_PATH = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "scenarios.json"


def _load_scenarios():
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return [pytest.param(s, id=s["name"]) for s in data["scenarios"]]


def _write_bash_agent(tmp_path: Path, lines: List[str]) -> Path:
    """Write a bash script that prints each line to stdout verbatim, then exits 0.

    Uses printf with single-quoted args so AGENT_PARTIAL:"..." style lines
    (which contain JSON double-quotes) pass through without bash interpretation.
    """
    script = tmp_path / "agent.sh"
    body = "#!/usr/bin/env bash\n"
    for line in lines:
        # Single-quote the whole line; embedded single quotes via '\'' .
        quoted = "'" + line.replace("'", "'\\''") + "'"
        body += f"printf '%s\\n' {quoted}\n"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.mark.parametrize("scenario", _load_scenarios())
def test_scenario_conformance(scenario: dict, tmp_path: Path) -> None:
    agent = _write_bash_agent(tmp_path, scenario["lines"])
    expect = scenario["expect"]
    # scenario["profile_overrides"] lets tests set max_reply_chars etc.
    profile = {"command": str(agent), **scenario.get("profile_overrides", {})}
    partials: List[str] = []
    r = run(
        profile,
        RunOptions(
            message="hi",
            streaming=scenario.get("streaming", True),
            on_partial=partials.append,
        ),
    )
    assert r.reply == expect["reply"], (
        f"{scenario['name']}: reply got {r.reply!r}, expected {expect['reply']!r}"
    )
    assert r.session_id == expect["session_id"], (
        f"{scenario['name']}: session_id got {r.session_id!r}, expected {expect['session_id']!r}"
    )
    assert r.error == expect["error"], (
        f"{scenario['name']}: error got {r.error!r}, expected {expect['error']!r}"
    )
    assert r.exit_code == expect["exit_code"], (
        f"{scenario['name']}: exit_code got {r.exit_code!r}, expected {expect['exit_code']!r}"
    )
    assert partials == expect["partials"], (
        f"{scenario['name']}: partials got {partials!r}, expected {expect['partials']!r}"
    )
