"""Cross-implementation conformance test for the runner's stderr diagnosis.

Drives the shared ``spec/conformance/diagnostics.json`` fixture (the single
source of truth for the (pattern, hint) table) through the Python runner and
asserts (a) the runner's embedded ``STDERR_DIAGNOSTICS`` copy matches the
fixture rule-for-rule, and (b) each rule's ``sample`` produces the expected
``hint`` via ``diagnose_stderr_failure``. The Node SDK runs the same fixture
through ``diagnoseStderrFailure`` in ``sdk/node/src/diagnostics.test.js`` —
together they guarantee the two runners stay at parity on post-mortem hints
without one silently drifting.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentproc.runner import STDERR_DIAGNOSTICS, diagnose_stderr_failure

DIAG_PATH = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "diagnostics.json"
data = json.loads(DIAG_PATH.read_text(encoding="utf-8"))


def test_embedded_table_mirrors_fixture():
    assert len(STDERR_DIAGNOSTICS) == len(data["rules"]), "rule count mismatch"
    for i, expected in enumerate(data["rules"]):
        got = STDERR_DIAGNOSTICS[i]
        assert got["id"] == expected["id"], f"rule {i} id"
        assert got["pattern"] == expected["pattern"], f"rule {i} pattern"
        assert got["hint"] == expected["hint"], f"rule {i} hint"
        assert got.get("flags", 0) == _flags(expected.get("flags")), f"rule {i} flags"


def _flags(s):
    if not s:
        return 0
    import re
    out = 0
    if "i" in s:
        out |= re.IGNORECASE
    return out


def test_samples_produce_expected_hints():
    for rule in data["rules"]:
        assert diagnose_stderr_failure(rule["sample"]) == rule["expect"], rule["id"]


def test_empty_and_unrecognized_return_empty():
    assert diagnose_stderr_failure("") == ""
    assert diagnose_stderr_failure("totally fine, nothing to see") == ""
