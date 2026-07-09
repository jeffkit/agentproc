"""AgentProc runner — the canonical bridge-side engine.

This module is the canonical implementation of the AgentProc bridge-side
contract (spec/protocol.md, wire protocol 0.2). The CLI (cli.py) is a thin
wrapper around it.

Responsibilities:
  - Parse and validate a profile dict
  - Substitute {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}} placeholders
  - Inject AGENT_* env vars + profile env block
  - Spawn the agent command (no shell)
  - Read stdout line by line, classify protocol lines vs reply body
  - Forward AGENT_PARTIAL: in real time (via on_partial callback)
  - Capture the last AGENT_SESSION: line (last-wins rule)
  - Honor AGENT_ERROR: lines
  - Optional tool permission (profile.permission): keep stdin open, honor
    AGENT_PERMISSION_REQUEST: / write AGENT_PERMISSION_RESPONSE:
  - Enforce timeout_secs with SIGTERM → kill_grace_secs → SIGKILL
  - Write message to stdin and close (when profile.stdin == 'message'
    and permission is off)
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
    "ENV_INFRA_VARS",
    "build_base_env",
    "RunResult",
    "RunOptions",
    "run",
    "normalize_profile",
    "classify_line",
    "is_valid_session_id",
    "decode_json_value",
    "decode_json_object",
    "format_permission_response",
    "is_valid_permission_request",
    "substitute",
    "expand_env_ref",
    "expand_path",
    "STDERR_DIAGNOSTICS",
    "diagnose_stderr_failure",
]

PROTOCOL_VERSION = "0.2"

DEFAULT_TIMEOUT_SECS = 1800
DEFAULT_KILL_GRACE_SECS = 5
DEFAULT_MAX_REPLY_CHARS = 8000

PREFIX_SESSION = "AGENT_SESSION:"
PREFIX_PARTIAL = "AGENT_PARTIAL:"
PREFIX_ERROR = "AGENT_ERROR:"
PREFIX_PERMISSION_REQUEST = "AGENT_PERMISSION_REQUEST:"
PREFIX_PERMISSION_RESPONSE = "AGENT_PERMISSION_RESPONSE:"

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 124
EXIT_SIGINT = 130
EXIT_SIGTERM = 143


# ---------------------------------------------------------------------------
# Environment inheritance policy
# ---------------------------------------------------------------------------

# When a profile declares ``env_allowlist``, the child process does NOT inherit
# the bridge's environment wholesale — doing so would let any secret the bridge
# happens to hold reach the agent even though the profile never declared it,
# making ``env_allowlist`` a cosmetic filter rather than a trust boundary.
# Instead the child env is built from: (1) this minimal INFRA set (copied from
# ``os.environ`` when present) so the agent can still find its interpreter /
# temp dir / locale, (2) the profile ``env`` block (allowlist-filtered),
# (3) the injected ``AGENT_*`` vars, (4) ``extra_env`` from the CLI ``--env``
# flag. Secrets not declared by the profile never reach the agent. When
# ``env_allowlist`` is absent, the bridge inherits ``os.environ`` wholesale
# (back-compat) — the trust-the-profile behaviour.
ENV_INFRA_VARS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TERM", "TMPDIR", "TZ", "PWD",
    # Windows infra
    "SystemRoot", "TEMP", "TMP", "USERPROFILE", "USERNAME", "PATHEXT",
    "COMSPEC", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS",
)


def build_base_env(allowlist: Optional[set]) -> Dict[str, str]:
    """Build the child process base env per the inheritance policy.

    ``None`` allowlist → full ``os.environ`` inheritance (back-compat).
    A set → only the infra vars, ready for profile.env / AGENT_* / extra_env
    to be layered on.
    """
    if allowlist is None:
        return dict(os.environ)
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
    image_url: str = ""
    file_url: str = ""
    cwd: Optional[str] = None
    profile_dir: Optional[str] = None
    timeout_secs: Optional[int] = None
    on_partial: Optional[Callable[[str], None]] = None
    on_session: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_protocol_line: Optional[Callable[[str], None]] = None
    on_stderr: Optional[Callable[[str], None]] = None
    on_permission: Optional[Callable[[Dict[str, Any]], Any]] = None
    forward_stdin: Optional[bool] = None


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

    has_args_field = "args" in src and src["args"] is not None
    args_value = src.get("args") or []
    if not isinstance(args_value, list):
        raise ValueError("profile.args must be a list")

    # Per spec: `command` is argv[0]; `args` is argv[1..]. Two mutually
    # exclusive forms:
    #   (a) `args` absent + command has whitespace → split command into argv
    #       (the legacy shorthand: `command: python3 ./bridge.py`)
    #   (b) `args` present (even empty `[]`) → command is a single token,
    #       never split. This is the escape hatch for paths with whitespace:
    #         command: "/path with spaces/my agent"
    #         args: []
    # `args: []` (explicit empty list) is DISTINCT from "args absent": the
    # explicit form means "do not split command"; the absent form falls back
    # to the whitespace-splitting shorthand.
    if has_args_field:
        argv = [command.strip()]
    else:
        argv = command.strip().split()
    if not argv or not argv[0]:
        raise ValueError("profile.command produced empty argv")

    cwd_value = src.get("cwd")
    env_value = src.get("env") or {}
    if not isinstance(env_value, dict):
        raise ValueError("profile.env must be a dict")

    # env_allowlist (optional): when present, ${VAR} references in the env
    # block whose name is NOT in the list expand to empty + a stderr warning.
    # Absent ⇒ current behaviour (expand against the full bridge environment).
    # Opt-in: existing profiles keep working unchanged.
    allowlist_raw = src.get("env_allowlist")
    if allowlist_raw is None:
        env_allowlist: Optional[set] = None
    elif isinstance(allowlist_raw, list):
        env_allowlist = {str(x) for x in allowlist_raw}
    else:
        raise ValueError("profile.env_allowlist must be a list")

    return {
        "argv": argv,
        "args": [str(a) for a in args_value],
        "cwd": expand_path(str(cwd_value)) if cwd_value else None,
        "env": env_value,
        "env_allowlist": env_allowlist,
        "stdin": "message" if src.get("stdin") == "message" else "none",
        # Opt-in tool-authorization channel (wire 0.2). Default False.
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
# Line classification (per spec)
# ---------------------------------------------------------------------------

def decode_json_value(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        return text
    # Only JSON strings are meaningful payloads — a sentinel's value is text
    # for the user. Non-string JSON (number/bool/null/array/object) means the
    # agent misused the API; fall back to the raw text so the result is
    # language-independent (str(True) != String(true) across runtimes).
    return v if isinstance(v, str) else text


def decode_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object payload (permission frames). None on failure."""
    text = str(raw).strip()
    if not text:
        return None
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(v, dict):
        return v
    return None


