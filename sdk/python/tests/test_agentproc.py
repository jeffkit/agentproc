"""Tests for the agentproc SDK (agent-side entry, wire 0.3).

Run with: `pytest -q`

Strategy: the SDK calls sys.exit() at the end of create_profile, which is
awkward to test in-process. So we split into two groups:

  1. Pure-function tests (session_file_path, load_history, append_history) —
     call in-process, assert on return values.

  2. create_profile end-to-end tests — drive create_profile in a subprocess,
     writing a {"type":"turn",...} object to its stdin, and capture the NDJSON
     events on stdout / exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import agentproc
from agentproc import (
    AgentResult,
    HistoryEntry,
    ProtocolError,
    append_history,
    create_profile,
    load_history,
    session_file_path,
)

SDK_DIR = Path(__file__).resolve().parents[1]
SDK_SRC = SDK_DIR / "src"


# ---------------------------------------------------------------------------
# 1. Pure-function tests
# ---------------------------------------------------------------------------

class TestSessionFilePath:
    def test_under_default_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        p = session_file_path("abc123")
        assert p.name == "abc123.jsonl"
        assert ".agentproc" in p.parts
        assert "sessions" in p.parts

    def test_respects_base_dir(self, tmp_path):
        p = session_file_path("xyz", str(tmp_path))
        assert p == tmp_path / "xyz.jsonl"

    def test_creates_base_dir(self, tmp_path):
        target = tmp_path / "new" / "deep"
        p = session_file_path("s1", str(target))
        assert target.exists()
        assert p.parent == target

    def test_empty_session_id_raises(self):
        with pytest.raises(ValueError, match="session_id must be non-empty"):
            session_file_path("")

    def test_rejects_path_traversal_ids(self):
        for bad in ["a/b", "a\\b", "..", "../../tmp/x"]:
            with pytest.raises(ValueError, match="safe filename component"):
                session_file_path(bad)

    def test_accepts_dot_dot_inside_id(self, tmp_path):
        p = session_file_path("a..b", str(tmp_path))
        assert p == tmp_path / "a..b.jsonl"


class TestLoadAppendHistory:
    def test_load_empty_session_id(self):
        assert load_history("") == []

    def test_load_missing_file(self, tmp_path):
        assert load_history("never", str(tmp_path)) == []

    def test_round_trip(self, tmp_path):
        base = str(tmp_path)
        append_history("s1", [
            HistoryEntry(role="user", content="hello"),
            HistoryEntry(role="assistant", content="hi"),
        ], base)
        loaded = load_history("s1", base)
        assert len(loaded) == 2
        assert loaded[0].role == "user"
        assert loaded[0].content == "hello"
        assert loaded[0].timestamp  # non-empty
        assert loaded[1].role == "assistant"

    def test_append_empty_session_id_is_noop(self, tmp_path):
        append_history("", [HistoryEntry(role="user", content="x")], str(tmp_path))
        assert list(tmp_path.iterdir()) == []

    def test_append_empty_entries_is_noop(self, tmp_path):
        append_history("s1", [], str(tmp_path))
        assert list(tmp_path.iterdir()) == []

    def test_load_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "s1.jsonl"
        f.write_text("\n".join([
            json.dumps({"role": "user", "content": "ok", "timestamp": "t1"}),
            "this is not json",
            json.dumps({"role": "assistant", "content": "still ok", "timestamp": "t2"}),
        ]) + "\n")
        loaded = load_history("s1", str(tmp_path))
        assert len(loaded) == 2
        assert loaded[1].content == "still ok"


class TestAttachmentsSurface:
    def test_no_legacy_attachment_helpers_exported(self):
        # Wire 0.3 carries attachments as `turn.attachments` (read by
        # create_profile from stdin); there is no separate parser helper.
        assert not hasattr(agentproc, "Attachment")
        assert not hasattr(agentproc, "_parse_attachments")
        assert not hasattr(agentproc, "parseAttachments")


def test_protocol_version_is_0_3():
    assert agentproc.PROTOCOL_VERSION == "0.3"


# ---------------------------------------------------------------------------
# 2. create_profile end-to-end tests
# ---------------------------------------------------------------------------

def _turn(**extra) -> dict:
    base = {
        "type": "turn",
        "message": "hi",
        "session_id": "",
        "session_name": "default",
        "from_user": "",
        "protocol_version": "0.3",
    }
    base.update(extra)
    return base


def _run_agent(turn: dict, handler_src: str, is_async: bool = True) -> tuple[str, str, int]:
    """Run a handler under create_profile in a subprocess.

    The turn object is written to the subprocess's stdin as one NDJSON line.
    handler_src is the body of a function(ctx) -> ...; it is dedented and
    re-indented to exactly 4 spaces to fit inside the handler. By default the
    handler is wrapped as ``async def``; pass ``is_async=False`` for a sync
    handler (the body must then not ``await``).
    """
    body = textwrap.indent(textwrap.dedent(handler_src).strip(), "    ")
    decl = "async def handler(ctx):" if is_async else "def handler(ctx):"
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(SDK_SRC)!r})\n"
        "import agentproc\n"
        "\n"
        f"{decl}\n"
        f"{body}\n"
        "\n"
        "agentproc.create_profile(handler)\n"
    )
    proc_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
                "PYTHONPATH": str(SDK_SRC)}
    result = subprocess.run(
        [sys.executable, "-c", program],
        env=proc_env,
        input=json.dumps(turn) + "\n",
        capture_output=True,
        text=True,
    )
    return result.stdout, result.stderr, result.returncode


class TestCreateProfileE2E:

    def test_string_response(self):
        out, err, code = _run_agent(
            _turn(),
            "return 'You said: ' + ctx.message",
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"text","text":"You said: hi"}\n' in out

    def test_session_id_emitted(self):
        out, err, code = _run_agent(
            _turn(),
            "from agentproc import AgentResult; "
            "return AgentResult(response='ok', session_id='sess-123')",
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"session","id":"sess-123"}\n' in out
        assert '{"type":"text","text":"ok"}\n' in out

    def test_send_partial(self):
        out, err, code = _run_agent(
            _turn(),
            """
            await ctx.send_partial('chunk 1')
            await ctx.send_partial('chunk 2')
            from agentproc import AgentResult
            return AgentResult(response='', session_id='s1')
            """,
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"partial","text":"chunk 1"}\n' in out
        assert '{"type":"partial","text":"chunk 2"}\n' in out
        assert '{"type":"session","id":"s1"}\n' in out

    def test_send_partial_with_role(self):
        out, err, code = _run_agent(
            _turn(),
            """
            await ctx.send_partial('thinking...', role='thinking')
            return 'answer'
            """,
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"partial","text":"thinking...","role":"thinking"}\n' in out
        assert '{"type":"text","text":"answer"}\n' in out

    def test_send_error(self):
        out, err, code = _run_agent(
            _turn(),
            "await ctx.send_error('rate limited; retry in 60s')",
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"error","message":"rate limited; retry in 60s"}\n' in out

    def test_protocol_error_exception(self):
        out, err, code = _run_agent(
            _turn(),
            "from agentproc import ProtocolError\n"
            "raise ProtocolError('bad input')",
        )
        assert code == 1, f"stderr={err}"
        assert '{"type":"error","message":"bad input"}\n' in out

    def test_handler_exception(self):
        out, err, code = _run_agent(
            _turn(),
            "raise RuntimeError('boom')",
        )
        assert code == 1
        assert "boom" in err
        # Generic exceptions are NOT mapped to a {"type":"error"} event.
        assert '"type":"error"' not in out

    def test_context_carries_turn_fields(self):
        out, err, code = _run_agent(
            _turn(
                session_id="prev-sess",
                session_name="work",
                from_user="u123",
                attachments=[{"kind": "image", "url": "https://x/img.png"}],
            ),
            """
            import json
            return json.dumps({
                "msg": ctx.message,
                "sid": ctx.session_id,
                "sname": ctx.session_name,
                "from": ctx.from_user,
                "pv": ctx.protocol_version,
                "atts": [a["kind"] + ":" + a["url"] for a in (ctx.attachments or [])],
            })
            """,
        )
        assert code == 0, f"stderr={err}"
        # The returned JSON string is wrapped in a {"type":"text"} event.
        text_line = next(ln for ln in out.splitlines() if '"type":"text"' in ln)
        ctx = json.loads(json.loads(text_line)["text"])
        assert ctx["msg"] == "hi"
        assert ctx["sid"] == "prev-sess"
        assert ctx["sname"] == "work"
        assert ctx["from"] == "u123"
        assert ctx["pv"] == "0.3"
        assert ctx["atts"] == ["image:https://x/img.png"]

    def test_default_protocol_version(self):
        out, err, code = _run_agent(
            _turn(),
            "return 'pv=' + ctx.protocol_version",
        )
        assert code == 0
        assert "pv=0.3" in out

    def test_session_id_with_colons_emitted_verbatim(self):
        # Wire 0.3: session ids are opaque JSON strings; colons are fine and
        # must not be split or re-encoded.
        out, err, code = _run_agent(
            _turn(),
            "from agentproc import AgentResult; "
            "return AgentResult(response='ok', session_id='thread:abc')",
        )
        assert code == 0
        assert '{"type":"session","id":"thread:abc"}\n' in out

    def test_handler_can_return_none(self):
        out, err, code = _run_agent(
            _turn(),
            "await ctx.send_partial('only partial')",
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"partial","text":"only partial"}\n' in out
        assert "Traceback" not in err

    def test_sync_handler_returning_string(self):
        out, err, code = _run_agent(
            _turn(),
            "return 'sync reply'",
            is_async=False,
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"text","text":"sync reply"}\n' in out
        assert "Traceback" not in err

    def test_sync_handler_send_partial_bare(self):
        out, err, code = _run_agent(
            _turn(),
            "ctx.send_partial('bare chunk')\nreturn 'final'",
            is_async=False,
        )
        assert code == 0, f"stderr={err}"
        assert '{"type":"partial","text":"bare chunk"}\n' in out
        assert '{"type":"text","text":"final"}\n' in out
        assert "never awaited" not in err


class TestProtocolErrorUsage:
    def test_can_be_raised_and_str(self):
        try:
            raise ProtocolError("something")
        except ProtocolError as e:
            assert str(e) == "something"
