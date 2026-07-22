"""End-to-end tests for hub bridge scripts (wire 0.4).

Each hub profile ships a Python bridge that wraps an AI CLI emitting NDJSON.
These tests drive each bridge's ``build_args`` + ``parse_event`` through the
shared ``stream_utils.run_bridge`` (or ``run_plain_cli``) against fixture NDJSON
streams — feeding a ``{"type":"turn",...}`` object on stdin and asserting the
NDJSON event output (``{"type":"partial"}`` / ``{"type":"result"}`` /
``{"type":"error"}``) — without needing the real CLI installed.

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
from typing import List

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
# Fake subprocess for stream_utils.run_bridge / run_plain_cli
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


class _StderrReader:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text


class _FakeProc:
    """Fake Popen return value for run_bridge (stdout iter + stderr + wait)."""

    def __init__(self, ndjson_lines: List[str], returncode: int = 0, stderr: str = ""):
        self.stdout = _FakePipe(ndjson_lines)
        self._stderr = stderr
        self.returncode = returncode

    @property
    def stderr(self):
        return _StderrReader(self._stderr)

    def wait(self):
        return self.returncode


class _FakeCompletedProcess:
    """Fake subprocess.run return value for run_plain_cli."""

    def __init__(self, stdout: str, returncode: int, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _events_to_ndjson(events) -> List[str]:
    return [json.dumps(e, ensure_ascii=False) for e in events]


def _make_turn(*, message="hi", session_id="", attachments=None) -> str:
    turn = {
        "type": "turn",
        "message": message,
        "session_id": session_id,
        "session_name": "default",
        "protocol_version": "0.4",
    }
    if attachments is not None:
        turn["attachments"] = attachments
    return json.dumps(turn)


def _resolve_parse_event(mod):
    """cursor exposes ``make_parse_event()`` returning a closure; others expose
    ``parse_event`` directly. Return a callable in either case."""
    if hasattr(mod, "make_parse_event"):
        return mod.make_parse_event()
    return mod.parse_event


def _run_bridge(mod, events, *, returncode=0, session_id="", message="hi", attachments=None):
    """Run the bridge module against fixture events; capture emitted NDJSON.

    Monkeypatches ``subprocess.Popen`` and ``_emit_obj`` inside stream_utils so
    the bridge sees a fake CLI emitting the given NDJSON event dicts, one per
    line, and we capture the AgentProc events it emits on stdout.
    """
    import _shared.stream_utils as su

    fake_proc = _FakeProc(_events_to_ndjson(events), returncode=returncode)
    captured: List[dict] = []

    real_emit_obj = su._emit_obj
    real_popen = su.subprocess.Popen
    saved_stdin = sys.stdin

    su._emit_obj = lambda obj: captured.append(obj)
    su.subprocess.Popen = lambda args, **kw: fake_proc
    sys.stdin = io.StringIO(_make_turn(message=message, session_id=session_id,
                                       attachments=attachments) + "\n")
    try:
        rc = su.run_bridge(
            getattr(mod, "CLI_NAME"),
            getattr(mod, "INSTALL_HINT"),
            mod.build_args,
            _resolve_parse_event(mod),
        )
    finally:
        su._emit_obj = real_emit_obj
        su.subprocess.Popen = real_popen
        sys.stdin = saved_stdin

    return rc, captured


def _run_plain_cli(mod, *, message="hi", fake_result=None):
    """Run a plain-CLI bridge (run_plain_cli) with subprocess.run mocked."""
    import _shared.stream_utils as su

    captured: List[dict] = []
    real_emit_obj = su._emit_obj
    real_run = su.subprocess.run
    saved_stdin = sys.stdin

    su._emit_obj = lambda obj: captured.append(obj)
    su.subprocess.run = lambda args, **kw: _FakeCompletedProcess(**(fake_result or {}))
    sys.stdin = io.StringIO(_make_turn(message=message) + "\n")
    try:
        rc = su.run_plain_cli(
            getattr(mod, "CLI_NAME"),
            getattr(mod, "INSTALL_HINT"),
            mod.build_args,
            timeout_env=getattr(mod, "TIMEOUT_ENV", "CLI_TIMEOUT"),
            default_timeout=getattr(mod, "DEFAULT_TIMEOUT", 600),
        )
    finally:
        su._emit_obj = real_emit_obj
        su.subprocess.run = real_run
        sys.stdin = saved_stdin

    return rc, captured


def _classify_output(events: List[dict]):
    """Split captured NDJSON events into session / partials / error / body.

    Wire 0.4 carries session_id on partial / result / error events (no
    separate {"type":"session"} event).
    """
    out = {"session": "", "partials": [], "error": "", "body": []}
    for e in events:
        t = e.get("type")
        sid = e.get("session_id") or ""
        if sid:
            out["session"] = sid
        if t == "partial":
            out["partials"].append(e.get("text", ""))
        elif t == "error":
            out["error"] = e.get("message", "")
        elif t == "result":
            out["body"].append(e.get("text", ""))
    return out


# ---------------------------------------------------------------------------
# Shared assertions used by every NDJSON bridge
# ---------------------------------------------------------------------------

def _assert_streaming_turn_works(mod, partial_event, result_event):
    """A streaming turn emits partial(s), forwards the session_id, and ends with
    a final {"type":"result"} event (the result text, or the last partial when
    the CLI has no terminal text). No error."""
    rc, out = _run_bridge(mod, [partial_event, result_event])
    assert rc == 0
    parsed = _classify_output(out)
    assert parsed["partials"] == ["hello world"], f"partials wrong: {parsed}"
    assert parsed["session"], "session id should be forwarded"
    assert parsed["error"] == ""
    # 0.4 always emits a final result event (reply body).
    assert parsed["body"], f"no final result event emitted: {parsed}"


def _assert_error_preserves_session(mod, error_result_event):
    """Spec: an error event does not invalidate the session — bridges MUST still
    persist the session id for the next turn (on the error event)."""
    rc, out = _run_bridge(mod, [error_result_event])
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
        rc, out = _run_bridge(self.mod, [fail])
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
        rc, out = _run_bridge(self.mod, [init, msg, result])
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "gem-sess-1"
        assert parsed["partials"] == ["hi"]
        assert parsed["body"] == ["hi"]  # last partial becomes the reply text

    def test_non_streaming_uses_final_text(self):
        init = {"type": "init", "session_id": "gem-sess-1"}
        # delta=false (or absent) → full text, used as final_text
        msg = {"type": "message", "role": "assistant", "content": "full reply"}
        result = {"type": "result", "status": "success"}
        rc, out = _run_bridge(self.mod, [init, msg, result])
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "gem-sess-1"
        assert parsed["partials"] == []
        assert parsed["body"] == ["full reply"]

    def test_result_error_emits_error(self):
        init = {"type": "init", "session_id": "gem-sess-1"}
        result = {"type": "result", "status": "error", "error": {"message": "bad model"}}
        rc, out = _run_bridge(self.mod, [init, result])
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"] == "bad model"
        # Session learned from init MUST survive the error.
        assert parsed["session"] == "gem-sess-1"


class TestGrokBuildBridge:
    """grok emits text/thought/end/error NDJSON (verified against grok 0.2.101).
    thought is dropped; end carries sessionId + accumulated final text."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("grok-build")

    def test_build_args_first_turn(self):
        args = self.mod.build_args("hi", "", {})
        assert args[0] == "grok"
        assert "-p" in args and "hi" in args
        assert "--output-format" in args and "streaming-json" in args
        assert "--always-approve" in args
        assert "--no-auto-update" in args

    def test_build_args_resume_uses_short_r_flag(self):
        args = self.mod.build_args("hi", "grok-sess-1", {})
        assert "-r" in args and "grok-sess-1" in args
        assert "--resume" not in args

    def test_build_args_model_override(self):
        args = self.mod.build_args("hi", "", {"GROK_MODEL": "grok-4.5"})
        assert "-m" in args and "grok-4.5" in args

    def test_streaming_coalesces_tokens_into_blocks(self):
        """Grok emits token-sized text; bridge flushes Claude-like blocks."""
        # >= SOFT_CHARS (40) ending with 。 → one soft flush, then leftover on end.
        tokens = list("今天天气非常好猫咪在窗台上晒太阳打盹的样子非常可爱让人忍不住想要多看几眼真是惬意。")
        assert len("".join(tokens)) >= 40
        events = [{"type": "thought", "data": "planning"}]
        events += [{"type": "text", "data": t} for t in tokens]
        events += [{"type": "text", "data": "尾句"}]
        events.append({
            "type": "end",
            "stopReason": "EndTurn",
            "sessionId": "019f691a-769c-7a33-85e2-5b98100b7716",
        })
        rc, out = _run_bridge(self.mod, events)
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "019f691a-769c-7a33-85e2-5b98100b7716"
        # Not one partial per token — a block flush + leftover on end.
        assert len(parsed["partials"]) >= 1
        assert len(parsed["partials"]) < len(tokens) + 1
        assert "".join(parsed["partials"]) == "".join(tokens) + "尾句"
        assert parsed["body"] == ["".join(tokens) + "尾句"]

    def test_short_reply_flushes_on_end(self):
        events = [
            {"type": "text", "data": "hello"},
            {"type": "text", "data": " world"},
            {
                "type": "end",
                "stopReason": "EndTurn",
                "sessionId": "sid-short",
            },
        ]
        rc, out = _run_bridge(self.mod, events)
        parsed = _classify_output(out)
        assert rc == 0
        # Too short to soft/hard flush mid-stream; drained as one block on end.
        assert parsed["partials"] == ["hello world"]
        assert parsed["body"] == ["hello world"]
        assert parsed["session"] == "sid-short"

    def test_error_preserves_session_when_present(self):
        events = [
            {"type": "error", "message": "Not signed in", "sessionId": "sid-1"},
        ]
        rc, out = _run_bridge(self.mod, events)
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"] == "Not signed in"
        assert parsed["session"] == "sid-1"


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
        rc, out = _run_bridge(self.mod, [init, msg, result])
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "qwen-sess-1"
        assert parsed["partials"] == ["hello"]
        assert parsed["body"] == ["hello"]


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
        )
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["session"] == "cur-sess-1"
        # Two real deltas must reach the user; the duplicate full text must NOT.
        assert parsed["partials"] == ["hel", "lo"], (
            f"duplicate full-text event leaked: {parsed}"
        )
        # The terminal result provides the final reply text.
        assert parsed["body"] == ["hello"]

    def test_result_error_preserves_session(self):
        init = {"type": "system", "subtype": "init", "session_id": "cur-sess-1"}
        result = {"type": "result", "session_id": "cur-sess-1", "is_error": True, "result": "boom"}
        rc, out = _run_bridge(self.mod, [init, result])
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"] == "boom"
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

    def test_permission_true_is_refused(self):
        """codebuddy does not support mid-turn permission; the bridge refuses
        turn.permission before delegating to run_bridge."""
        from _shared.stream_utils import emit_error  # noqa: F401  (ensures import path)
        captured: List[dict] = []
        real_emit = self.mod.emit_error
        self.mod.emit_error = lambda m: captured.append({"type": "error", "message": m})
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(
            json.dumps({"type": "turn", "message": "hi", "permission": True}) + "\n"
        )
        try:
            rc = self.mod.main()
        finally:
            self.mod.emit_error = real_emit
            sys.stdin = saved_stdin
        assert rc == 1
        assert captured and captured[0]["type"] == "error"
        assert "permission" in captured[0]["message"].lower()


