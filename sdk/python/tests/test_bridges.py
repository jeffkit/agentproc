"""End-to-end tests for hub bridge scripts.

Each hub profile ships a Python bridge that wraps an AI CLI emitting NDJSON.
These tests exercise each bridge's ``build_args`` + ``parse_event`` against
fixture NDJSON streams, asserting the resulting ``AGENT_PARTIAL:`` /
``AGENT_SESSION:`` / ``AGENT_ERROR:`` output — without needing the real CLI
installed.

This is the layer that catches:
  - the codex resume-without-``--json`` regression
  - cursor's accumulated-dedup closure state
  - error-with-session-id losing the session id (spec: session MUST persist)
  - per-CLI event schema drift
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

import pytest

HUB_ROOT = Path(__file__).resolve().parents[3] / "hub"
SHARED_DIR = HUB_ROOT / "_shared"


def _load_bridge(profile_name: str):
    """Import a hub bridge module by profile name. Returns the module object.

    The bridge scripts do ``from _shared.stream_utils import ...`` which only
    works when ``hub/`` is on sys.path. We add it once for the whole session.
    """
    if str(HUB_ROOT) not in sys.path:
        sys.path.insert(0, str(HUB_ROOT))
    bridge_path = HUB_ROOT / profile_name / "bridge.py"
    spec = importlib.util.spec_from_file_location(f"hub_{profile_name}_bridge", bridge_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fake subprocess for stream_utils.run_bridge
# ---------------------------------------------------------------------------

class _FakePipe:
    """A minimal file-like that yields pre-recorded lines, then EOF."""

    def __init__(self, lines: List[str]):
        # Each "line" should not include the trailing newline; __iter__ adds it
        # to mirror the behaviour of a real text-mode pipe.
        self._lines = list(lines)

    def __iter__(self):
        for line in self._lines:
            yield line + "\n"

    def read(self):
        return ""


class _FakeProc:
    def __init__(self, ndjson_lines: List[str], returncode: int = 0):
        self.stdout = _FakePipe(ndjson_lines)
        self._stderr = ""
        self.returncode = returncode

    @property
    def stderr(self):
        # stream_utils does proc.stderr.read() once after the loop.
        return _StderrReader(self._stderr)

    def wait(self):
        return self.returncode


class _StderrReader:
    """Wraps a string so .read() returns it (mimics pipe.stderr.read())."""

    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text


def _events_to_ndjson(events) -> List[str]:
    return [json.dumps(e, ensure_ascii=False) for e in events]


def _run_bridge(mod, events, *, streaming=True, returncode=0, session_id="", message="hi"):
    """Run the bridge module against fixture events; capture stdout.

    Monkeypatches ``subprocess.Popen`` inside stream_utils so the bridge sees
    a fake CLI that emits the given NDJSON event dicts, one per line.
    """
    import _shared.stream_utils as su

    lines = _events_to_ndjson(events)
    fake_proc = _FakeProc(lines, returncode=returncode)

    captured_stdout: List[str] = []

    def fake_emit(line: str):
        captured_stdout.append(line)

    env = {
        "AGENT_MESSAGE": message,
        "AGENT_SESSION_ID": session_id,
        "AGENT_STREAMING": "1" if streaming else "0",
    }
    monkeypatch_targets = []

    real_popen = su.subprocess.Popen
    real_emit = su.emit
    real_environ = os.environ

    class _Patcher:
        def __enter__(self):
            su.subprocess.Popen = lambda args, **kw: fake_proc
            su.emit = fake_emit
            # Replace os.environ in the stream_utils module's view: run_bridge
            # reads env = os.environ. We swap the attribute on the os module
            # referenced by stream_utils.
            os.environ = env
            return self

        def __exit__(self, *exc):
            su.subprocess.Popen = real_popen
            su.emit = real_emit
            os.environ = real_environ

    patcher = _Patcher()
    patcher.__enter__()
    try:
        # Call run_bridge directly so we can inspect the captured stdout.
        rc = su.run_bridge(
            getattr(mod, "CLI_NAME"),
            getattr(mod, "INSTALL_HINT"),
            mod.build_args,
            # cursor uses make_parse_event() (a closure factory); the others
            # expose parse_event directly. Normalise.
            _resolve_parse_event(mod),
        )
    finally:
        patcher.__exit__()

    return rc, captured_stdout


def _resolve_parse_event(mod):
    """cursor exposes ``make_parse_event()`` returning a closure; others expose
    ``parse_event`` directly. Return a callable in either case."""
    if hasattr(mod, "make_parse_event"):
        return mod.make_parse_event()
    return mod.parse_event


def _classify_output(lines: List[str]):
    """Split captured stdout into session / partials / error / body buckets."""
    out = {"session": "", "partials": [], "error": "", "body": []}
    for raw in lines:
        # The bridge emits "AGENT_X:payload" — no trailing newline because we
        # captured the inner string, not the emit() wrapper. Recreate the form.
        if raw.startswith("AGENT_SESSION:"):
            out["session"] = raw[len("AGENT_SESSION:"):].strip()
        elif raw.startswith("AGENT_PARTIAL:"):
            out["partials"].append(json.loads(raw[len("AGENT_PARTIAL:"):]))
        elif raw.startswith("AGENT_ERROR:"):
            out["error"] = json.loads(raw[len("AGENT_ERROR:"):])
        else:
            out["body"].append(raw)
    return out


# ---------------------------------------------------------------------------
# Shared assertions used by every NDJSON bridge
# ---------------------------------------------------------------------------

def _assert_streaming_turn_works(mod, partial_event, result_event):
    events = [partial_event, result_event]
    rc, out = _run_bridge(mod, events, streaming=True)
    assert rc == 0
    parsed = _classify_output(out)
    assert parsed["partials"] == ["hello world"], f"partials wrong: {parsed}"
    assert parsed["session"], "session id should be forwarded"
    assert parsed["error"] == ""
    # When streaming emitted partials, final_text MUST NOT also be sent as body.
    assert parsed["body"] == [], f"final text leaked into body: {parsed}"


def _assert_error_preserves_session(mod, error_result_event):
    """Spec: AGENT_ERROR does not invalidate the session — bridges MUST still
    persist the session id for the next turn."""
    rc, out = _run_bridge(mod, [error_result_event], streaming=True)
    parsed = _classify_output(out)
    assert rc == 1, "error turn must exit non-zero"
    assert parsed["error"], "error message must reach the user"
    assert parsed["session"], (
        "session id must be forwarded even when the turn errors — "
        "losing it breaks multi-turn resume after a CLI error"
    )


# ---------------------------------------------------------------------------
# Per-bridge tests
# ---------------------------------------------------------------------------

class TestClaudeCodeBridge:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("claude-code")

    def test_build_args_includes_message_and_stream_json(self):
        args = self.mod.build_args("hello", "", {})
        assert args[0] == "claude"
        assert "-p" in args and "hello" in args
        assert "--output-format" in args and "stream-json" in args

    def test_build_args_first_turn_has_no_resume(self):
        args = self.mod.build_args("hi", "", {})
        assert "--resume" not in args

    def test_build_args_resume_added_when_session_id_present(self):
        args = self.mod.build_args("hi", "sess-123", {})
        assert "--resume" in args and "sess-123" in args

    def test_streaming_turn_emits_partial_then_session(self):
        partial = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello world"}]},
        }
        result = {"type": "result", "session_id": "cli-sess-1", "result": "hello world"}
        _assert_streaming_turn_works(self.mod, partial, result)

    def test_error_result_still_forwards_session(self):
        error_result = {
            "type": "result",
            "session_id": "cli-sess-1",
            "is_error": True,
            "result": "rate limited",
        }
        _assert_error_preserves_session(self.mod, error_result)


class TestCodexBridge:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("codex")

    def test_build_args_first_turn_uses_json(self):
        args = self.mod.build_args("hi", "", {})
        assert args == ["codex", "exec", "--json", "hi"]

    def test_build_args_resume_also_has_json(self):
        """Regression: resume path used to omit --json, which made codex emit
        non-NDJSON output that the bridge could not parse."""
        args = self.mod.build_args("hi", "thread-1", {})
        assert args[:4] == ["codex", "exec", "resume", "--json"], (
            f"resume must include --json, got: {args}"
        )
        assert args[4] == "thread-1"
        assert args[5] == "hi"

    def test_build_args_model_added_on_both_paths(self):
        a1 = self.mod.build_args("hi", "", {"CODEX_MODEL": "gpt-5"})
        assert '-c' in a1 and 'model="gpt-5"' in a1
        a2 = self.mod.build_args("hi", "t1", {"CODEX_MODEL": "gpt-5"})
        assert '-c' in a2 and 'model="gpt-5"' in a2

    def test_streaming_turn_emits_partial_then_session(self):
        partial = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "hello world"},
        }
        result = {"type": "thread.started", "thread_id": "t-1"}
        _assert_streaming_turn_works(self.mod, partial, result)

    def test_turn_failed_emits_error(self):
        fail = {"type": "turn.failed", "error": "quota exceeded"}
        rc, out = _run_bridge(self.mod, [fail], streaming=True)
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"] == "quota exceeded"


class TestGeminiCliBridge:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("gemini-cli")

    def test_build_args_first_turn(self):
        args = self.mod.build_args("hi", "", {})
        assert args[0] == "gemini"
        assert "-p" in args and "hi" in args
        assert "--output-format" in args and "stream-json" in args

    def test_init_emits_session_upfront(self):
        """gemini emits session_id in the init event (unlike claude/codex)."""
        init = {"type": "init", "session_id": "gem-sess-1"}
        msg = {"type": "message", "role": "assistant", "content": "hi", "delta": True}
        result = {"type": "result", "status": "success"}
        rc, out = _run_bridge(self.mod, [init, msg, result], streaming=True)
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "gem-sess-1"
        assert parsed["partials"] == ["hi"]

    def test_non_streaming_uses_final_text(self):
        init = {"type": "init", "session_id": "gem-sess-1"}
        # delta=false (or absent) → full text, used as final fallback
        msg = {"type": "message", "role": "assistant", "content": "full reply"}
        result = {"type": "result", "status": "success"}
        rc, out = _run_bridge(self.mod, [init, msg, result], streaming=False)
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "gem-sess-1"
        assert parsed["partials"] == []
        assert parsed["body"] == ["full reply"]

    def test_result_error_emits_error(self):
        init = {"type": "init", "session_id": "gem-sess-1"}
        result = {"type": "result", "status": "error", "error": {"message": "bad model"}}
        rc, out = _run_bridge(self.mod, [init, result], streaming=True)
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"] == "bad model"
        # Session learned from init MUST survive the error.
        assert parsed["session"] == "gem-sess-1"


class TestQwenCodeBridge:
    """qwen-code is a gemini-cli fork — schema should match. These tests pin
    that assumption so a schema drift shows up immediately."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("qwen-code")

    def test_build_args_uses_qwen_binary(self):
        args = self.mod.build_args("hi", "", {})
        assert args[0] == "qwen"

    def test_streaming_turn_matches_gemini_shape(self):
        init = {"type": "init", "session_id": "qwen-sess-1"}
        msg = {"type": "message", "role": "assistant", "content": "hello", "delta": True}
        result = {"type": "result", "status": "success"}
        rc, out = _run_bridge(self.mod, [init, msg, result], streaming=True)
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "qwen-sess-1"
        assert parsed["partials"] == ["hello"]


