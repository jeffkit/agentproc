"""Issue #6 regression tests — Python run_via_executor streaming behavior.

1. Real-time streaming: on_partial fires as each stdout line arrives
   (Node readline parity), not only after the whole turn buffers to EOF.
2. Timeout salvage: when the executor times out, already-produced stdout
   is returned to the caller, not discarded.
3. Partial/final dedup: streaming partials are NOT concatenated into
   reply on top of final_text (spec: Node/Rust keep them disjoint).
4. Partials-only turns (no final) succeed with reply == ''.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentproc.runner import EXIT_SUCCESS, EXIT_TIMEOUT, RunOptions, run_via_executor


def _make_opts(**kwargs):
    defaults = dict(
        message="hello",
        session_id="",
        extra_env={},
        timeout_secs=10,
        streaming=None,
        on_partial=None,
        on_session=None,
        on_error=None,
        cwd=None,
        profile_dir=None,
    )
    defaults.update(kwargs)
    return RunOptions(**defaults)


SLOW_AGENT = (
    "import sys, time\n"
    "print(json.dumps({'type':'partial','text':'chunk1'}), flush=True)\n"
    "time.sleep(1.5)\n"
    "print(json.dumps({'type':'final','text':'done'}), flush=True)\n"
).replace("json.dumps", "import json; json.dumps")


def _ndjson_executor(script):
    def build_args(message, session_id, env, ctx):
        return [sys.executable, "-c", script]

    def parse_event(event):
        t = event.get("type")
        if t == "partial":
            return {"partial_text": event["text"]}
        if t == "final":
            return {"final_text": event["text"]}
        return None

    return {
        "cli_name": "slow-agent",
        "install_hint": "",
        "build_args": build_args,
        "parse_event": parse_event,
    }


class TestStreamingTimeliness(unittest.TestCase):
    def test_first_partial_arrives_before_turn_ends(self):
        """First on_partial must fire within ~1s of the partial line being
        written, i.e. before the final line 1.5s later — proving line-by-line
        reads, not communicate()-to-EOF buffering."""
        times = []
        ex = _ndjson_executor(
            "import json,sys,time\n"
            "print(json.dumps({'type':'partial','text':'c1'}),flush=True)\n"
            "time.sleep(1.5)\n"
            "print(json.dumps({'type':'final','text':'done'}),flush=True)\n"
        )
        start = time.monotonic()
        run_via_executor(ex, _make_opts(on_partial=lambda t: times.append(time.monotonic() - start)))
        self.assertTrue(times, "on_partial never fired")
        self.assertLess(times[0], 1.2, "first partial delayed until end of turn (buffered, not streamed)")


class TestTimeoutSalvage(unittest.TestCase):
    def test_timeout_returns_partial_output(self):
        """On timeout, output produced before the timeout must be returned
        (reply carries partials / error message carries salvaged stdout),
        not silently dropped."""
        ex = _ndjson_executor(
            "import json,sys,time\n"
            "print(json.dumps({'type':'partial','text':'halfway'}),flush=True)\n"
            "print(json.dumps({'type':'final','text':'halfway-done'}),flush=True)\n"
            "time.sleep(30)\n"
        )
        partials = []
        result = run_via_executor(
            ex,
            _make_opts(timeout_secs=2, on_partial=partials.append),
        )
        self.assertEqual(result.exit_code, EXIT_TIMEOUT)
        self.assertTrue(result.timed_out)
        self.assertEqual(partials, ["halfway"])
        self.assertEqual(result.reply, "halfway-done")


class TestPartialFinalDedup(unittest.TestCase):
    def test_streaming_reply_not_duplicated(self):
        """partials forwarded → reply stays '' (Node runner.js:700-702,
        spawn path, scenarios.json) — partials are never concatenated onto
        the final text."""
        ex = _ndjson_executor(
            "import json\n"
            "print(json.dumps({'type':'partial','text':'abc'}))\n"
            "print(json.dumps({'type':'final','text':'abcdef'}))\n"
        )
        partials = []
        result = run_via_executor(ex, _make_opts(streaming=True, on_partial=partials.append))
        self.assertEqual(partials, ["abc"])
        self.assertEqual(result.reply, "")
        self.assertEqual(result.exit_code, EXIT_SUCCESS)

    def test_partials_only_no_final_succeeds_with_empty_reply(self):
        """When partials were streamed and no final arrives, the turn
        succeeds with reply == '' (body already delivered via partials)."""
        ex = _ndjson_executor(
            "import json\n"
            "print(json.dumps({'type':'partial','text':'streamed-body'}))\n"
        )
        partials = []
        result = run_via_executor(ex, _make_opts(streaming=True, on_partial=partials.append))
        self.assertEqual(partials, ["streamed-body"])
        self.assertEqual(result.reply, "")
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.error, "")


if __name__ == "__main__":
    unittest.main()
