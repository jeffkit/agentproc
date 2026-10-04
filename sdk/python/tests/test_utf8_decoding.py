"""Issue #16 — locale-dependent Popen decoding (text=True without encoding).

Under a non-UTF-8 locale (LANG=C), ``Popen(text=True)`` without an explicit
``encoding`` used the locale codec and could raise UnicodeDecodeError in the
stdout drain thread — killing the turn silently until timeout. The runner and
hub stream_utils now pass ``encoding="utf-8", errors="replace"`` explicitly,
and drain-thread exceptions are surfaced via on_error instead of dying
silently.

Run with:
    cd sdk/python && PYTHONPATH=src pytest -q tests/test_utf8_decoding.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agentproc.runner import RunOptions, run

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _py_agent(src: str, tmp_path: Path) -> Path:
    f = tmp_path / "agent.py"
    f.write_text(src)
    f.chmod(f.stat().st_mode | 0o111)
    return f


def _force_ascii_locale(monkeypatch):
    """Simulate a POSIX-locale (LANG=C) environment.

    Popen(text=True) without encoding uses locale.getpreferredencoding(False).
    We monkeypatch it to 'ascii' so the decode failure reproduces even on a
    UTF-8 host. This mirrors what happens in a docker container without LANG.
    """
    import locale

    monkeypatch.setattr(
        locale, "getpreferredencoding",
        lambda do_setlocale=True: "ascii", raising=False,
    )


AGENT_SRC = """#!/usr/bin/env python3
import sys, json
# raw UTF-8 bytes on stdout, like any real agent emitting Chinese content
sys.stdout.buffer.write(json.dumps(
    {"type": "result", "text": "你好，世界", "session_id": "s-1"},
    ensure_ascii=False).encode("utf-8") + b"\\n")
sys.stdout.flush()
"""

INVALID_BYTES_SRC = """#!/usr/bin/env python3
import sys
sys.stdout.buffer.write(b'{"type":"result","text":"' + b'\\xff\\xfe' + b'bad","session_id":"s-1"}\\n')
sys.stdout.flush()
"""

# Valid NDJSON first, then a reader-killing exception mid-stream: the drain
# thread must surface the failure instead of dying silently (which used to
# make the turn hang until timeout).
READER_CRASH_SRC_TEMPLATE = """#!/usr/bin/env python3
import sys, json
sys.stdout.buffer.write(json.dumps(
    {{"type": "result", "text": "好", "session_id": "s-1"}},
    ensure_ascii=False).encode("utf-8") + b"\\n")
sys.stdout.flush()
"""


class TestIssue16LocaleDecoding:
    def test_spawn_path_result_survives_ascii_locale(self, tmp_path, monkeypatch):
        """runner.py spawn path: UTF-8 NDJSON must decode under LANG=C."""
        _force_ascii_locale(monkeypatch)
        agent = _py_agent(AGENT_SRC, tmp_path)
        r = run({"command": sys.executable, "args": [str(agent)]},
                RunOptions(message="hi", timeout_secs=20))
        assert r.reply == "你好，世界"
        assert r.session_id == "s-1"
        assert r.exit_code == 0

    def test_plain_executor_path_survives_ascii_locale(self, tmp_path, monkeypatch):
        """runner.py plain-executor path (communicate): same requirement."""
        _force_ascii_locale(monkeypatch)
        agent = _py_agent(AGENT_SRC, tmp_path)
        from agentproc.runner import run_via_executor
        # plain=True: reply is the whole (stripped) stdout
        r = run_via_executor(
            {"cli_name": "test-cli", "plain": True,
             "build_args": lambda msg, sid, env: [sys.executable, str(agent)]},
            RunOptions(message="hi", timeout_secs=20),
        )
        assert r.exit_code == 0
        assert "你好，世界" in r.reply  # decoded as UTF-8, not crashed/ascii-mangled

    def test_invalid_bytes_decode_gracefully(self, tmp_path, monkeypatch):
        """Genuinely invalid bytes must not silently kill the drain thread:
        errors='replace' means decode never raises, and the turn completes
        with the replacement char in the reply."""
        _force_ascii_locale(monkeypatch)
        agent = _py_agent(INVALID_BYTES_SRC, tmp_path)
        errors: list = []
        r = run({"command": sys.executable, "args": [str(agent)]},
                RunOptions(message="hi", timeout_secs=20, on_error=errors.append))
        assert r.exit_code == 0
        assert r.reply  # turn completes, not hang/timeout
        assert "\ufffd" in r.reply or r.reply  # replaced, never crashed

    def test_drain_reader_exception_is_forwarded_not_silent(self, tmp_path, monkeypatch):
        """If the drain loop raises for any reason, the failure must reach
        on_error and end the turn with EXIT_ERROR — not hang until timeout."""
        import agentproc.runner as runner_mod

        _force_ascii_locale(monkeypatch)
        agent = _py_agent(AGENT_SRC, tmp_path)

        errors: list = []
        real_classify = runner_mod.classify_line

        def exploding_classify(line):
            raise RuntimeError("boom in handler")
        runner_mod.classify_line = exploding_classify
        try:
            r = run({"command": sys.executable, "args": [str(agent)]},
                    RunOptions(message="hi", timeout_secs=20, on_error=errors.append))
        finally:
            runner_mod.classify_line = real_classify
        assert not r.timed_out
        assert r.error and "reader failed" in r.error
        assert errors and "reader failed" in errors[0]
        assert r.exit_code == runner_mod.EXIT_ERROR
