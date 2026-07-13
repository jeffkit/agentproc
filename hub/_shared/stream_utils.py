"""
Shared bridge utilities for AgentProc hub profiles (wire 0.3).

A bridge wraps a CLI that emits NDJSON (one JSON object per line) on stdout.
The bridge reads the {"type":"turn",...} object from its own stdin, spawns the
CLI, and translates the CLI's NDJSON stream into AgentProc wire-0.3 output
(one JSON event per line on stdout):

  - {"type":"partial","text":...}   — live streaming chunk (always emitted;
                                       the runner forwards it only when the
                                       profile's streaming is true)
  - {"type":"session","id":...}     — declare session id (last wins)
  - {"type":"error","message":...}  — error message (always honored, exit 1)
  - {"type":"text","text":...}      — final reply body (emitted once at end)

A profile supplies:

  - ``cli_name``         — e.g. "claude", "codex", "gemini"
  - ``cli_install_hint`` — short install instruction shown on ENOENT
  - ``build_args(message, session_id, env) -> list[str]``
  - ``parse_event(event) -> EventResult | None``
      where EventResult has any of: partial_text, final_text, session_id, error

This module handles turn parsing, subprocess lifecycle, line reading, JSON
decoding, exit-code mapping, and the NDJSON emission contract. Each bridge
stays under ~30 lines.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass
class EventResult:
    # Incremental text streamed mid-turn → emitted as a {"type":"partial"} event.
    partial_text: Optional[str] = None
    # Terminal text — the final assembled reply → emitted as a {"type":"text"}
    # event at end. When a CLI only produces a single completed message (no
    # deltas), set this; if a bridge marks it partial_text instead, the last
    # partial_text is used as the reply fallback.
    final_text: Optional[str] = None
    session_id: Optional[str] = None
    error: Optional[str] = None


def _emit_obj(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit(obj: Dict[str, Any]) -> None:
    """Emit one NDJSON event dict on stdout. (Bridges may pass a pre-built
    event dict; for the common cases use emit_partial / emit_text /
    emit_session / emit_error instead.)"""
    _emit_obj(obj)


def emit_partial(text: str) -> None:
    _emit_obj({"type": "partial", "text": text})


def emit_text(text: str) -> None:
    _emit_obj({"type": "text", "text": text})


def emit_session(session_id: str) -> None:
    _emit_obj({"type": "session", "id": session_id})


def emit_error(text: str) -> None:
    _emit_obj({"type": "error", "message": text})


def _read_turn() -> Dict[str, Any]:
    """Read exactly one line (the turn object) from stdin. Empty dict on failure."""
    try:
        line = sys.stdin.readline()
    except Exception:
        return {}
    if not line:
        return {}
    try:
        v = json.loads(line.rstrip("\r\n"))
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    return {}


# Public alias for bridges that need to inspect the turn before delegating
# (e.g. codebuddy refuses turn.permission before calling run_bridge).
read_turn = _read_turn


def _has_any_attachment(turn: Dict[str, Any]) -> bool:
    atts = turn.get("attachments")
    return isinstance(atts, list) and len(atts) > 0


def run_bridge(
    cli_name: str,
    cli_install_hint: str,
    build_args: Callable[[str, str, "os._Environ"], list[str]],
    parse_event: Callable[[dict], Optional[EventResult]],
    *,
    turn: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Drive a CLI as an AgentProc agent (wire 0.3).

    Reads the turn object from stdin (unless ``turn`` is passed — used by
    bridges that need to inspect the turn, e.g. to refuse ``permission: true``
    before delegating), spawns the CLI built by ``build_args``, and translates
    its NDJSON stream to AgentProc NDJSON output.
    """
    if turn is None:
        turn = _read_turn()
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    session_id = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""
    env = os.environ

    # Per spec: message may be empty when the turn carries attachments
    # (e.g. an image-only message). Only reject when there is truly nothing
    # to do — no text AND no attachment of any kind.
    if not message and not _has_any_attachment(turn):
        emit_error("turn.message is required (or include turn.attachments)")
        return 1

    args = build_args(message, session_id, env)
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        emit_error(f"{cli_name} CLI not found. {cli_install_hint}")
        return 1

    found_session_id: Optional[str] = None
    last_final_text: Optional[str] = None
    last_partial_text: Optional[str] = None
    error_message: Optional[str] = None

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        result = parse_event(event)
        if result is None:
            continue

        if result.session_id:
            found_session_id = result.session_id
        if result.error:
            error_message = result.error
        if result.partial_text:
            # Always emit partials; the runner forwards them only when the
            # profile's streaming is true (and drops them otherwise).
            emit_partial(result.partial_text)
            last_partial_text = result.partial_text
        if result.final_text:
            last_final_text = result.final_text

    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    if error_message:
        # Persist the session for the next turn BEFORE emitting the error —
        # the error terminates this turn but does not invalidate the session.
        if found_session_id:
            emit_session(found_session_id)
        emit_error(error_message)
        return 1
    if proc.returncode != 0 and not found_session_id:
        msg = f"{cli_name} exited with {proc.returncode}"
        if stderr_output.strip():
            msg += f": {stderr_output.strip()[:500]}"
        emit_error(msg)
        return 1

    if found_session_id:
        emit_session(found_session_id)
    reply_text = last_final_text if last_final_text is not None else last_partial_text
    if reply_text:
        emit_text(reply_text)
    return 0


def main_entry(
    cli_name: str,
    cli_install_hint: str,
    build_args: Callable[[str, str, "os._Environ"], list[str]],
    parse_event: Callable[[dict], Optional[EventResult]],
    *,
    turn: Optional[Dict[str, Any]] = None,
) -> int:
    """Convenience wrapper for ``if __name__ == "__main__": sys.exit(main_entry(...))``.

    ``turn`` lets bridges that already read the turn from stdin (e.g. to inspect
    ``permission``) pass it through instead of having ``run_bridge`` re-read stdin.
    """
    try:
        return run_bridge(cli_name, cli_install_hint, build_args, parse_event, turn=turn)
    except BrokenPipeError:
        return 1


def run_plain_cli(
    cli_name: str,
    cli_install_hint: str,
    build_args: Callable[[str], list[str]],
    *,
    timeout_env: str = "CLI_TIMEOUT",
    default_timeout: int = 600,
) -> int:
    """Drive a one-shot CLI that returns the full reply as plain stdout text
    (no streaming, no session id). Reads the turn from stdin, runs the CLI
    with a timeout, and emits the trimmed stdout as a single
    ``{"type":"text"}`` event (or ``{"type":"error"}`` on failure).

    ``build_args(message) -> list[str]`` builds the argv; per-CLI config
    (model, api key, …) is read from ``os.environ`` inside ``build_args`` as
    before — the runner injects the profile ``env`` block into the process
    environment.
    """
    turn = _read_turn()
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    if not message and not _has_any_attachment(turn):
        emit_error(f"{cli_name}: turn.message is required (or include turn.attachments)")
        return 1

    args = build_args(message)
    timeout = int(os.environ.get(timeout_env, str(default_timeout)))
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        emit_error(f"{cli_name} CLI not found. {cli_install_hint}")
        return 1
    except subprocess.TimeoutExpired:
        emit_error(f"{cli_name} timed out")
        return 124

    if proc.returncode != 0:
        msg = f"{cli_name} exited with {proc.returncode}"
        stderr = (proc.stderr or "").strip()
        if stderr:
            msg += f": {stderr[:500]}"
        emit_error(msg)
        return 1

    text = (proc.stdout or "").strip()
    if text:
        emit_text(text)
        return 0
    emit_error(f"{cli_name} returned empty output")
    return 1
