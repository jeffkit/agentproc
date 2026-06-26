"""
Shared bridge utilities for AgentProc hub profiles.

A bridge wraps a CLI that emits NDJSON (one JSON object per line) on stdout.
The bridge reads the stream line by line, extracts three things per event,
and emits AgentProc protocol output:

  - partial text   → AGENT_PARTIAL:<json-string>   (streaming only)
  - session id     → AGENT_SESSION:<opaque-id>     (last wins)
  - error message  → AGENT_ERROR:<json-string>     (always honored)

A profile supplies:

  - ``cli_name``         — e.g. "claude", "codex", "gemini"
  - ``cli_install_hint`` — short install instruction shown on ENOENT
  - ``build_args(message, session_id, env) -> list[str]``
  - ``parse_event(event) -> EventResult | None``
      where EventResult has any of: partial_text, session_id, error

This module handles subprocess lifecycle, line reading, JSON decoding,
non-streaming fallback (emit final text at end), exit-code mapping, and
the AGENT_* emission contract. Each bridge stays under ~30 lines.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class EventResult:
    # Incremental text streamed mid-turn. Emitted as AGENT_PARTIAL: when streaming.
    partial_text: Optional[str] = None
    # Terminal text — the final assembled reply. Emitted only in non-streaming
    # mode as the reply body. When streaming, ignored (partials already carried it).
    final_text: Optional[str] = None
    session_id: Optional[str] = None
    error: Optional[str] = None


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_error(text: str) -> None:
    emit(f"AGENT_ERROR:{json.dumps(text, ensure_ascii=False)}")


def emit_partial(text: str) -> None:
    emit(f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}")


def emit_session(session_id: str) -> None:
    emit(f"AGENT_SESSION:{session_id}")


def run_bridge(
    cli_name: str,
    cli_install_hint: str,
    build_args: Callable[[str, str, os._Environ], list[str]],
    parse_event: Callable[[dict], Optional[EventResult]],
) -> int:
    """
    Drive a CLI as an AgentProc agent.

    Reads AGENT_MESSAGE / AGENT_SESSION_ID / AGENT_STREAMING from the
    environment, spawns the CLI built by ``build_args``, and translates
    its NDJSON stream to AgentProc protocol output.
    """
    env = os.environ
    message = env.get("AGENT_MESSAGE", "")
    if not message:
        emit_error("AGENT_MESSAGE env var is required")
        return 1
    session_id = env.get("AGENT_SESSION_ID", "")
    streaming = env.get("AGENT_STREAMING", "1") != "0"

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
    last_partial: Optional[str] = None
    last_final_text: Optional[str] = None
    error_message: Optional[str] = None
    saw_any_partial: bool = False

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
            last_partial = result.partial_text
            if streaming:
                emit_partial(result.partial_text)
                saw_any_partial = True
        if result.final_text:
            # Only used as fallback in non-streaming mode (or when no partials
            # were actually emitted). Streaming mode prefers the live partials.
            if not streaming or not saw_any_partial:
                last_final_text = result.final_text

    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    if error_message:
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
    if last_final_text and not streaming:
        emit(last_final_text)
    return 0


def main_entry(
    cli_name: str,
    cli_install_hint: str,
    build_args: Callable[[str, str, os._Environ], list[str]],
    parse_event: Callable[[dict], Optional[EventResult]],
) -> int:
    """Convenience wrapper for ``if __name__ == "__main__": sys.exit(main_entry(...))``."""
    try:
        return run_bridge(cli_name, cli_install_hint, build_args, parse_event)
    except BrokenPipeError:
        return 1
