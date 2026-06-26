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
    profile_dir: Optional[str] = None
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
        .replace("{{PROFILE_DIR}}", ctx.get("profile_dir", ""))
    )


def expand_env_ref(value: str, env: Dict[str, str]) -> str:
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: env.get(m.group(1), ""),
        str(value),
    )


def _diagnose_spawn_error(
    err: BaseException,
    *,
    argv: List[str],
    cwd: Optional[str],
    env: Dict[str, str],
) -> str:
    """Produce a human-friendly hint for a spawn FileNotFoundError / ENOENT.

    Subprocess raises FileNotFoundError when either the command isn't on
    PATH or the cwd doesn't exist — Python folds both into the same
    exception attributed to argv[0], which is misleading. Disambiguate.
    """
    # (a) cwd doesn't exist or isn't a directory
    if cwd:
        p = Path(cwd)
        try:
            if not p.is_dir():
                return f"profile.cwd is not a directory: {cwd}"
        except PermissionError:
            return f"profile.cwd is not accessible (permission denied): {cwd}"
        except OSError:
            return f"profile.cwd does not exist: {cwd}. Pass --cwd <path> to point at a real directory."

    # (b) the command (argv[0]) is not on PATH (bare name, no slash)
    cmd = argv[0] if argv else ""
    is_pathed = "/" in cmd or "\\" in cmd
    if not is_pathed and cmd:
        from shutil import which
        if not which(cmd):
            return (
                f"'{cmd}' not found on PATH. Install it, or if it's installed, "
                "make sure PATH is set correctly when the bridge spawns the agent."
            )

    # (c) argv[0] looks like a path — check whether the file itself exists
    if is_pathed and cmd:
        if not Path(cmd).exists():
            return f"command path does not exist or is not executable: {cmd}"

    # (d) Command exists; suspect an argv file argument (e.g. python3 ./bridge.py)
    for a in argv[1:]:
        if a.startswith("-"):
            continue
        if "/" in a or "\\" in a:
            resolved = a if Path(a).is_absolute() else (
                str(Path(cwd) / a) if cwd else str(Path(a).resolve())
            )
            if not Path(resolved).exists():
                return (
                    f"argument file not found: {a} (resolved to {resolved}). "
                    "The profile likely needs --cwd or the bundled script path is wrong."
                )

    return ""


def _diagnose_stderr_failure(stderr_text: str) -> str:
    """Best-effort pattern check against the agent's accumulated stderr.

    Catches "bridge file not found" failures that the wrapped interpreter
    writes to its own stderr before exiting non-zero. Returns a friendly
    hint, or '' if nothing recognizable.
    """
    if not stderr_text:
        return ""
    lower = stderr_text.lower()

    # python3: "can't open file '/path/x.py': [Errno 2] No such file or directory"
    m = re.search(r"(?:can'?t|cannot) open file '([^']+)': \[Errno 2\] No such file or directory", stderr_text)
    if m:
        return (
            f"agent script not found: {m.group(1)}. Check the profile's command "
            "path (likely a {{PROFILE_DIR}} issue or a typo)."
        )

    # node: "Error: Cannot find module '/path/x.js'"
    m = re.search(r"Cannot find module '([^']+)'", stderr_text)
    if m:
        return (
            f"agent script not found: {m.group(1)}. Check the profile's command "
            "path (likely a {{PROFILE_DIR}} issue or a typo)."
        )

    # bash: "bash: line N: ./x.sh: No such file or directory"
    m = re.search(r"(?:^|\n)[^:]+: line \d+: ([^:]+): No such file or directory", stderr_text)
    if m:
        return f"agent script not found: {m.group(1)}. Check the profile's command path."

    # Generic errno 2 / ENOENT sentinel.
    if re.search(r"errno 2|enoent|no such file or directory", lower):
        return "agent reported a missing file. Check the profile's command and cwd."

    return ""


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
    # Resolve relative cwd against the profile's own directory (if known),
    # so profiles written as `cwd: .` work no matter where the user invokes
    # from. Absolute paths and ~-prefixed paths are already absolute.
    if cwd and not Path(cwd).is_absolute() and options.profile_dir:
        cwd = str(Path(options.profile_dir) / cwd)

    subst_ctx = {
        "message": options.message,
        "session_id": options.session_id,
        "session_name": options.session_name,
        "profile_dir": options.profile_dir or "",
    }

    # Substitute placeholders in argv (command) too, not just args — so
    # `command: python3 {{PROFILE_DIR}}/bridge.py` resolves correctly.
    argv = [substitute(a, subst_ctx) for a in profile["argv"]]
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
    stderr_all: List[str] = []  # capped sliding window, for post-mortem
    STDERR_CAP = 8192

    def _append_stderr(text: str) -> None:
        stderr_all.append(text)
        total = sum(len(s) for s in stderr_all)
        while total > STDERR_CAP and len(stderr_all) > 1:
            total -= len(stderr_all.pop(0))

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
        tip = _diagnose_spawn_error(e, argv=argv, cwd=cwd, env=env)
        if options.on_stderr:
            options.on_stderr(f"[agentproc runner] spawn error: {e}")
            if tip:
                options.on_stderr(f"[agentproc runner] hint: {tip}")
        if options.on_error:
            options.on_error(f"failed to start agent: {tip or str(e)}")
        if not result.error:
            result.error = tip or str(e)
        result.exit_code = EXIT_ERROR
        return result
    except PermissionError as e:
        if options.on_stderr:
            options.on_stderr(f"[agentproc runner] spawn error: {e}")
        if options.on_error:
            options.on_error(f"failed to start agent: {e}")
        if not result.error:
            result.error = str(e)
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
            _append_stderr(line)
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
    # Drain stderr fully before reading stderr_all. The post-mortem
    # diagnosis depends on having the full stderr text. Generous timeout
    # since the process has already exited — at most we wait for the
    # pipe buffer to flush.
    stderr_thread.join(timeout=10)
    if stderr_thread.is_alive():
        # Very rare; indicates a pathological agent that keeps stderr open.
        # The diagnosis may be incomplete, but we don't want to hang forever.
        if options.on_stderr:
            options.on_stderr("[agentproc runner] warning: stderr drain timed out; diagnosis may be incomplete")

    result.reply = "\n".join(body_lines)
    if len(result.reply) > profile["max_reply_chars"]:
        suffix = "\n\n…(truncated)" if profile["max_reply_chars"] == DEFAULT_MAX_REPLY_CHARS else ""
        result.reply = result.reply[: profile["max_reply_chars"]] + suffix

    # If the agent exited non-zero with no AGENT_ERROR, peek at its stderr for
    # common "command/file not found" patterns and surface a friendly hint.
    if not timed_out and not result.error and exit_code != 0:
        stderr_text = "".join(stderr_all)
        hint = _diagnose_stderr_failure(stderr_text)
        if hint:
            result.error = hint
            if options.on_error:
                options.on_error(hint)

    if timed_out:
        result.timed_out = True
        result.exit_code = EXIT_TIMEOUT
    elif result.error:
        result.exit_code = EXIT_ERROR if exit_code == 0 else exit_code
    else:
        result.exit_code = exit_code

    return result
