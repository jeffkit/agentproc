"""AgentProc runner — the canonical bridge-side engine.

This module is the canonical implementation of the AgentProc bridge-side
contract (spec/protocol.md, wire protocol 0.3). The CLI (cli.py) is a thin
wrapper around it.

Wire 0.3 is NDJSON in both directions:
  - stdin:  one {"type":"turn",...} line, then optional
            {"type":"permission_response",...} lines when permission is on.
  - stdout: one JSON object per line, discriminated by `type`:
            partial | text | session | error | permission_request.

Responsibilities:
  - Parse and validate a profile dict
  - Substitute {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}}, {{PROFILE_DIR}} placeholders
  - Build the child env (infra set + profile env block + CLI --env extras)
  - Spawn the agent command (no shell); command is always argv[0], never split
  - Write the turn object to the agent's stdin (and keep stdin open when
    profile.permission is true, for permission_response traffic)
  - Read stdout line by line, parse each line as a JSON event
  - Forward {"type":"partial"} in real time (via on_partial callback)
  - Capture the last {"type":"session"} event (last-wins rule)
  - Honor {"type":"error"} events
  - Optional tool permission: honor permission_request / write permission_response
  - Enforce timeout_secs with SIGTERM → kill_grace_secs → SIGKILL
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
from typing import Any, Callable, Dict, List, Optional, Union

__all__ = [
    "PROTOCOL_VERSION",
    "DEFAULT_TIMEOUT_SECS",
    "DEFAULT_KILL_GRACE_SECS",
    "DEFAULT_MAX_REPLY_CHARS",
    "DEFAULT_TRUNCATION_SUFFIX",
    "EXIT_SUCCESS",
    "EXIT_ERROR",
    "EXIT_TIMEOUT",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "ENV_INFRA_VARS",
    "build_base_env",
    "RunResult",
    "RunOptions",
    "run",
    "normalize_profile",
    "classify_line",
    "parse_json_line",
    "is_valid_session_id",
    "format_permission_response",
    "is_valid_permission_request",
    "substitute",
    "expand_env_ref",
    "expand_path",
    "STDERR_DIAGNOSTICS",
    "diagnose_stderr_failure",
]

PROTOCOL_VERSION = "0.3"

DEFAULT_TIMEOUT_SECS = 1800
DEFAULT_KILL_GRACE_SECS = 5
DEFAULT_MAX_REPLY_CHARS = 8000
DEFAULT_TRUNCATION_SUFFIX = "\n\n…(truncated)"

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 124
EXIT_SIGINT = 130
EXIT_SIGTERM = 143


# ---------------------------------------------------------------------------
# Environment composition policy (wire 0.3)
# ---------------------------------------------------------------------------
#
# The child env is built from exactly three layers (later overrides earlier):
#   (1) this minimal INFRA set (copied from ``os.environ`` when present),
#   (2) the profile ``env`` block (${VAR} expanded; optionally allowlist-filtered),
#   (3) ``extra_env`` from the CLI ``--env`` flag.
# The per-turn request does NOT travel in env (it travels on stdin as the
# turn object), so there are no ``AGENT_*`` injections. There is no
# ``env_inherit: all`` escape hatch in 0.3 — the infra set is always the base.
ENV_INFRA_VARS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TERM", "TMPDIR", "TZ", "PWD",
    # Windows infra
    "SystemRoot", "TEMP", "TMP", "USERPROFILE", "USERNAME", "PATHEXT",
    "COMSPEC", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS",
)


def build_base_env() -> Dict[str, str]:
    """Build the child process base env — the infra set, always."""
    base: Dict[str, str] = {}
    for name in ENV_INFRA_VARS:
        if name in os.environ:
            base[name] = os.environ[name]
    return base


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
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    cwd: Optional[str] = None
    profile_dir: Optional[str] = None
    timeout_secs: Optional[int] = None
    on_partial: Optional[Callable[[str], None]] = None
    on_session: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_protocol_line: Optional[Callable[[str], None]] = None
    on_stderr: Optional[Callable[[str], None]] = None
    on_permission: Optional[Callable[[Dict[str, Any]], Any]] = None


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

    args_value = src.get("args") or []
    if not isinstance(args_value, list):
        raise ValueError("profile.args must be a list")

    # Wire 0.3: `command` is always argv[0], a single token, NEVER split —
    # even if it contains whitespace. `args` is argv[1..], a YAML list of
    # tokens, defaulting to []. The 0.2 "args absent ⇒ split command on
    # whitespace" shorthand is removed. Paths with whitespace are carried
    # whole by YAML quoting and passed to execve as one token.
    argv = [command.strip()]

    cwd_value = src.get("cwd")
    env_value = src.get("env") or {}
    if not isinstance(env_value, dict):
        raise ValueError("profile.env must be a dict")

    # env_allowlist (optional): when present, ${VAR} references in the env
    # block whose name is NOT in the list expand to empty + a stderr warning.
    # Absent ⇒ expand against the full bridge environment.
    allowlist_raw = src.get("env_allowlist")
    if allowlist_raw is None:
        env_allowlist: Optional[set] = None
    elif isinstance(allowlist_raw, list):
        env_allowlist = {str(x) for x in allowlist_raw}
    else:
        raise ValueError("profile.env_allowlist must be a list")

    return {
        "command": command.strip(),
        "argv": argv,
        "args": [str(a) for a in args_value],
        "cwd": expand_path(str(cwd_value)) if cwd_value else None,
        "env": env_value,
        "env_allowlist": env_allowlist,
        # Opt-in tool-authorization channel (wire 0.3). Default False.
        "permission": src.get("permission") is True,
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
        # Spec profile YAML: optional truncation notice appended when the reply
        # is capped. Defaults to "\n\n…(truncated)". An empty string disables
        # the notice entirely (the cap still applies, just no visible marker).
        "truncation_suffix": (
            src["truncation_suffix"] if isinstance(src.get("truncation_suffix"), str)
            else DEFAULT_TRUNCATION_SUFFIX
        ),
        # Bridge-side hint: when False, the runner ignores {"type":"partial"}
        # events and assembles the reply from {"type":"text"} events only.
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


def expand_env_ref(
    value: str,
    env: Dict[str, str],
    allowlist: Optional[set] = None,
    on_blocked: Optional[Callable[[str], None]] = None,
) -> str:
    """Expand ${VAR} references against ``env``.

    When ``allowlist`` is a set of variable names, references to names NOT in
    the set expand to empty string and ``on_blocked`` (if given) is called
    with each blocked name. When ``allowlist`` is None, all references expand
    normally (the default, pre-allowlist behaviour).
    """
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        if allowlist is not None and name not in allowlist:
            if on_blocked:
                on_blocked(name)
            return ""
        return env.get(name, "")
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, str(value))


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


# Shared (pattern, hint) table for post-mortem stderr diagnosis. This is the
# runtime-embedded copy of spec/conformance/diagnostics.json — the single
# source of truth. The conformance test asserts the two stay in sync. Rules
# are evaluated in order; first match wins. A ``{n}`` token in the hint is
# replaced by capture group n; ``{{PROFILE_DIR}}`` is a literal, not a format
# token (only numeric ``{n}`` tokens are substituted).
STDERR_DIAGNOSTICS: List[Dict[str, str]] = [
    {
        "id": "python-open-file",
        "pattern": r"(?:can'?t|cannot) open file '([^']+)': \[Errno 2\] No such file or directory",
        "hint": "agent script not found: {1}. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).",
    },
    {
        "id": "node-cannot-find-module",
        "pattern": r"Cannot find module '([^']+)'",
        "hint": "agent script not found: {1}. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).",
    },
    {
        "id": "bash-line-no-such-file",
        "pattern": r"(?:^|\n)[^:]+: line \d+: ([^:]+): No such file or directory",
        "hint": "agent script not found: {1}. Check the profile's command path.",
    },
    {
        "id": "generic-enoent",
        "pattern": r"errno 2|enoent|no such file or directory",
        "flags": re.IGNORECASE,
        "hint": "agent reported a missing file. Check the profile's command and cwd.",
    },
]


def _format_hint(hint: str, m: "re.Match[str]") -> str:
    return re.sub(r"\{(\d+)\}", lambda mm: (m.group(int(mm.group(1))) or ""), hint)


def diagnose_stderr_failure(stderr_text: str) -> str:
    """Best-effort pattern check against the agent's accumulated stderr.

    Catches "bridge file not found" failures that the wrapped interpreter
    writes to its own stderr before exiting non-zero. Returns a friendly
    hint, or ``""`` if nothing recognizable. Data-driven by
    ``STDERR_DIAGNOSTICS`` (the embedded mirror of
    ``spec/conformance/diagnostics.json``).
    """
    if not stderr_text:
        return ""
    for rule in STDERR_DIAGNOSTICS:
        flags = rule.get("flags", 0)
        m = re.search(rule["pattern"], stderr_text, flags)
        if m:
            return _format_hint(rule["hint"], m)
    return ""


# ---------------------------------------------------------------------------
# Event parsing (wire 0.3 — every stdout line is a JSON object)
# ---------------------------------------------------------------------------

def parse_json_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one stdout line as a JSON object. None on failure."""
    text = line.strip()
    if not text:
        return None
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(v, dict):
        return v
    return None


