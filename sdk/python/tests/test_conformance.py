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

from agentproc.runner import classify_line

CASES_PATH = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases.json"


def _load_cases():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [pytest.param(c["line"], c["expect"], id=c["line"][:60]) for c in data["cases"]]


@pytest.mark.parametrize("line,expect", _load_cases())
def test_classify_line_conformance(line: str, expect: dict) -> None:
    got = classify_line(line)
    assert got == expect, f"line={line!r}: got {got}, expected {expect}"
