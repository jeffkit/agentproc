"""Malformed-input robustness for the built-in executors and the runner.

Regression tests for the review findings: a malformed-but-plausible event
(``content: null``, ``item`` a string, ``data: 123``, ``part`` a string, …)
used to raise out of ``parse_event`` and — because ``run_via_executor`` had
no recovery — kill the whole runner. The hardened executors return None /
str-only values instead (Go SDK parity), and the runner isolates any
residual parse_event exception per line.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from agentproc.executors import EXECUTORS
from agentproc.runner import RunOptions, run_via_executor

# Shapes that crashed the pre-hardening parse_event implementations.
CRASH_SHAPES: List[Dict[str, Any]] = [
    {"type": "assistant", "message": None},
    {"type": "assistant", "message": "abc"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": 123}]}},
    {"type": "assistant", "message": {"content": "abc"}},
    {"type": "assistant", "message": {"content": [None, 42, "x"]}},
    {"type": "item.completed", "item": "str"},
    {"type": "result", "status": "error", "error": "stringy"},
    {"type": "text", "data": 123},
    {"type": "error", "part": "notadict"},
    {"type": "result", "result": 123},
    {"type": "result", "is_error": True, "result": 123},
    {"type": "result", "session_id": 42},
    {"type": "message", "role": "assistant", "content": 123},
    {"type": "system", "subtype": "init", "session_id": 42},
    {"type": "thread.started", "thread_id": 42},
    {"type": "end", "sessionId": 42},
    {"type": "error", "message": 123},
]


def _handlers(name: str) -> Dict[str, Any]:
    executor = EXECUTORS[name]
    if "make_handlers" in executor:
        return executor["make_handlers"]()
    return executor


@pytest.mark.parametrize("executor_name", sorted(EXECUTORS))
@pytest.mark.parametrize("shape", CRASH_SHAPES)
def test_parse_event_never_raises_on_malformed_shapes(executor_name: str, shape: dict) -> None:
    handlers = _handlers(executor_name)
    parse_event = handlers.get("parse_event")
    if parse_event is None:
        pytest.skip(f"{executor_name} has no parse_event (plain CLI)")
    # Must not raise; may return None or a well-formed dict.
    parse_event(dict(shape))


def test_wrong_typed_result_does_not_leak_into_parse_result() -> None:
    claude = EXECUTORS["claude-code"]
    out = claude["parse_event"]({"type": "result", "result": 123, "session_id": 42})
    # 123 is not a reply body: nothing usable survives (None ≈ the Go SDK's
    # empty ParseResult — both contribute nothing to the turn).
    assert out is None or ("final_text" not in out and "session_id" not in out)

    out = claude["parse_event"]({"type": "result", "is_error": True, "result": 123})
    assert out["error"] == "claude reported an error"
    assert "session_id" not in out

    out = claude["parse_event"]({"type": "result", "is_error": True, "result": 123})
    assert out["error"] == "claude reported an error"
    assert "session_id" not in out

    gemini = EXECUTORS["gemini-cli"]
    out = gemini["parse_event"]({"type": "message", "role": "assistant", "content": 123})
    assert out is None  # a numeric content is not a chunk

    grok = EXECUTORS["grok-build"]
    handlers = grok["make_handlers"]()
    # data:123 must not reach the string accumulator (it used to TypeError).
    assert handlers["parse_event"]({"type": "text", "data": 123}) is None
    assert handlers["parse_event"]({"type": "end"})["final_text"] == ""


def _fake_executor(lines: List[str], raise_on: Optional[str] = None) -> Dict[str, Any]:
    """printf-backed executor whose parse_event raises on a marked line."""

    def build_args(message, session_id, env, ctx=None):
        joined = "\n".join(json.dumps(l) for l in lines)
        return ["printf", "%s\\n", joined]

    def parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        t = event.get("type")
        if raise_on is not None and t == raise_on:
            raise ValueError(f"synthetic parse_event crash on {t}")
        if t == "partial":
            return {"partial_text": event.get("text")}
        if t == "result":
            out: Dict[str, Any] = {"final_text": event.get("text", "")}
            if event.get("session_id"):
                out["session_id"] = event["session_id"]
            return out
        return None

    return {
        "cli_name": "fake-cli",
        "install_hint": "",
        "plain": False,
        "build_args": build_args,
        "parse_event": parse_event,
    }


def test_runner_isolates_parse_event_crash() -> None:
    # The "assistant" line makes the fake parse_event raise; the result line
    # after it must still complete the turn (pre-fix: the exception
    # propagated out of run_via_executor and killed the runner).
    executor = _fake_executor(
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "boom"}]}},
            {"type": "result", "text": "done", "session_id": "s-1"},
        ],
        raise_on="assistant",
    )
    warnings: List[str] = []
    partials: List[str] = []
    result = run_via_executor(
        executor,
        RunOptions(
            message="hello",
            timeout_secs=10,
            on_partial=partials.append,
            on_stderr=warnings.append,
        ),
    )
    assert result.reply == "done"
    assert result.session_id == "s-1"
    assert result.exit_code == 0
    assert any("parse_event" in w and "raised" in w for w in warnings)


def test_send_permission_request_defaults_input_to_object(capsys) -> None:
    # A missing (or non-dict) input must emit {} — "input": null is
    # wire-invalid and rejected by every runner.
    from agentproc import AgentContext

    ctx = AgentContext(
        message="hi",
        session_id="",
        session_name="default",
        protocol_version="0.4",
        attachments=[],
        permission=True,
    )
    ctx.send_permission_request({"request_id": "r1", "tool_name": "Bash"})
    line = json.loads(capsys.readouterr().out.strip())
    assert line["input"] == {}
    assert "null" not in capsys.readouterr().out