class TestEmptyMessageHandling:
    """Covers the spec rule: turn.message may be empty when the turn carries
    attachments (e.g. an image-only message). The hub layer rejects only when
    there is no text AND no attachment of any kind."""

    def _run_with_turn(self, *, message, attachments=None):
        import _shared.stream_utils as su
        fake_proc = _FakeProc(_events_to_ndjson([]), returncode=0)
        captured: List[dict] = []
        real_emit = su._emit_obj
        real_popen = su.subprocess.Popen
        saved_stdin = sys.stdin
        su._emit_obj = lambda o: captured.append(o)
        su.subprocess.Popen = lambda args, **kw: fake_proc
        sys.stdin = io.StringIO(
            _make_turn(message=message, attachments=attachments) + "\n"
        )
        try:
            rc = su.run_bridge(
                "test-cli",
                "install hint",
                lambda msg, sid, env: ["test-cli", msg or "(empty)"],
                lambda event: None,
            )
        finally:
            su._emit_obj = real_emit
            su.subprocess.Popen = real_popen
            sys.stdin = saved_stdin
        return rc, captured

    def test_empty_message_no_attachment_is_rejected(self):
        rc, captured = self._run_with_turn(message="")
        assert rc == 1
        assert any(e.get("type") == "error" for e in captured)

    def test_empty_message_with_image_attachment_is_accepted(self):
        rc, captured = self._run_with_turn(
            message="",
            attachments=[{"type": "image", "url": "https://example.com/x.png"}],
        )
        assert rc == 0
        assert not any(e.get("type") == "error" for e in captured)

    def test_empty_message_with_file_attachment_is_accepted(self):
        rc, captured = self._run_with_turn(
            message="",
            attachments=[{"type": "file", "url": "https://example.com/x.pdf"}],
        )
        assert rc == 0
        assert not any(e.get("type") == "error" for e in captured)

    def test_empty_message_with_empty_attachments_array_is_rejected(self):
        rc, captured = self._run_with_turn(message="", attachments=[])
        assert rc == 1
        assert any(e.get("type") == "error" for e in captured)

    def test_non_empty_message_always_accepted(self):
        # Whitespace-only counts as non-empty content (the agent decides what
        # to do with it); the bridge only rejects truly empty + no attachment.
        rc, captured = self._run_with_turn(message="   ")
        assert rc == 0