class TestCursorBridge:
    """cursor emits N delta chunks AND THEN a final assistant event with the
    full assembled text — the bridge's accumulated-dedup closure must drop
    the duplicate."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("cursor")

    def test_build_args_uses_agent_binary(self):
        args = self.mod.build_args("hi", "", {})
        assert args[0] == "agent"  # NOT 'cursor'

    def test_build_args_resume_uses_chat_id(self):
        args = self.mod.build_args("hi", "chat-1", {})
        assert "--resume" in args and "chat-1" in args

    def test_system_init_emits_session(self):
        init = {"type": "system", "subtype": "init", "session_id": "cur-sess-1"}
        assistant1 = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hel"}]},
        }
        assistant2 = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "lo"}]},
        }
        # Cursor's quirk: a final assistant event with the FULL text.
        full_assistant = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
        result = {"type": "result", "session_id": "cur-sess-1", "result": "hello"}
        rc, out = _run_bridge(
            self.mod,
            [init, assistant1, assistant2, full_assistant, result],
            streaming=True,
        )
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "cur-sess-1"
        # Two real deltas must reach the user; the duplicate full text must NOT.
        assert parsed["partials"] == ["hel", "lo"], (
            f"duplicate full-text event leaked: {parsed}"
        )
        assert parsed["body"] == []

    def test_result_error_preserves_session(self):
        init = {"type": "system", "subtype": "init", "session_id": "cur-sess-1"}
        result = {"type": "result", "session_id": "cur-sess-1", "is_error": True, "result": "boom"}
        _assert_error_preserves_session(self.mod, [init, result][-1])
        # Re-run with both events to be sure init's session survives.
        rc, out = _run_bridge(self.mod, [init, result], streaming=True)
        parsed = _classify_output(out)
        assert parsed["session"] == "cur-sess-1"


class TestCodebuddyBridge:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("codebuddy")

    def test_build_args_resume_uses_short_r_flag(self):
        """codebuddy uses -r (not --resume)."""
        args = self.mod.build_args("hi", "cb-1", {})
        assert "-r" in args and "cb-1" in args
        assert "--resume" not in args

    def test_streaming_turn_claude_compatible_shape(self):
        partial = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello world"}]},
        }
        result = {"type": "result", "session_id": "cb-sess-1", "result": "hello world"}
        _assert_streaming_turn_works(self.mod, partial, result)


class TestEmptyMessageHandling:
    """Covers the spec rule: AGENT_MESSAGE may be empty when the turn carries
    attachments. The hub layer rejects only when there is no text AND no
    attachment of any kind."""

    def _run_with_env(self, env, events=None):
        import _shared.stream_utils as su
        fake_proc = _FakeProc(_events_to_ndjson(events or []), returncode=0)
        captured = []
        real_popen = su.subprocess.Popen
        real_emit = su.emit
        real_environ = os.environ
        try:
            su.subprocess.Popen = lambda args, **kw: fake_proc
            su.emit = lambda line: captured.append(line)
            os.environ = env
            rc = su.run_bridge(
                "test-cli",
                "install hint",
                lambda msg, sid, env: ["test-cli", msg or "(empty)"],
                lambda event: None,
            )
        finally:
            su.subprocess.Popen = real_popen
            su.emit = real_emit
            os.environ = real_environ
        return rc, captured

    def test_empty_message_no_attachment_is_rejected(self):
        rc, captured = self._run_with_env({
            "AGENT_MESSAGE": "",
            "AGENT_SESSION_ID": "",
            "AGENT_STREAMING": "1",
        })
        assert rc == 1
        assert any(l.startswith("AGENT_ERROR:") for l in captured)

    def test_empty_message_with_image_url_is_accepted(self):
        rc, captured = self._run_with_env({
            "AGENT_MESSAGE": "",
            "AGENT_SESSION_ID": "",
            "AGENT_STREAMING": "1",
            "AGENT_IMAGE_URL": "https://example.com/x.png",
        })
        assert rc == 0
        assert not any(l.startswith("AGENT_ERROR:") for l in captured)

    def test_empty_message_with_file_url_is_accepted(self):
        rc, captured = self._run_with_env({
            "AGENT_MESSAGE": "",
            "AGENT_SESSION_ID": "",
            "AGENT_STREAMING": "1",
            "AGENT_FILE_URL": "https://example.com/x.pdf",
        })
        assert rc == 0
        assert not any(l.startswith("AGENT_ERROR:") for l in captured)

    def test_empty_message_with_attachments_array_is_rejected(self):
        # AGENT_ATTACHMENTS was removed in 0.5.0; the bridge no longer treats it
        # as an attachment signal, so an empty message with only AGENT_ATTACHMENTS
        # set is now rejected (would-be image-only messages must use
        # AGENT_IMAGE_URL / AGENT_FILE_URL).
        rc, captured = self._run_with_env({
            "AGENT_MESSAGE": "",
            "AGENT_SESSION_ID": "",
            "AGENT_STREAMING": "1",
            "AGENT_ATTACHMENTS": '[{"type":"image","url":"https://example.com/x.png"}]',
        })
        assert rc == 1
        assert any(l.startswith("AGENT_ERROR:") for l in captured)

    def test_empty_message_with_empty_attachments_array_is_rejected(self):
        rc, captured = self._run_with_env({
            "AGENT_MESSAGE": "",
            "AGENT_SESSION_ID": "",
            "AGENT_STREAMING": "1",
            "AGENT_ATTACHMENTS": "[]",
        })
        assert rc == 1

    def test_non_empty_message_always_accepted(self):
        # Whitespace-only counts as non-empty content (the agent decides what
        # to do with it); the bridge only rejects truly empty + no attachment.
        rc, captured = self._run_with_env({
            "AGENT_MESSAGE": "   ",
            "AGENT_SESSION_ID": "",
            "AGENT_STREAMING": "1",
        })
        assert rc == 0