def classify_line(line: str) -> Dict[str, Any]:
    """Classify one stdout line into a typed event.

    Returns a dict with ``kind``:
      session | partial | text | error | permission_request | malformed
    """
    obj = parse_json_line(line)
    if not obj or not isinstance(obj.get("type"), str):
        return {"kind": "malformed", "value": line}
    t = obj["type"]
    if t == "session":
        return {"kind": "session", "value": obj.get("id") if isinstance(obj.get("id"), str) else ""}
    if t == "partial":
        out: Dict[str, Any] = {
            "kind": "partial",
            "value": obj.get("text") if isinstance(obj.get("text"), str) else "",
        }
        if isinstance(obj.get("role"), str):
            out["role"] = obj.get("role")
        return out
    if t == "text":
        return {"kind": "text", "value": obj.get("text") if isinstance(obj.get("text"), str) else ""}
    if t == "error":
        return {"kind": "error", "value": obj.get("message") if isinstance(obj.get("message"), str) else ""}
    if t == "permission_request":
        return {"kind": "permission_request", "value": obj}
    return {"kind": "malformed", "value": line}


def format_permission_response(decision: Dict[str, Any]) -> str:
    """Format a {"type":"permission_response",...} line for stdin."""
    payload: Dict[str, Any] = {
        "type": "permission_response",
        "request_id": str(decision["request_id"]),
        "behavior": "allow" if decision.get("behavior") == "allow" else "deny",
    }
    updated = decision.get("updated_input")
    if updated is not None and isinstance(updated, dict):
        payload["updated_input"] = updated
    message = decision.get("message")
    if message is not None and message != "":
        payload["message"] = str(message)
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def is_valid_permission_request(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    rid = obj.get("request_id")
    if not isinstance(rid, str) or not rid.strip():
        return False
    if re.search(r"[\s\r\n\x00-\x1f]", rid):
        return False
    tool = obj.get("tool_name")
    if not isinstance(tool, str) or tool == "":
        return False
    inp = obj.get("input")
    if not isinstance(inp, dict):
        return False
    return True


# Wire 0.3: the session id is an arbitrary JSON string on the wire (no
# colon/whitespace restriction — that was an artifact of the 0.2
# colon-delimited prefix). The only remaining constraint is STORAGE safety:
# the SDK history helpers store each session as <id>.jsonl, so an id
# containing a path separator (/ or \), a NUL / control char, or equal to
# ``.`` / ``..`` would path-traverse out of the sessions directory. The
# runner rejects such ids (preserving the previously captured id + a stderr
# warning) so they do not round-trip. Colons, spaces, ``+``, and unicode are
# all fine.
_SESSION_ID_RE = re.compile(r"[\/\\\x00-\x1f]")


def is_valid_session_id(value: Any) -> bool:
    """True if ``value`` is a storage-safe session id (non-empty, no path
    separators or control chars, not ``.`` or ``..``)."""
    if not isinstance(value, str) or not value:
        return False
    if value in (".", ".."):
        return False
    return not _SESSION_ID_RE.search(value)


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

    # Substitute placeholders in argv (command) too, not just args.
    argv = [substitute(a, subst_ctx) for a in profile["argv"]]
    for a in profile["args"]:
        argv.append(substitute(a, subst_ctx))

    allowlist = profile["env_allowlist"]
    env = build_base_env()
    for k, v in profile["env"].items():
        env[k] = expand_env_ref(
            substitute(str(v), subst_ctx),
            os.environ,
            allowlist=allowlist,
            on_blocked=(
                lambda name: options.on_stderr(
                    f"[agentproc runner] env_allowlist blocked ${{{name}}} "
                    f"(not in allowlist); expanded to empty"
                ) if options.on_stderr else None
            ),
        )
    for k, v in options.extra_env.items():
        env[k] = str(v)

    # Build the turn object (wire 0.3 stdin payload). No AGENT_* env in 0.3.
    turn: Dict[str, Any] = {
        "type": "turn",
        "message": options.message,
        "session_id": options.session_id,
        "session_name": options.session_name,
        "from_user": options.from_user,
        "protocol_version": PROTOCOL_VERSION,
    }
    # attachments: include the key when the caller provided a list
    # (presence-as-feature); omit otherwise.
    if options.attachments:
        turn["attachments"] = options.attachments
    if profile["permission"]:
        turn["permission"] = True

    result = RunResult()
    text_chunks: List[str] = []
    # Spec 5.4: once an error event arrives, subsequent partial/text events
    # MUST be discarded (they cannot contribute to a failed turn's reply).
    # `session` is exempt — last-wins still applies so the id for the next
    # turn can be captured even after an error.
    error_seen = False
    pending_permission_ids: set = set()
    stdin_lock = threading.Lock()
    stdin_closed = False
    # Streaming partial truncation tracking — see matching comment in runner.js.
    _cumulative_partial_chars = 0
    _partials_truncated = False
    _max_chars = profile["max_reply_chars"]
    _trunc_suffix = profile["truncation_suffix"]
    # Bounded head capture (1 MB) used for post-mortem pattern diagnosis.
    # The diagnostic patterns target interpreter-startup errors (file/module
    # not found) which appear in the first bytes, so a head cap preserves
    # the high-value signal without unbounded growth. Beyond the cap the
    # tail is dropped with a one-shot marker.
    stderr_full: List[str] = []
    STDERR_FULL_CAP = 1 << 20  # 1 MB
    stderr_full_len = 0
    stderr_full_truncated = False

    def _append_stderr(text: str) -> None:
        nonlocal stderr_full_len, stderr_full_truncated
        if stderr_full_len < STDERR_FULL_CAP:
            room = STDERR_FULL_CAP - stderr_full_len
            piece = text[:room]
            stderr_full.append(piece)
            stderr_full_len += len(piece)
        elif not stderr_full_truncated:
            marker = "\n[agentproc runner] stderr capped at 1 MB; trailing output dropped\n"
            stderr_full.append(marker)
            stderr_full_truncated = True

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
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

    def _write_permission_response(decision: Dict[str, Any]) -> bool:
        nonlocal stdin_closed
        with stdin_lock:
            if stdin_closed or proc.stdin is None or proc.stdin.closed:
                return False
            try:
                proc.stdin.write(format_permission_response(decision) + "\n")
                proc.stdin.flush()
                rid = decision.get("request_id")
                if rid is not None:
                    pending_permission_ids.discard(str(rid))
                return True
            except (BrokenPipeError, ValueError, OSError):
                return False

    def _close_stdin() -> None:
        nonlocal stdin_closed
        with stdin_lock:
            if stdin_closed or proc.stdin is None:
                return
            stdin_closed = True
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

    # Write the turn line; keep stdin open only when permission is on.
    try:
        with stdin_lock:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.write(json.dumps(turn, ensure_ascii=False, separators=(',', ':')) + "\n")
                proc.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass
    if not profile["permission"]:
        _close_stdin()

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
        nonlocal _cumulative_partial_chars, _partials_truncated, error_seen
        line = raw_line.rstrip("\r")
        c = classify_line(line)
        kind = c["kind"]
        if kind == "session":
            if not is_valid_session_id(c["value"]):
                if options.on_stderr:
                    options.on_stderr(
                        f"[agentproc runner] ignoring invalid session id "
                        f"{c['value']!r} (must be non-empty, no path separators "
                        "or control chars); previous session id preserved"
                    )
                if options.on_protocol_line:
                    options.on_protocol_line(line)
            else:
                result.session_id = c["value"]
                if options.on_session:
                    options.on_session(c["value"])
                if options.on_protocol_line:
                    options.on_protocol_line(line)
        elif kind == "partial":
            # Spec 5.4: post-error partials are discarded (not forwarded, not
            # appended). on_protocol_line still fires so debug traces stay
            # complete.
            if (
                not error_seen
                and streaming
                and options.on_partial
                and not _partials_truncated
            ):
                remaining = _max_chars - _cumulative_partial_chars
                if len(c["value"]) >= remaining:
                    if remaining > 0:
                        options.on_partial(c["value"][:remaining])
                    if _trunc_suffix:
                        options.on_partial(_trunc_suffix)
                    _partials_truncated = True
                else:
                    options.on_partial(c["value"])
                    _cumulative_partial_chars += len(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
        elif kind == "text":
            # Spec 5.4: post-error text is discarded.
            if not error_seen:
                text_chunks.append(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
        elif kind == "error":
            result.error = c["value"]
            error_seen = True
            if options.on_error:
                options.on_error(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
            pending_permission_ids.clear()
        elif kind == "permission_request":
            if not profile["permission"]:
                if options.on_stderr:
                    options.on_stderr(
                        '[agentproc runner] ignoring {"type":"permission_request"} '
                        "(profile.permission is not true)"
                    )
                if options.on_protocol_line:
                    options.on_protocol_line(line)
            elif not is_valid_permission_request(c["value"]):
                if options.on_stderr:
                    options.on_stderr(
                        f"[agentproc runner] malformed permission_request: {line[:200]!r}"
                    )
                rid = ""
                if isinstance(c["value"], dict):
                    raw_rid = c["value"].get("request_id")
                    if isinstance(raw_rid, str):
                        rid = raw_rid.strip()
                if rid and not re.search(r"[\s\r\n\x00-\x1f]", rid):
                    _write_permission_response({
                        "request_id": rid,
                        "behavior": "deny",
                        "message": "malformed permission request",
                    })
                if options.on_protocol_line:
                    options.on_protocol_line(line)
            else:
                req = c["value"]
                assert isinstance(req, dict)
                pending_permission_ids.add(req["request_id"])
                if options.on_protocol_line:
                    options.on_protocol_line(line)
                if options.on_permission is not None:
                    try:
                        decision = options.on_permission(req)
                        if isinstance(decision, dict):
                            # Spec: when the bridge omits updated_input, the
                            # response MUST omit it too — the agent (or wrapped
                            # CLI) is responsible for falling back to the
                            # request's original input. Don't auto-fill
                            # req["input"] here: that would erase the
                            # distinction between "user accepted unchanged"
                            # and "user never touched it" for downstream CLIs
                            # (e.g. Claude Code's updatedInput semantics).
                            updated = decision.get("updated_input")
                            if updated is None and "updatedInput" in decision:
                                updated = decision.get("updatedInput")
                            response_decision: Dict[str, Any] = {
                                "request_id": req["request_id"],
                                "behavior": (
                                    "allow" if decision.get("behavior") == "allow"
                                    else "deny"
                                ),
                                "message": decision.get("message"),
                            }
                            if isinstance(updated, dict):
                                response_decision["updated_input"] = updated
                            _write_permission_response(response_decision)
                    except Exception as exc:  # noqa: BLE001 — surface to agent as deny
                        if options.on_stderr:
                            options.on_stderr(
                                f"[agentproc runner] on_permission failed: {exc}"
                            )
                        _write_permission_response({
                            "request_id": req["request_id"],
                            "behavior": "deny",
                            "message": "permission handler error",
                        })
                # No on_permission: leave the agent blocked until turn timeout.
        else:
            # malformed: log + ignore (not forwarded as body in 0.3).
            if options.on_stderr:
                options.on_stderr(
                    f"[agentproc runner] ignoring malformed stdout line: {line[:200]!r}"
                )
            if options.on_protocol_line:
                options.on_protocol_line(line)

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
                        # Spec: prefer deny with timeout message for pending
                        # permission requests, then kill.
                        for rid in list(pending_permission_ids):
                            _write_permission_response({
                                "request_id": rid,
                                "behavior": "deny",
                                "message": "permission timed out",
                            })
                        _close_stdin()
                        # terminate() is SIGTERM on POSIX, TerminateProcess on
                        # Windows — both are the "polite shutdown" the spec's
                        # SIGTERM→SIGKILL contract refers to.
                        try:
                            proc.terminate()
                        except (ProcessLookupError, PermissionError):
                            pass
                        try:
                            proc.wait(timeout=profile["kill_grace_secs"])
                        except subprocess.TimeoutExpired:
                            try:
                                proc.kill()
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
        # SIGINT only exists on POSIX. On Windows we fall back to terminate().
        if hasattr(signal, "SIGINT"):
            try:
                proc.send_signal(signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            try:
                proc.terminate()
            except (ProcessLookupError, PermissionError):
                pass
        exit_code = proc.wait()

    _close_stdin()
    # Process has exited. Drain threads should hit EOF on their pipes within
    # milliseconds and finish. The hard timeout here is a backstop for the
    # rare case a grandchild inherited the stderr fd and is still alive — it
    # keeps the runner from hanging indefinitely without letting drain latency
    # balloon past the kill_grace_secs budget. 1s each = at most ~2s of extra
    # latency; diagnosis may be incomplete if it fires, but the post-mortem
    # stderr patterns target interpreter-startup errors that land in the
    # first bytes of stderr anyway, well within the 1MB head capture.
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    if stderr_thread.is_alive():
        if options.on_stderr:
            options.on_stderr("[agentproc runner] warning: stderr drain timed out; diagnosis may be incomplete")

    # Reply body = concatenation of {"type":"text"} events (direct, no separator).
    result.reply = "".join(text_chunks)
    if len(result.reply) > profile["max_reply_chars"]:
        result.reply = result.reply[: profile["max_reply_chars"]] + profile["truncation_suffix"]

    # If the agent exited non-zero with no error event, peek at its stderr for
    # common "command/file not found" patterns and surface a friendly hint.
    # Uses the head-capped stderr_full (1 MB) — the interpreter-startup errors
    # these patterns target land in the first bytes, well within the cap.
    if not timed_out and not result.error and exit_code != 0:
        stderr_text = "".join(stderr_full)
        hint = diagnose_stderr_failure(stderr_text)
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