# ---------------------------------------------------------------------------
# opencode bridge
# ---------------------------------------------------------------------------

class TestOpencodeBridge:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("opencode")

    def test_build_args_basic(self):
        args = self.mod.build_args("hello", "", {})
        assert args == ["opencode", "run", "hello", "--auto", "--format", "json"]

    def test_build_args_no_session_on_first_turn(self):
        args = self.mod.build_args("hi", "", {})
        assert "--session" not in args

    def test_build_args_with_session(self):
        args = self.mod.build_args("hi", "ses_123", {})
        assert "--session" in args and "ses_123" in args

    def test_build_args_with_model(self):
        args = self.mod.build_args("hi", "", {"OPENCODE_MODEL": "anthropic/claude-opus-4-5"})
        assert "--model" in args and "anthropic/claude-opus-4-5" in args

    def test_streaming_turn_emits_partial_and_session(self):
        step_start = {"type": "step_start", "sessionID": "ses_abc"}
        text_event = {"type": "text", "sessionID": "ses_abc", "part": {"text": "hello world"}}
        step_finish = {"type": "step_finish", "sessionID": "ses_abc", "part": {"reason": "stop"}}
        rc, out = _run_bridge(self.mod, [step_start, text_event, step_finish])
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["partials"] == ["hello world"]
        assert parsed["session"] == "ses_abc"
        assert parsed["body"] == ["hello world"]

    def test_error_preserves_session(self):
        """Spec: session id must survive even when the turn errors."""
        step_start = {"type": "step_start", "sessionID": "ses_abc"}
        error_event = {
            "type": "error",
            "sessionID": "ses_abc",
            "part": {"message": "rate limited"},
        }
        rc, out = _run_bridge(self.mod, [step_start, error_event])
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"] == "rate limited"
        assert parsed["session"] == "ses_abc"

    def test_non_streaming_bridge_still_emits_partials(self):
        # In 0.4 the bridge always emits {"type":"partial"} events; the runner
        # is what suppresses them for non-streaming profiles. So driving the
        # bridge directly still surfaces the partial — and the final result
        # event is the last partial.
        text_event = {"type": "text", "sessionID": "ses_abc", "part": {"text": "full reply"}}
        step_finish = {"type": "step_finish", "sessionID": "ses_abc"}
        rc, out = _run_bridge(self.mod, [text_event, step_finish])
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["partials"] == ["full reply"]
        assert parsed["session"] == "ses_abc"
        assert parsed["body"] == ["full reply"]

    def test_tool_use_events_only_forward_session(self):
        step_start = {"type": "step_start", "sessionID": "ses_abc"}
        tool = {"type": "tool_use", "sessionID": "ses_abc", "part": {"tool": "bash"}}
        text_event = {"type": "text", "sessionID": "ses_abc", "part": {"text": "done"}}
        rc, out = _run_bridge(self.mod, [step_start, tool, text_event])
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["partials"] == ["done"]
        assert parsed["session"] == "ses_abc"


