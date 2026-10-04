"""Issue #6 — executor-path cross-SDK reply parity.

The executor path is observably identical to the Node `runViaExecutor`
runner (and to Python's own spawn path / spec/conformance/scenarios.json):

- scenarios.json "streaming with partials: non-empty result.text is not
  appended to reply": partials forwarded + result.text → reply == "".
- scenarios.json "empty output → empty reply, success": no output + exit 0
  → success with reply == "".

Node `runner.js:700-702` only assigns reply when NO partial was forwarded
(`if (!partialsForwarded && lastFinalText !== null)`), and never errors on an
empty NDJSON turn; these cases pin the same behavior on the executor path.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentproc.runner import EXIT_SUCCESS, run_via_executor
from test_issue6_streaming import _make_opts, _ndjson_executor  # noqa: E402


class TestExecutorReplyParity(unittest.TestCase):
    def test_partial_then_result_reply_stays_empty(self):
        """Partials forwarded + result.text → reply stays '' (Node gate)."""
        ex = _ndjson_executor(
            "import json\n"
            "print(json.dumps({'type':'partial','text':'abc'}))\n"
            "print(json.dumps({'type':'final','text':'abcdef'}))\n"
        )
        partials = []
        result = run_via_executor(
            ex, _make_opts(streaming=True, on_partial=partials.append)
        )
        self.assertEqual(partials, ["abc"])
        self.assertEqual(result.reply, "")
        self.assertEqual(result.exit_code, EXIT_SUCCESS)

    def test_empty_output_succeeds_with_empty_reply(self):
        """No output + exit 0 → success with reply '' (Node/spec semantics)."""
        ex = _ndjson_executor("pass\n")
        result = run_via_executor(ex, _make_opts(streaming=True))
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.reply, "")
        self.assertEqual(result.error, "")


if __name__ == "__main__":
    unittest.main()