def format_permission_response(decision: Dict[str, Any]) -> str:
    """Format an AGENT_PERMISSION_RESPONSE: line for stdin."""
    payload: Dict[str, Any] = {
        "request_id": str(decision["request_id"]),
        "behavior": "allow" if decision.get("behavior") == "allow" else "deny",
    }
    updated = decision.get("updated_input")
    if updated is not None and isinstance(updated, dict):
        payload["updated_input"] = updated
    message = decision.get("message")
    if message is not None and message != "":
        payload["message"] = str(message)
    return PREFIX_PERMISSION_RESPONSE + json.dumps(payload, ensure_ascii=False)


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


def classify_line(line: str) -> Dict[str, Any]:
    if line.startswith(PREFIX_SESSION):
        return {"kind": "session", "value": line[len(PREFIX_SESSION):].strip()}
    if line.startswith(PREFIX_PARTIAL):
        return {"kind": "partial", "value": decode_json_value(line[len(PREFIX_PARTIAL):])}
    if line.startswith(PREFIX_ERROR):
        return {"kind": "error", "value": decode_json_value(line[len(PREFIX_ERROR):])}
    if line.startswith(PREFIX_PERMISSION_REQUEST):
        return {
            "kind": "permission_request",
            "value": decode_json_object(line[len(PREFIX_PERMISSION_REQUEST):]),
        }
    return {"kind": "body", "value": line}


# Per spec: session id is opaque but MUST NOT contain whitespace, control
# characters, or colons. Valid: base64url alphabet (A-Z a-z 0-9 - _) plus
# . ~ = ; non-empty. `/` and `+` are deliberately excluded — `/` makes the id
# unsafe as a filename component (the SDK history helpers store <id>.jsonl,
# and a `/`-bearing id would path-traverse). A session line whose value
# fails this is ignored (previous id preserved).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._~=-]+$")