# ---------------------------------------------------------------------------
# kimi-code bridge
# ---------------------------------------------------------------------------

class TestKimiCodeBridge:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("kimi-code")

    def test_build_args_first_turn_generates_uuid_session(self):
        args = self.mod.build_args("hello", "", {})
        assert args[0] == "kimi"
        assert "--print" in args
        assert "-p" in args
        assert "--output-format=stream-json" in args
        # --session must be present with a non-empty value
        assert "--session" in args
        idx = args.index("--session")
        assert len(args[idx + 1]) > 0, "session id must not be empty on first turn"

    def test_build_args_resume_uses_provided_session(self):
        args = self.mod.build_args("hi", "my-sess-99", {})
        idx = args.index("--session")
        assert args[idx + 1] == "my-sess-99"

    def test_build_args_with_model(self):
        args = self.mod.build_args("hi", "", {"KIMI_MODEL": "kimi-latest"})
        assert "--model" in args and "kimi-latest" in args

    def test_streaming_assistant_message(self):
        # kimi emits {"role": "assistant", "content": "..."} on each chunk.
        assistant = {"role": "assistant", "content": "hello world"}
        rc, out = _run_bridge(
            self.mod, [assistant], session_id="kimi-sess-1"
        )
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["partials"] == ["hello world"]
        assert parsed["session"] == "kimi-sess-1"
        assert parsed["body"] == ["hello world"]

    def test_tool_role_is_ignored(self):
        tool_event = {"role": "tool", "content": "some tool result"}
        rc, out = _run_bridge(
            self.mod, [tool_event], session_id="kimi-sess-2"
        )
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["partials"] == []
        assert parsed["error"] == ""


