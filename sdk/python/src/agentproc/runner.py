"""AgentProc runner — the canonical bridge-side engine.

This module is the canonical implementation of the AgentProc bridge-side
contract (spec/protocol.md). The CLI (cli.py) is a thin wrapper around it.

Responsibilities:
  - Parse and validate a profile dict
  - Substitute {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}} placeholders
  - Inject AGENT_* env vars + profile env block
  - Spawn the agent command (no shell)
  - Read stdout line by line, classify protocol lines vs reply body
  - Forward AGENT_PARTIAL: in real time (via on_partial callback)
  - Capture the last AGENT_SESSION: line (last-wins rule)
  - Honor AGENT_ERROR: lines
  - Enforce timeout_secs with SIGTERM → kill_grace_secs → SIGKILL
  - Write message to stdin and close (when profile.stdin == 'message')
  - Return RunResult(reply, session_id, error, exit_code, timed_out)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

__all__ = [
    "PROTOCOL_VERSION",
    "DEFAULT_TIMEOUT_SECS",
    "DEFAULT_KILL_GRACE_SECS",
    "DEFAULT_MAX_REPLY_CHARS",
    "EXIT_SUCCESS",
    "EXIT_ERROR",
    "EXIT_TIMEOUT",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "RunResult",
    "RunOptions",
    "run",
    "normalize_profile",
    "classify_line",
    "decode_json_value",
    "substitute",
    "expand_env_ref",
    "expand_path",
]

PROTOCOL_VERSION = "0.1"

DEFAULT_TIMEOUT_SECS = 1800
DEFAULT_KILL_GRACE_SECS = 5
DEFAULT_MAX_REPLY_CHARS = 8000

PREFIX_SESSION = "AGENT_SESSION:"
PREFIX_PARTIAL = "AGENT_PARTIAL:"
PREFIX_ERROR = "AGENT_ERROR:"

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 124
EXIT_SIGINT = 130
EXIT_SIGTERM = 143


@dataclass
class RunResult:
    """Result of running an agent process."""

    reply: str = ""
    session_id: str = ""
    error: str = ""
    exit_code: int = 0
    timed_out: bool = False


@dataclass
class RunOptions:
    """Options passed to run()."""

    message: str
    session_id: str = ""
    session_name: str = "default"
    from_user: str = ""
    streaming: Optional[bool] = None
    extra_env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    timeout_secs: Optional[int] = None
    on_partial: Optional[Callable[[str], None]] = None
    on_session: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_protocol_line: Optional[Callable[[str], None]] = None
    on_stderr: Optional[Callable[[str], None]] = None


# ---------------------------------------------------------------------------
# Profile parsing & validation
# ---------------------------------------------------------------------------

def normalize_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a profile dict."""
    if not isinstance(raw, dict):
        raise ValueError("profile must be a dict")

    src = raw.get("agentproc") if isinstance(raw.get("agentproc"), dict) else raw

    command = src.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("profile.command must be a non-empty string")

    argv = command.strip().split()
    if not argv:
        raise ValueError("profile.command produced empty argv")

    args_value = src.get("args", [])
    if not isinstance(args_value, list):
        raise ValueError("profile.args must be a list")

    cwd_value = src.get("cwd")
    env_value = src.get("env") or {}
    if not isinstance(env_value, dict):
        raise ValueError("profile.env must be a dict")

    return {
        "argv": argv,
        "args": [str(a) for a in args_value],
        "cwd": expand_path(str(cwd_value)) if cwd_value else None,
        "env": env_value,
        "stdin": "message" if src.get("stdin") == "message" else "none",
        "timeout_secs": (
            int(src["timeout_secs"]) if _is_int_like(src.get("timeout_secs"))
            else DEFAULT_TIMEOUT_SECS
        ),
        "kill_grace_secs": (
            int(src["kill_grace_secs"]) if _is_int_like(src.get("kill_grace_secs"))
            else DEFAULT_KILL_GRACE_SECS
        ),
        "max_reply_chars": (
            int(src["max_reply_chars"]) if _is_int_like(src.get("max_reply_chars"))
            else DEFAULT_MAX_REPLY_CHARS
        ),
        "streaming": src.get("streaming", True) is not False,
    }


def _is_int_like(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return True
    return False


def expand_path(p: str) -> str:
    if p == "~":
        return str(Path.home())
    if p.startswith("~/"):
        return str(Path.home() / p[2:])
    return p


def substitute(value: str, ctx: Dict[str, str]) -> str:
    return (
        str(value)
        .replace("{{MESSAGE}}", ctx.get("message", ""))
        .replace("{{SESSION_ID}}", ctx.get("session_id", ""))
        .replace("{{SESSION_NAME}}", ctx.get("session_name", ""))
    )


def expand_env_ref(value: str, env: Dict[str, str]) -> str:
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: env.get(m.group(1), ""),
        str(value),
    )


