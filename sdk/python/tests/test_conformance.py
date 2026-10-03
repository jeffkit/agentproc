"""Cross-implementation conformance tests.

Drives the shared `spec/conformance/cases.json` fixture through the Python
runner's `classify_line` and asserts the result matches the expected
{kind, value}. The Node SDK runs the same fixture through its `classifyLine`
in `sdk/node/src/conformance.test.js` — together they guarantee the two
reference implementations classify stdout identically.

When you change the spec's line-recognition rules, add a case here first;
both SDKs will fail until they agree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentproc.runner import RunOptions, _compose_env, classify_line, normalize_profile

CASES_PATH = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases.json"


def _load_cases():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [pytest.param(c["line"], c["expect"], id=c["line"][:60]) for c in data["cases"]]


@pytest.mark.parametrize("line,expect", _load_cases())
def test_classify_line_conformance(line: str, expect: dict) -> None:
    got = classify_line(line)
    assert got == expect, f"line={line!r}: got {got}, expected {expect}"


def _load_env_cases():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data.get("env_compose", [])


@pytest.mark.parametrize("case", _load_env_cases())
def test_env_compose_conformance(case: dict, monkeypatch) -> None:
    """The shared env-composition policy, driven through _compose_env.

    _compose_env reads os.environ both for ${VAR} expansion and for the
    infra set, so the fake host env is monkeypatched in — everything not
    listed is deleted to prove no other host variable leaks through.
    """
    host_env = case["host_env"]
    for name in list(host_env) + ["_AGENTPROC_FAKE_MARKER"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("_AGENTPROC_FAKE_MARKER", "1")
    for name, value in host_env.items():
        monkeypatch.setenv(name, value)

    profile = normalize_profile(
        {"executor": "conformance", "env": case["profile_env"],
         "env_allowlist": case["env_allowlist"]}
    )
    env = _compose_env(
        profile,
        RunOptions(message="hi", extra_env=dict(case["extra_env"])),
    )

    for name, value in case["expect_contains"].items():
        assert env.get(name) == value, f"{name}: expected {value!r}, got {env.get(name)!r}"
    for name in case["expect_absent"]:
        assert name not in env, f"{name} leaked into the composed child env"