# ---------------------------------------------------------------------------
# Plain-text bridges: build_args tests (no subprocess mocking needed)
# ---------------------------------------------------------------------------

class TestPiBridgeArgs:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("pi")

    def test_build_args_default_includes_no_extensions(self, monkeypatch):
        monkeypatch.delenv("PI_NO_EXTENSIONS", raising=False)  # default is "1"
        monkeypatch.delenv("PI_MODEL", raising=False)
        monkeypatch.setenv("PI_NO_EXTENSIONS", "1")
        args = self.mod.build_args("hello")
        assert args[:4] == ["pi", "-p", "hello", "--approve"]
        assert "--no-extensions" in args

    def test_build_args_no_extensions_disabled(self, monkeypatch):
        monkeypatch.setenv("PI_NO_EXTENSIONS", "0")
        monkeypatch.delenv("PI_MODEL", raising=False)
        args = self.mod.build_args("hello")
        assert "--no-extensions" not in args

    def test_build_args_with_model(self, monkeypatch):
        monkeypatch.setenv("PI_NO_EXTENSIONS", "1")
        monkeypatch.setenv("PI_MODEL", "anthropic/claude-opus-4-5")
        args = self.mod.build_args("hello")
        assert "--model" in args and "anthropic/claude-opus-4-5" in args