def is_valid_session_id(value: str) -> bool:
    """True if ``value`` is a spec-compliant session id (non-empty, no
    whitespace / control chars / colons)."""
    return bool(value) and bool(_SESSION_ID_RE.fullmatch(value))


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

    allowlist = profile["env_allowlist"]
    env = build_base_env(allowlist)
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

    env["AGENT_MESSAGE"] = options.message
    env["AGENT_SESSION_ID"] = options.session_id
    env["AGENT_SESSION_NAME"] = options.session_name
    env["AGENT_FROM_USER"] = options.from_user
    env["AGENT_STREAMING"] = "1" if streaming else "0"
    env["AGENT_PROTOCOL_VERSION"] = PROTOCOL_VERSION
    # Single-attachment passthrough. Only inject when non-empty so that an
    # unset variable stays unset (matches the spec's "set when present" rule;
    # an agent can distinguish "no image" from "image URL is empty string").
    if options.image_url:
        env["AGENT_IMAGE_URL"] = options.image_url
    if options.file_url:
        env["AGENT_FILE_URL"] = options.file_url
    if profile["permission"]:
        env["AGENT_PERMISSION"] = "1"

    # Stdin is a pipe when we need to write the message and/or keep the
    # channel open for mid-turn AGENT_PERMISSION_RESPONSE: lines.
    write_message = profile["stdin"] == "message" or bool(options.forward_stdin)
    need_stdin_pipe = profile["permission"] or write_message
    stdin_payload = options.message if write_message else None

    result = RunResult()
    body_lines: List[str] = []
    pending_permission_ids: set = set()
    stdin_lock = threading.Lock()
    stdin_closed = not need_stdin_pipe
    # Streaming partial truncation tracking — see matching comment in runner.js.
    _cumulative_partial_chars = 0
    _partials_truncated = False
    _max_chars = profile["max_reply_chars"]
    _trunc_suffix = "\n\n…(truncated)" if _max_chars == DEFAULT_MAX_REPLY_CHARS else ""
    # Two views on stderr:
    #   - stderr_window: bounded sliding window (8 KB) for the on_stderr UI
    #     callback, so a noisy agent cannot exhaust memory.
    #   - stderr_full:   bounded head capture (1 MB) used only for post-mortem
    #     pattern diagnosis. The diagnostic patterns target interpreter-startup
    #     errors (file/module not found) which appear in the first bytes, so a
    #     head cap preserves the high-value signal without unbounded growth.
    #     Beyond the cap the tail is dropped with a one-shot marker.
    stderr_window: List[str] = []
    stderr_full: List[str] = []
    STDERR_CAP = 8192
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
        stderr_window.append(text)
        total = sum(len(s) for s in stderr_window)
        while total > STDERR_CAP and len(stderr_window) > 1:
            total -= len(stderr_window.pop(0))

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if need_stdin_pipe else subprocess.DEVNULL,
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

    if need_stdin_pipe:
        if stdin_payload is not None:
            try:
                with stdin_lock:
                    if proc.stdin is not None and not proc.stdin.closed:
                        proc.stdin.write(stdin_payload)
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
        nonlocal _cumulative_partial_chars, _partials_truncated
        line = raw_line.rstrip("\r")
        c = classify_line(line)
        if c["kind"] == "session":
            if not is_valid_session_id(c["value"]):
                if options.on_stderr:
                    options.on_stderr(
                        f"[agentproc runner] ignoring invalid AGENT_SESSION value "
                        f"{c['value']!r} (must be non-empty, no whitespace/"
                        "control chars/colons); previous session id preserved"
                    )
                if options.on_protocol_line:
                    options.on_protocol_line(line)
            else:
                result.session_id = c["value"]
                if options.on_session:
                    options.on_session(c["value"])
                if options.on_protocol_line:
                    options.on_protocol_line(line)
        elif c["kind"] == "partial":
            if streaming and options.on_partial and not _partials_truncated:
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
        elif c["kind"] == "error":
            result.error = c["value"]
            if options.on_error:
                options.on_error(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
            pending_permission_ids.clear()
        elif c["kind"] == "permission_request":
            if not profile["permission"]:
                if options.on_stderr:
                    options.on_stderr(
                        "[agentproc runner] ignoring AGENT_PERMISSION_REQUEST: "
                        "(profile.permission is not true)"
                    )
                if options.on_protocol_line:
                    options.on_protocol_line(line)
            elif not is_valid_permission_request(c["value"]):
                if options.on_stderr:
                    options.on_stderr(
                        "[agentproc runner] malformed AGENT_PERMISSION_REQUEST: "
                        f"{line[:200]!r}"
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
                            updated = decision.get("updated_input")
                            if updated is None and "updatedInput" in decision:
                                updated = decision.get("updatedInput")
                            if updated is None:
                                updated = req.get("input")
                            _write_permission_response({
                                "request_id": req["request_id"],
                                "behavior": (
                                    "allow" if decision.get("behavior") == "allow"
                                    else "deny"
                                ),
                                "updated_input": updated if isinstance(updated, dict) else req.get("input"),
                                "message": decision.get("message"),
                            })
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
    stdout_thread.join(timeout=5)
    # Drain stderr fully before running post-mortem diagnosis. The diagnosis
    # reads the full capture, so we need the drain thread to have caught up.
    # Generous timeout since the process has already exited — at most we wait
    # for the pipe buffer to flush.
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