# ---------------------------------------------------------------------------
# Line classification (per spec)
# ---------------------------------------------------------------------------

def decode_json_value(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    try:
        v = json.loads(text)
        return v if isinstance(v, str) else str(v)
    except json.JSONDecodeError:
        return text


def classify_line(line: str) -> Dict[str, str]:
    if line.startswith(PREFIX_SESSION):
        return {"kind": "session", "value": line[len(PREFIX_SESSION):].strip()}
    if line.startswith(PREFIX_PARTIAL):
        return {"kind": "partial", "value": decode_json_value(line[len(PREFIX_PARTIAL):])}
    if line.startswith(PREFIX_ERROR):
        return {"kind": "error", "value": decode_json_value(line[len(PREFIX_ERROR):])}
    return {"kind": "body", "value": line}


# ---------------------------------------------------------------------------
# run() — the main entry point
# ---------------------------------------------------------------------------

def run(profile_raw: Dict[str, Any], options: RunOptions) -> RunResult:
    """Run an agent process per the AgentProc spec."""
    profile = normalize_profile(profile_raw)

    streaming = (
        options.streaming if options.streaming is not None else profile["streaming"]
    )
    timeout_secs = (
        options.timeout_secs if options.timeout_secs is not None else profile["timeout_secs"]
    )
    cwd = options.cwd or profile["cwd"]

    subst_ctx = {
        "message": options.message,
        "session_id": options.session_id,
        "session_name": options.session_name,
    }

    argv = list(profile["argv"])
    for a in profile["args"]:
        argv.append(substitute(a, subst_ctx))

    env = dict(os.environ)
    for k, v in profile["env"].items():
        env[k] = expand_env_ref(substitute(str(v), subst_ctx), os.environ)
    for k, v in options.extra_env.items():
        env[k] = str(v)

    env["AGENT_MESSAGE"] = options.message
    env["AGENT_SESSION_ID"] = options.session_id
    env["AGENT_SESSION_NAME"] = options.session_name
    env["AGENT_FROM_USER"] = options.from_user
    env["AGENT_STREAMING"] = "1" if streaming else "0"
    env["AGENT_PROTOCOL_VERSION"] = PROTOCOL_VERSION

    stdin_payload = options.message if profile["stdin"] == "message" else None

    result = RunResult()
    body_lines: List[str] = []

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as e:
        if options.on_stderr:
            options.on_stderr(f"[agentproc runner] spawn error: {e}")
        result.exit_code = EXIT_ERROR
        return result
    except PermissionError as e:
        if options.on_stderr:
            options.on_stderr(f"[agentproc runner] spawn error: {e}")
        result.exit_code = EXIT_ERROR
        return result

    if stdin_payload is not None:
        try:
            proc.stdin.write(stdin_payload)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip("\r\n")
            if options.on_stderr:
                options.on_stderr(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout is not None

    def _handle_line(raw_line: str) -> None:
        line = raw_line.rstrip("\r")
        c = classify_line(line)
        if c["kind"] == "session":
            result.session_id = c["value"]
            if options.on_session:
                options.on_session(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
        elif c["kind"] == "partial":
            if streaming and options.on_partial:
                options.on_partial(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
        elif c["kind"] == "error":
            result.error = c["value"]
            if options.on_error:
                options.on_error(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
        else:
            body_lines.append(line)

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            _handle_line(raw_line.rstrip("\n"))

    stdout_thread = threading.Thread(target=_drain_stdout, daemon=True)
    stdout_thread.start()

    exit_code: int
    timed_out = False
    try:
        if timeout_secs and timeout_secs > 0:
            deadline = time.monotonic() + timeout_secs
            while True:
                try:
                    exit_code = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        try:
                            proc.send_signal(signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
                        try:
                            proc.wait(timeout=profile["kill_grace_secs"])
                        except subprocess.TimeoutExpired:
                            try:
                                proc.send_signal(signal.SIGKILL)
                            except (ProcessLookupError, PermissionError):
                                pass
                        try:
                            exit_code = proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            exit_code = EXIT_TIMEOUT
                        break
        else:
            exit_code = proc.wait()
    except KeyboardInterrupt:
        try:
            proc.send_signal(signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            pass
        exit_code = proc.wait()

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=2)

    result.reply = "\n".join(body_lines)
    if len(result.reply) > profile["max_reply_chars"]:
        suffix = "\n\n…(truncated)" if profile["max_reply_chars"] == DEFAULT_MAX_REPLY_CHARS else ""
        result.reply = result.reply[: profile["max_reply_chars"]] + suffix

    if timed_out:
        result.timed_out = True
        result.exit_code = EXIT_TIMEOUT
    elif result.error:
        result.exit_code = EXIT_ERROR if exit_code == 0 else exit_code
    else:
        result.exit_code = exit_code

    return result