class TestAiderBridgeArgs:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("aider")

    def test_build_args_basic(self, monkeypatch):
        monkeypatch.delenv("AIDER_MODEL", raising=False)
        args = self.mod.build_args("fix this bug")
        assert args == [
            "aider",
            "--message", "fix this bug",
            "--yes-always",
            "--no-show-release-notes",
            "--no-stream",
        ]

    def test_build_args_with_model(self, monkeypatch):
        monkeypatch.setenv("AIDER_MODEL", "claude-opus-4-5")
        args = self.mod.build_args("fix this")
        assert "--model" in args and "claude-opus-4-5" in args


class TestDeepseekBridgeArgs:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("deepseek")

    def test_build_args_basic(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
        args = self.mod.build_args("hello")
        assert args == ["deepseek", "exec", "-p", "hello"]

    def test_build_args_with_model(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        args = self.mod.build_args("hello")
        assert "--model" in args and "deepseek-v4-flash" in args


# ---------------------------------------------------------------------------
# agy bridge (plain-text one-shot, no streaming, no session)
# ---------------------------------------------------------------------------

class TestAgyBridge:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_bridge("agy")

    # ── build_args ───────────────────────────────────────────────────────────

    def test_build_args_basic(self, monkeypatch):
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.setenv("AGY_DANGEROUSLY_SKIP_PERMISSIONS", "1")
        args = self.mod.build_args("hello")
        assert args[:3] == ["agy", "--print", "hello"]
        assert "--dangerously-skip-permissions" in args

    def test_build_args_skip_permissions_disabled(self, monkeypatch):
        monkeypatch.setenv("AGY_DANGEROUSLY_SKIP_PERMISSIONS", "0")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        args = self.mod.build_args("hello")
        assert "--dangerously-skip-permissions" not in args

    def test_build_args_with_model(self, monkeypatch):
        monkeypatch.setenv("AGY_DANGEROUSLY_SKIP_PERMISSIONS", "0")
        monkeypatch.setenv("AGY_MODEL", "gpt-4o")
        args = self.mod.build_args("hello")
        assert "--model" in args and "gpt-4o" in args

    # ── full bridge (subprocess.run mocked) ──────────────────────────────────

    def test_success_emits_reply_body(self):
        rc, out = _run_plain_cli(
            self.mod,
            fake_result={"stdout": "agy ok\n", "returncode": 0},
        )
        parsed = _classify_output(out)
        assert rc == 0
        assert parsed["body"] == ["agy ok"]

    def test_empty_message_emits_error(self):
        rc, out = _run_plain_cli(
            self.mod, message="",
            fake_result={"stdout": "", "returncode": 0},
        )
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"]

    def test_nonzero_exit_emits_agent_error(self):
        rc, out = _run_plain_cli(
            self.mod,
            fake_result={"stdout": "", "returncode": 1, "stderr": "auth failed"},
        )
        parsed = _classify_output(out)
        assert rc == 1
        assert "auth failed" in parsed["error"]

    def test_empty_stdout_emits_agent_error(self):
        rc, out = _run_plain_cli(
            self.mod,
            fake_result={"stdout": "", "returncode": 0},
        )
        parsed = _classify_output(out)
        assert rc == 1
        assert parsed["error"]
