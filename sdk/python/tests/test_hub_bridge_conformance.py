"""Hub bridge engine conformance (wire 0.3).

Drives the shared ``hub/_shared/stream_utils.run_bridge`` with a minimal
identity ``parse_event`` against the shared
``spec/conformance/hub_bridge.json`` fixture. The Node SDK runs the same
fixture in ``sdk/node/src/hub_bridge_conformance.test.js``.

Together they guarantee the Python and Node hub bridge engines agree on
multi-line interaction semantics that ``run_bridge`` owns: error-mid-stream
preserving session, session-discovered-at-end ordering, empty-message +
attachment accepted, exit-code mapping, partial-as-reply fallback.

Per-CLI ``parse_event`` variations are NOT covered here — each hub profile
has its own ``test_bridges.py`` cases for that. This file pins the engine.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

HUB_ROOT = Path(__file__).resolve().parents[3] / "hub"
SDK_PYTHON_ROOT = Path(__file__).resolve().parents[1]
if str(HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(HUB_ROOT))

from _shared.stream_utils import EventResult, run_bridge  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "hub_bridge.json"


def _load_scenarios():
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return [pytest.param(s, id=s["name"]) for s in data["scenarios"]]


def _identity_parse_event(event: dict) -> "EventResult | None":
    t = event.get("type")
    if t == "partial":
        return EventResult(partial_text=event.get("text", ""))
    if t == "text":
        return EventResult(final_text=event.get("text", ""))
    if t == "session":
        sid = event.get("id")
        return EventResult(session_id=sid) if isinstance(sid, str) else None
    if t == "error":
        return EventResult(error=event.get("message", ""))
    return None


def _identity_build_args(_message: str, _session_id: str, _env) -> list[str]:
    # Returned argv is never actually executed — the test patches
    # subprocess.Popen with a fake proc. The list just needs a recognizable
    # argv[0] for error messages.
    return ["fake-cli"]


# ---------------------------------------------------------------------------
# Fake subprocess for run_bridge
# ---------------------------------------------------------------------------


class _FakePipe:
    def __init__(self, lines: List[str]):
        self._lines = list(lines)

    def __iter__(self):
        for line in self._lines:
            yield line + "\n"

    def read(self):
        return ""


class _StderrReader:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text


class _FakeProc:
    def __init__(self, ndjson_lines: List[str], returncode: int, stderr: str):
        self.stdout = _FakePipe(ndjson_lines)
        self.stderr = _StderrReader(stderr)
        self.returncode = returncode

    def wait(self):
        return self.returncode


@pytest.mark.parametrize("scenario", _load_scenarios())
def test_hub_bridge_conformance(scenario: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    turn = scenario["turn"]
    cli_stdout = scenario.get("cli_stdout", [])
    cli_exit = scenario.get("cli_exit", 0)
    cli_stderr = scenario.get("cli_stderr", "")

    def _fake_popen(args, *a, **kw):  # noqa: ANN001 — signature mirrors subprocess.Popen
        return _FakeProc(cli_stdout, cli_exit, cli_stderr)

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    exit_code = run_bridge(
        cli_name="fake-cli",
        cli_install_hint="install hint",
        build_args=_identity_build_args,
        parse_event=_identity_parse_event,
        turn=turn,
    )

    captured = capsys.readouterr()
    actual_lines = [line for line in captured.out.split("\n") if line]

    expect = scenario["expect"]
    assert exit_code == expect["exit_code"], (
        f"{scenario['name']}: exit got {exit_code}, expected {expect['exit_code']}"
    )

    if "stdout_lines" in expect:
        assert actual_lines == expect["stdout_lines"], (
            f"{scenario['name']}: stdout got {actual_lines!r}, expected {expect['stdout_lines']!r}"
        )
        return

    if "stdout_lines_any_of" in expect:
        candidates = expect["stdout_lines_any_of"]
        assert any(actual_lines == cand for cand in candidates), (
            f"{scenario['name']}: stdout got {actual_lines!r}, expected any of {candidates!r}"
        )
        return

    if "stdout_lines_contains" in expect:
        joined = "\n".join(actual_lines)
        for needle in expect["stdout_lines_contains"]:
            assert needle in joined, (
                f"{scenario['name']}: expected substring {needle!r} in stdout, got {joined!r}"
            )
