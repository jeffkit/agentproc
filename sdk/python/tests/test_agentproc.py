"""Tests for the agentproc SDK.

Run with: `pytest -q`

Strategy: the SDK calls sys.exit() at the end of create_profile, which is
awkward to test in-process. So we split into two groups:

  1. Pure-function tests (session_file_path, load_history, append_history) —
     call in-process, assert on return values.

  2. create_profile end-to-end tests — drive create_profile in a subprocess
     with AGENT_* env vars set, capture stdout / exit code.
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
        # Point HOME at a tmp path so we don't touch the real home dir.
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
        # Defense in depth: the bridge rejects `/`-bearing ids, but a handler
        # can call session_file_path with any string. Reject path separators
        # and `..` so a malicious id can't escape the sessions directory.
        for bad in ["a/b", "a\\b", "..", "../../tmp/x"]:
            with pytest.raises(ValueError, match="safe filename component"):
                session_file_path(bad)

    def test_accepts_dot_dot_inside_id(self, tmp_path):
        # `a..b` is a valid session id (charset allows `.`) and not a traversal
        # — the filename is `a..b.jsonl`. Pre-fix the `".." in session_id` guard
        # rejected it; the unified check only rejects the exact `.`/`..`.
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


class TestParseAttachments:
    def test_removed_from_sdk(self):
        # AGENT_ATTACHMENTS was removed in SDK 0.5.0; ensure the public surface
        # no longer exposes Attachment / _parse_attachments.
        assert not hasattr(agentproc, "Attachment")
        assert not hasattr(agentproc, "_parse_attachments")
        assert not hasattr(agentproc, "parseAttachments")


def test_protocol_version_is_0_1():
    assert agentproc.PROTOCOL_VERSION == "0.1"


# ---------------------------------------------------------------------------
# 2. create_profile end-to-end tests
# ---------------------------------------------------------------- ...

def _run_agent(env: dict, handler_src: str) -> tuple[str, str, int]:
    """Run a handler under create_profile in a subprocess.

    handler_src is the body of an async function(ctx) -> ...; it is dedented
    and re-indented to exactly 4 spaces to fit inside the handler.
    """
    body = textwrap.indent(textwrap.dedent(handler_src).strip(), "    ")
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(SDK_SRC)!r})\n"
        "import agentproc\n"
        "\n"
        "async def handler(ctx):\n"
        f"{body}\n"
        "\n"
        "agentproc.create_profile(handler)\n"
    )
    proc_env = {**os.environ, **env, "PYTHONPATH": str(SDK_SRC)}
    result = subprocess.run(
        [sys.executable, "-c", program],
        env=proc_env,
        capture_output=True,
        text=True,
    )
    return result.stdout, result.stderr, result.returncode


class TestCreateProfileE2E:

    def test_string_response(self):
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "0"},
            "return 'You said: ' + ctx.message",
        )
        assert code == 0, f"stderr={err}"
        assert "You said: hi\n" in out

    def test_session_id_emitted(self):
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "0"},
            "from agentproc import AgentResult; "
            "return AgentResult(response='ok', session_id='sess-123')",
        )
        assert code == 0, f"stderr={err}"
        assert "AGENT_SESSION:sess-123\n" in out
        assert "ok\n" in out

    def test_send_partial(self):
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "1"},
            """
            await ctx.send_partial('chunk 1')
            await ctx.send_partial('chunk 2')
            from agentproc import AgentResult
            return AgentResult(response='', session_id='s1')
            """,
        )
        assert code == 0, f"stderr={err}"
        assert 'AGENT_PARTIAL:"chunk 1"\n' in out
        assert 'AGENT_PARTIAL:"chunk 2"\n' in out
        assert "AGENT_SESSION:s1\n" in out

    def test_send_error(self):
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "0"},
            "await ctx.send_error('rate limited; retry in 60s')",
        )
        # When handler returns None, SDK exits 0 — the error line is what matters.
        assert code == 0, f"stderr={err}"
        assert 'AGENT_ERROR:"rate limited; retry in 60s"\n' in out

    def test_protocol_error_exception(self):
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "0"},
            "from agentproc import ProtocolError\n"
            "raise ProtocolError('bad input')",
        )
        assert code == 1, f"stderr={err}"
        assert 'AGENT_ERROR:"bad input"\n' in out

    def test_handler_exception(self):
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "0"},
            "raise RuntimeError('boom')",
        )
        assert code == 1
        assert "boom" in err
        assert "AGENT_ERROR" not in out  # generic exceptions don't surface to user

    def test_context_carries_env(self):
        out, err, code = _run_agent(
            {
                "AGENT_MESSAGE": "hello",
                "AGENT_SESSION_ID": "prev-sess",
                "AGENT_SESSION_NAME": "work",
                "AGENT_FROM_USER": "u123",
                "AGENT_STREAMING": "0",
                "AGENT_IMAGE_URL": "https://x/img.png",
                "AGENT_FILE_URL": "https://y/file.pdf",
            },
            """
            import json
            return json.dumps({
                "msg": ctx.message,
                "sid": ctx.session_id,
                "sname": ctx.session_name,
                "from": ctx.from_user,
                "stream": ctx.streaming,
                "img": ctx.image_url,
                "file": ctx.file_url,
            })
            """,
        )
        assert code == 0, f"stderr={err}"
        parsed = json.loads(out.strip())
        assert parsed["msg"] == "hello"
        assert parsed["sid"] == "prev-sess"
        assert parsed["sname"] == "work"
        assert parsed["from"] == "u123"
        assert parsed["stream"] is False
        assert parsed["img"] == "https://x/img.png"
        assert parsed["file"] == "https://y/file.pdf"

    def test_default_protocol_version(self):
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "0"},
            "return 'pv=' + ctx.protocol_version",
        )
        assert code == 0
        assert "pv=0.1" in out

    def test_session_line_handles_colons_in_id(self):
        # The spec says session IDs are opaque strings without whitespace.
        # Make sure we don't accidentally split or json-encode them.
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "0"},
            "from agentproc import AgentResult; "
            "return AgentResult(response='ok', session_id='cli-handle-abc123')",
        )
        assert code == 0
        assert "AGENT_SESSION:cli-handle-abc123\n" in out

    def test_handler_can_return_none(self):
        # Handler signaled everything via send_partial; returns None.
        out, err, code = _run_agent(
            {"AGENT_MESSAGE": "hi", "AGENT_STREAMING": "1"},
            "await ctx.send_partial('only partial')",
        )
        assert code == 0, f"stderr={err}"
        assert 'AGENT_PARTIAL:"only partial"\n' in out
        # No traceback leaked.
        assert "Traceback" not in err


class TestProtocolErrorUsage:
    def test_can_be_raised_and_str(self):
        try:
            raise ProtocolError("something")
        except ProtocolError as e:
            assert str(e) == "something"
