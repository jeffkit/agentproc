"""Regression for issue #14: stderr pipe must be drained concurrently.

Fake CLIs write >64KB to stderr (filling the OS pipe buffer) before/while
emitting NDJSON on stdout. run_bridge must still complete in time and emit
the expected event; before the fix it deadlocked until an external timeout.
"""
import importlib.util
import io
import json
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
UTILS = HERE / "stream_utils.py"

_spec = importlib.util.spec_from_file_location("stream_utils", UTILS)
stream_utils = importlib.util.module_from_spec(_spec)
sys.modules["stream_utils"] = stream_utils
_spec.loader.exec_module(stream_utils)

FAKE_CLI_BODY = (
    "import json,sys\n"
    "for i in range(2000):\n"
    "    sys.stderr.write('x'*64 + '\\n')\n"
    "sys.stderr.flush()\n"
    "print(json.dumps({'type':'result','text':'done'}))\n"
)

FAKE_CLI_FAIL_BODY = (
    "import sys\n"
    "for i in range(2000):\n"
    "    sys.stderr.write('y'*64 + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(1)\n"
)


def _run_bridge_with_cli(body: str) -> tuple[int, list[dict], float]:
    with tempfile.TemporaryDirectory() as td:
        cli = Path(td) / "fakecli"
        cli.write_text("#!/usr/bin/env python3\n" + body)
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

        def build_args(message, session_id, env):
            return [sys.executable, str(cli)]

        def parse_event(event):
            if event.get("type") == "result":
                return stream_utils.EventResult(final_text=event["text"])
            return None

        turn = '{"type":"turn","message":"hi"}'
        old = sys.stdin
        sys.stdin = io.StringIO(turn)
        captured = []
        old_emit = stream_utils._emit_obj
        stream_utils._emit_obj = captured.append
        try:
            start = time.time()
            rc = stream_utils.run_bridge(
                "fakecli", "install hint", build_args, parse_event
            )
            elapsed = time.time() - start
        finally:
            sys.stdin = old
            stream_utils._emit_obj = old_emit
    return rc, list(captured), elapsed


class TestStderrDrain(unittest.TestCase):
    def test_stderr_flood_does_not_stall_stdout(self):
        rc, events, elapsed = _run_bridge_with_cli(FAKE_CLI_BODY)
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 20, "run_bridge deadlocked on full stderr pipe")
        results = [e for e in events if e.get("type") == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "done")

    def test_stderr_flood_nonzero_exit_keeps_stderr_summary(self):
        rc, events, elapsed = _run_bridge_with_cli(FAKE_CLI_FAIL_BODY)
        self.assertEqual(rc, 1)
        self.assertLess(elapsed, 20, "run_bridge deadlocked on full stderr pipe")
        errors = [e for e in events if e.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        msg = errors[0]["message"]
        self.assertIn(": ", msg)
        self.assertIn("y" * 64, msg)
        self.assertLessEqual(len(msg[msg.index(": ") + 2:]), 500)


if __name__ == "__main__":
    unittest.main()
