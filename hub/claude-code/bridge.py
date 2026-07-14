#!/usr/bin/env python3
"""
AgentProc bridge for the `claude` CLI (Anthropic Claude Code, wire 0.4).

Default (unattended) mode:
    claude -p <message> --output-format stream-json \\
        --dangerously-skip-permissions \\
        --disallowed-tools AskUserQuestion \\
        [--resume <session_id>] [--model <model>]

Permission mode (when the turn carries permission: true, i.e. profile
permission: true):
    claude --print --input-format stream-json --output-format stream-json \\
        --verbose --permission-prompt-tool stdio --permission-mode default \\
        --disallowed-tools AskUserQuestion \\
        [--resume <session_id>] [--model <model>]

    Translates Claude Code control_request / control_response ↔
    AgentProc {"type":"permission_request"} / {"type":"permission_response"}
    NDJSON events.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Any, Dict, Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import (  # noqa: E402
    EventResult,
    emit,
    emit_error,
    emit_partial,
    emit_result,
    main_entry,
    read_turn,
)

CLI_NAME = "claude"
INSTALL_HINT = "Install: npm install -g @anthropic-ai/claude-code"


def build_args(message: str, session_id: str, env) -> list[str]:
    """Unattended mode argv (used by stream_utils)."""
    args = [
        CLI_NAME, "-p", message,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    disallow = env.get("CLAUDE_DISALLOW_TOOLS", "AskUserQuestion")
    if disallow.strip():
        args += ["--disallowed-tools", disallow]
    model = env.get("CLAUDE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    return args


def build_permission_args(session_id: str, env) -> list[str]:
    """Permission-mode argv: bidirectional stream-json + stdio permission tool."""
    args = [
        CLI_NAME, "--print",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--verbose",
        "--permission-prompt-tool", "stdio",
        "--permission-mode", "default",
    ]
    disallow = env.get("CLAUDE_DISALLOW_TOOLS", "AskUserQuestion")
    if disallow.strip():
        args += ["--disallowed-tools", disallow]
    model = env.get("CLAUDE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    return args


def parse_event(event: dict) -> Optional[EventResult]:
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        session_id = event.get("session_id")
        return EventResult(session_id=session_id) if isinstance(session_id, str) and session_id else None
    if etype == "assistant":
        text = "".join(
            b.get("text", "")
            for b in (event.get("message") or {}).get("content", [])
            if b.get("type") == "text"
        )
        return EventResult(partial_text=text) if text else None
    if etype == "result":
        session_id = event.get("session_id")
        if event.get("is_error"):
            return EventResult(
                session_id=session_id,
                error=event.get("result", "claude reported an error"),
            )
        result_text = event.get("result", "")
        return EventResult(
            session_id=session_id,
            final_text=result_text if result_text else None,
        )
    return None


def _control_to_permission_request(event: dict) -> Optional[Dict[str, Any]]:
    """Map Claude control_request → AgentProc permission_request payload."""
    if event.get("type") != "control_request":
        return None
    request = event.get("request") or {}
    if request.get("subtype") != "can_use_tool":
        return None
    request_id = event.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    tool_name = request.get("tool_name") or request.get("display_name") or "tool"
    tool_input = request.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "tool_name": str(tool_name),
        "input": tool_input,
    }
    desc = request.get("description")
    if isinstance(desc, str) and desc:
        payload["description"] = desc
    tool_use_id = request.get("tool_use_id")
    if isinstance(tool_use_id, str) and tool_use_id:
        payload["tool_use_id"] = tool_use_id
    return payload


def _permission_response_to_control(resp: dict, original_input: dict) -> dict:
    """Map AgentProc {"type":"permission_response"} → Claude control_response."""
    request_id = str(resp.get("request_id", ""))
    behavior = resp.get("behavior")
    if behavior == "allow":
        updated = resp.get("updated_input")
        if not isinstance(updated, dict):
            updated = original_input
        return {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "allow",
                    "updatedInput": updated,
                },
            },
        }
    message = resp.get("message") or "denied by bridge"
    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
            "response": {
                "behavior": "deny",
                "message": str(message),
            },
        },
    }


def run_permission_mode(turn: dict, env) -> int:
    """Bidirectional Claude session with mid-turn tool authorization."""
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    session_id = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""
    attachments = turn.get("attachments") if isinstance(turn.get("attachments"), list) else []

    if not message and not attachments:
        emit_error("turn.message is required (or include turn.attachments)")
        return 1

    args = build_permission_args(session_id, env)
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        emit_error(f"{CLI_NAME} CLI not found. {INSTALL_HINT}")
        return 1

    assert proc.stdin is not None
    assert proc.stdout is not None

    # Pending Claude tool inputs keyed by request_id (for updatedInput default).
    pending_inputs: Dict[str, dict] = {}
    pending_lock = threading.Lock()
    # Responses from AgentProc runner, keyed by request_id.
    response_events: Dict[str, threading.Event] = {}
    responses: Dict[str, dict] = {}
    stop = threading.Event()

    def write_claude(obj: dict) -> None:
        try:
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    # Initial user turn (stream-json input format).
    write_claude({
        "type": "user",
        "message": {"role": "user", "content": message},
    })

    def drain_bridge_stdin() -> None:
        """Read {"type":"permission_response",...} NDJSON lines from the runner."""
        for raw in sys.stdin:
            if stop.is_set():
                break
            line = raw.rstrip("\r\n")
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "permission_response":
                continue
            rid = payload.get("request_id")
            if not isinstance(rid, str) or not rid:
                continue
            with pending_lock:
                responses[rid] = payload
                ev = response_events.get(rid)
            if ev is not None:
                ev.set()

    stdin_thread = threading.Thread(target=drain_bridge_stdin, daemon=True)
    stdin_thread.start()

    found_session_id: Optional[str] = None
    last_final_text: Optional[str] = None
    last_partial_text: Optional[str] = None
    error_message: Optional[str] = None

    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Mid-turn tool authorization from Claude.
        perm_req = _control_to_permission_request(event)
        if perm_req is not None:
            rid = perm_req["request_id"]
            with pending_lock:
                pending_inputs[rid] = perm_req.get("input") or {}
                ev = threading.Event()
                response_events[rid] = ev
            emit({"type": "permission_request", **perm_req})
            # Block this stdout reader until the runner answers (or process dies).
            while not ev.wait(timeout=0.5):
                if proc.poll() is not None or stop.is_set():
                    break
            with pending_lock:
                resp = responses.pop(rid, None)
                original = pending_inputs.pop(rid, {})
                response_events.pop(rid, None)
            if resp is None:
                resp = {
                    "request_id": rid,
                    "behavior": "deny",
                    "message": "no permission response (process ending)",
                }
            write_claude(_permission_response_to_control(resp, original))
            continue

        # Ignore other control_* noise; translate assistant/result as usual.
        if event.get("type") in ("control_request", "control_response", "sdk_control_request"):
            continue

        result = parse_event(event)
        if result is None:
            continue
        if result.session_id:
            found_session_id = result.session_id
        if result.error:
            error_message = result.error
        if result.partial_text:
            emit_partial(result.partial_text, session_id=found_session_id)
            last_partial_text = result.partial_text
        if result.final_text is not None:
            last_final_text = result.final_text

    stop.set()
    try:
        proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass
    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    if error_message:
        emit_error(error_message, session_id=found_session_id)
        return 1
    if proc.returncode != 0 and not found_session_id:
        msg = f"{CLI_NAME} exited with {proc.returncode}"
        if stderr_output.strip():
            msg += f": {stderr_output.strip()[:500]}"
        emit_error(msg)
        return 1

    reply_text = last_final_text if last_final_text is not None else last_partial_text
    emit_result(reply_text or "", session_id=found_session_id)
    return 0


def main() -> int:
    turn = read_turn()
    if turn.get("permission") is True:
        try:
            return run_permission_mode(turn, os.environ)
        except BrokenPipeError:
            return 1
    return main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event, turn=turn)


if __name__ == "__main__":
    sys.exit(main())
