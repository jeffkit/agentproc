"""AgentProc runner — the canonical bridge-side engine.

This module is the canonical implementation of the AgentProc bridge-side
contract (spec/protocol.md, wire protocol 0.4). The CLI (cli.py) is a thin
wrapper around it.

Wire 0.4 is NDJSON in both directions:
  - stdin:  one {"type":"turn",...} line, then optional
            {"type":"permission_response",...} lines when permission is on.
  - stdout: one JSON object per line, discriminated by `type`:
            partial | result | error | permission_request.
            Optional ``session_id`` field on events (first non-empty wins).

Responsibilities:
  - Parse and validate a profile dict
  - Substitute {{SESSION_ID}}, {{SESSION_NAME}}, {{PROFILE_DIR}} placeholders in argv and env
  - Build the child env (infra set + profile env block + CLI --env extras)
  - Spawn the agent command (no shell); command is always argv[0], never split
  - Write the turn object to the agent's stdin (and keep stdin open when
    profile.permission is true, for permission_response traffic)
  - Read stdout line by line, parse each line as a JSON event
  - Forward {"type":"partial"} in real time (via on_partial callback)
  - Persist the first non-empty valid ``session_id`` on events
  - Honor at most one {"type":"result"} (body assembly vs streamed partials)
  - Honor {"type":"error"} events
  - Optional tool permission: honor permission_request / write permission_response
  - Enforce timeout_secs with SIGTERM → kill_grace_secs → SIGKILL
  - Return RunResult(reply, session_id, error, exit_code, timed_out)
"""

from __future__ import annotations

import codecs
import inspect
import io
import json
import os
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

from . import run_lock as _run_lock

# Imported lazily to avoid circular imports; only used by run_via_executor.
from agentproc.executors import EXECUTORS, executor_names  # noqa: E402

__all__ = [
    "PROTOCOL_VERSION",
    "DEFAULT_TIMEOUT_SECS",
    "DEFAULT_KILL_GRACE_SECS",
    "EXIT_SUCCESS",
    "EXIT_ERROR",
    "EXIT_TIMEOUT",
    "EXIT_CANCELLED",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "ENV_INFRA_VARS",
    "build_base_env",
    "_compose_env",
    "AUTO_APPROVE_FLAGS",
    "RunResult",
    "RunOptions",
    "run",
    "run_via_executor",
    "normalize_profile",
    "parse_deadline",
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
    "EXECUTORS",
    "executor_names",
]

PROTOCOL_VERSION = "0.4"

DEFAULT_TIMEOUT_SECS = 1800
DEFAULT_KILL_GRACE_SECS = 5

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 124
# 协作式取消（cancel_event 命中）：与超时区分，宿主据此判定「被取消」而非
# 「失败」——plaita worker 取消监听置位后，取消不是错误终态。
EXIT_CANCELLED = 125
EXIT_SIGINT = 130
EXIT_SIGTERM = 143

# argv tokens that mean "auto-approve everything". Single source of truth is
# `auto_approve_flags` in spec/conformance/cases.json; the conformance driver
# asserts the two lists are equal item for item.
AUTO_APPROVE_FLAGS = (
    "--dangerously-skip-permissions",
    "--yolo",
    "--always-approve",
    "--yes-always",
    "--approve",
    "--auto",
)


def _auto_approve_enabled() -> bool:
    """Whether executors may bake auto-approve flags into argv.

    Read from the runner process's own environment (the spec makes
    AGENTPROC_AUTO_APPROVE a process-side knob, not a profile field). `0` /
    `false`, case-insensitive and whitespace-trimmed, turns it off.
    """
    value = os.environ.get("AGENTPROC_AUTO_APPROVE", "").strip().lower()
    return value not in ("0", "false")


def _posture_refusal(
    cli_name: str,
    supports_permission: bool,
    permission: bool,
    auto_approve_enabled: bool,
    argv: List[str],
) -> Optional[str]:
    """Why this turn must not be spawned, or None to proceed."""
    if permission and not supports_permission:
        return (
            f"executor '{cli_name}' has no AgentProc permission channel; "
            "refusing to run with auto-approve. Remove 'permission: true' from "
            "the profile, or use an executor that supports it (claude-code)."
        )
    if not auto_approve_enabled:
        for token in AUTO_APPROVE_FLAGS:
            if token in argv:
                return (
                    f"executor '{cli_name}' would run with the auto-approve flag "
                    f"'{token}', but AGENTPROC_AUTO_APPROVE is off. Unset "
                    "AGENTPROC_AUTO_APPROVE, or use a profile that does not need "
                    "auto-approval."
                )
    return None


def _normalise_exit_code(code):
    """Normalise a child exit status to a spec exit code.

    POSIX wait() reports signal deaths as a negative returncode (-15 for
    SIGTERM); normalise to 128 + signo (SIGINT → 130, SIGTERM → 143, matching
    EXIT_SIGINT/EXIT_SIGTERM). None (no status) → EXIT_ERROR. Windows
    returncodes are never negative, so the mapping is a no-op there.
    """
    if code is None:
        return EXIT_ERROR
    if code < 0:
        return 128 + (-code)
    return code


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
    # 代理（大小写两式都要，不同 CLI/库取的不一样）——2026-10-10：cursor-agent
    # 的**登录态校验必须走代理**，白名单缺这些变量时它拿不到 login 态、静默
    # 退化成「API-key 可用模型」子集，报错却是**误导性的**
    # `Cannot use this model: claude-4.6-sonnet-medium. Available models: auto,
    # composer-2.5, cursor-grok-…`（真因是认证降级，不是模型名错）。
    # 实测：白名单 + 仅 HTTPS_PROXY/https_proxy → `apiKeySource: login` 成功。
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
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
    usage: Optional[Dict[str, Any]] = None
    # Bridge-measured timing (spec "Event traceability"): started_at is the
    # ISO-8601 UTC turn-start instant; duration is bridge wall-clock seconds
    # covering spawn-to-exit. Distinct from agent self-reported usage.duration_ms.
    started_at: str = ""
    duration: float = 0.0


@dataclass
class RunOptions:
    """Options passed to run()."""

    message: str
    session_id: str = ""
    session_name: str = "default"
    streaming: Optional[bool] = None
    extra_env: Dict[str, str] = field(default_factory=dict)
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    cwd: Optional[str] = None
    profile_dir: Optional[str] = None
    # 遗言锁键（通常=workspace 绝对路径）：设置后 spawn 写 / 收尾清
    # run-lock 文件，供 kill-before-start 孤儿清场定位进程组（run_lock 模块）。
    run_lock_key: Optional[str] = None
    timeout_secs: Optional[int] = None
    # 协作式取消：一个 ``threading.Event`` 样对象（需有 ``is_set()``）。设置后
    # 运行器在等待子进程期间轮询该事件，命中即对 agent 进程组分级击杀
    # （SIGTERM → kill_grace_secs 宽限 → SIGKILL），返回 timed_out=False、
    # exit_code=EXIT_CANCELLED 的 RunResult。None = 不启用（现状）。
    cancel_event: Optional[Any] = None
    # Journal hook (spec "Event traceability", opt-in): called with a dict
    # per classified event / bridge-level decision. None ⇒ no journaling.
    on_journal: Optional[Callable[[Dict[str, Any]], None]] = None
    # Called as ``on_partial(text, role)`` where ``role`` is the partial's
    # ``role`` field (``None`` when the event carries none). A one-argument
    # callback still works: the arity is inspected once per run.
    on_partial: Optional[Callable[..., None]] = None
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

    # executor: optional SDK-registered name for in-process execution.
    executor_name = src.get("executor")
    if executor_name is not None and not isinstance(executor_name, str):
        executor_name = None
    else:
        executor_name = executor_name.strip() if executor_name else None

    command = src.get("command")
    # command is required unless executor is set
    if not executor_name and (not isinstance(command, str) or not command.strip()):
        raise ValueError(
            "profile.command must be a non-empty string "
            "(or set executor: to use an in-process executor)"
        )

    args_value = src.get("args") or []
    if not isinstance(args_value, list):
        raise ValueError("profile.args must be a list")

    # Wire 0.3: `command` is always argv[0], a single token, NEVER split —
    # even if it contains whitespace. `args` is argv[1..], a YAML list of
    # tokens, defaulting to []. The 0.2 "args absent ⇒ split command on
    # whitespace" shorthand is removed. Paths with whitespace are carried
    # whole by YAML quoting and passed to execve as one token.
    argv = [command.strip()] if command else []

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
        "command": command.strip() if command else None,
        "executor": executor_name,
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
        # Optional time budget / absolute deadline (spec "Time budget and
        # absolute deadline"); None when absent. deadline kept as raw string
        # here — parsed against turn-start at run() time.
        "budget_secs": (
            float(src["budget_secs"]) if _is_num_like(src.get("budget_secs"))
            else None
        ),
        "deadline": src.get("deadline"),
        # Bridge-side hint: when False, the runner ignores {"type":"partial"}
        # events and assembles the reply from {"type":"text"} events only.
        "streaming": src.get("streaming", True) is not False,
    }


def _utc_now_iso() -> str:
    # Millisecond precision + "+00:00" offset (spec "Event traceability"): the
    # journal `ts` and RunResult.started_at must be comparable across bridges.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_deadline(value: Any) -> Optional[float]:
    """Parse a profile `deadline` into epoch seconds; None when absent.

    Timezone-aware ISO-8601 only; a naive timestamp or unparseable value is a
    profile validation error (spec: bridges MUST NOT silently ignore it).
    Python 3.9's fromisoformat rejects the 'Z' suffix, so normalize it first.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("profile.deadline must be an ISO-8601 timestamp string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"profile.deadline must be a timezone-aware ISO-8601 timestamp, got {value!r}"
        ) from None
    if dt.tzinfo is None:
        raise ValueError(
            f"profile.deadline must include a timezone offset, got {value!r}"
        )
    return dt.timestamp()


def _is_int_like(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return True
    return False


def _is_num_like(v: Any) -> bool:
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v)
            return True
        except ValueError:
            return False
    return False


def expand_path(p: str) -> str:
    if p == "~":
        return str(Path.home())
    if p.startswith("~/"):
        return str(Path.home() / p[2:])
    return p


def substitute(value: str, ctx: Dict[str, str]) -> str:
    """Substitute {{SESSION_ID}}, {{SESSION_NAME}}, {{PROFILE_DIR}} placeholders.

    {{MESSAGE}} is intentionally not substituted — message travels via stdin only.
    """
    return (
        str(value)
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
# Event parsing (wire 0.4 — every stdout line is a JSON object)
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


def _attach_session_id(out: Dict[str, Any], obj: Dict[str, Any]) -> Dict[str, Any]:
    """Copy optional non-empty ``session_id`` onto a classified event."""
    sid = obj.get("session_id")
    if isinstance(sid, str) and sid != "":
        out["session_id"] = sid
    return out


def classify_line(line: str) -> Dict[str, Any]:
    """Classify one stdout line into a typed event.

    Returns a dict with ``kind``:
      partial | result | error | permission_request | malformed

    ``session`` and ``text`` (wire 0.3) are unknown in 0.4 → malformed.
    Recognised events MAY carry a string ``session_id`` field on the result.
    """
    obj = parse_json_line(line)
    if not obj or not isinstance(obj.get("type"), str):
        return {"kind": "malformed", "value": line}
    t = obj["type"]
    if t == "partial":
        out: Dict[str, Any] = {
            "kind": "partial",
            "value": obj.get("text") if isinstance(obj.get("text"), str) else "",
        }
        if isinstance(obj.get("role"), str):
            out["role"] = obj.get("role")
        return _attach_session_id(out, obj)
    if t == "result":
        out = {
            "kind": "result",
            "value": obj.get("text") if isinstance(obj.get("text"), str) else "",
        }
        usage = obj.get("usage")
        if isinstance(usage, dict):
            out["usage"] = usage
        return _attach_session_id(out, obj)
    if t == "error":
        out = {
            "kind": "error",
            "value": obj.get("message") if isinstance(obj.get("message"), str) else "",
        }
        usage = obj.get("usage")
        if isinstance(usage, dict):
            out["usage"] = usage
        return _attach_session_id(out, obj)
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


# Wire 0.4: the session id is an arbitrary JSON string on the wire (no
# colon/whitespace restriction — that was an artifact of the 0.2
# colon-delimited prefix). The only remaining constraint is STORAGE safety:
# the SDK history helpers store each session as <id>.jsonl, so an id
# containing a path separator (/ or \), a NUL / control char, or equal to
# ``.`` / ``..`` would path-traverse out of the sessions directory. The
# runner rejects such ids (preserving the previously captured id + a stderr
# warning) so they do not round-trip. Colons, spaces, ``+``, and unicode are
# all fine. Persistence is first-non-empty (not last-wins).
_SESSION_ID_RE = re.compile(r"[\/\\\x00-\x1f]")


def is_valid_session_id(value: Any) -> bool:
    """True if ``value`` is a wire-valid session id (non-empty, no path
    separators or control chars, not ``.`` or ``..``).

    This constraint is enforced at the wire classification step — not just at
    file-persistence time — because the SDK history helpers store sessions as
    ``<id>.jsonl`` flat files; a ``/`` in the id would create a subdirectory.
    """
    if not isinstance(value, str) or not value:
        return False
    if value in (".", ".."):
        return False
    return not _SESSION_ID_RE.search(value)


# ---------------------------------------------------------------------------
# run_via_executor() — in-process executor path
# ---------------------------------------------------------------------------

def _signal_process_group(proc: subprocess.Popen, sig: int) -> None:
    """对 agent 的整个进程组发信号（子进程用 start_new_session 脱离本组时尽力而为）。"""
    import os
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_process_group(
    proc: subprocess.Popen, on_warning: Optional[Callable[[str], None]] = None
) -> None:
    """超时兜底：SIGKILL 整个进程组并回收。"""
    import time
    _signal_process_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, ValueError):
        time.sleep(0.1)
        try:
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            # unwaitable child — surface a structured warning if we can
            if on_warning is not None:
                on_warning(
                    f"[agentproc runner] warning: child pid={proc.pid} "
                    "not reaped after SIGKILL"
                )


# Spawn-path drain tuning: how long a drain thread parks in poll() before it
# re-checks "child reaped and pipe drained?" (see _iter_pipe_lines) and the
# read chunk size.
_PIPE_POLL_MS = 20
_PIPE_CHUNK = 65536


def _iter_pipe_lines(stream: Any, writer_gone: Callable[[], bool]) -> Iterator[str]:
    """Yield the lines one child wrote to its pipe ``stream``, then stop.

    Iterating ``proc.stdout`` — the simpler spelling — cannot be used on
    POSIX: once the direct child has exited its bytes sit in the pipe, but the
    iterator's next read only returns after *every* duplicate of the write end
    is closed, and a helper process the agent spawned inherits one and may
    hold it for minutes. The drain thread is then blocked with the child's
    tail in hand and the caller, having no way to tell that tail apart from a
    straggler writing after the turn ended, drops it — observed as flaky
    lost ``result`` / ``partial`` lines (empty reply, missing session id).

    ``poll`` replaces the guess: by the time the child has been reaped, every
    byte it wrote is already in the pipe, so "writer gone + pipe empty" is a
    sound end-of-turn boundary and the child's own tail can no longer be
    missed. The boundary is that probe, not the child's exit: a straggler
    writing in the interval between the two is indistinguishable from the
    child, so its bytes are read as part of this turn (they can flip ``reply``
    to empty through ``partials_forwarded``, or have a ``session_id``
    adopted). That window is not one probe interval wide: the probe is only
    consulted while the pipe is idle, so a straggler that keeps writing keeps
    this loop reading until the caller's join backstop gives up on the drain
    thread (1 s per pipe). What is guaranteed is the other direction — once
    the boundary is observed the drain is over, so no callback fires after
    ``run()`` returns.

    ``writer_gone`` therefore MUST only become true once ``proc.wait()`` has
    returned — or, on the kill/cancel paths, given up after its last timed
    wait expired. Lines are decoded exactly like ``text=True, errors="replace"``
    iteration: universal newlines, replacement characters for malformed UTF-8,
    a final unterminated fragment yielded as-is — including at the boundary
    above, where the writer is gone and the fragment is the child's own tail.
    (A straggler mid-line across the boundary is indistinguishable from the
    child and is yielded the same way.)

    ``poll`` rather than ``select`` because ``select`` cannot watch a pipe fd
    at or above ``FD_SETSIZE`` (a bridge run with a raised ``RLIMIT_NOFILE``
    would get ``ValueError``) and cannot watch a pipe fd at all on Windows —
    Winsock ``select`` accepts sockets only, so every spawn-path turn there
    would be reported as a drain failure. Where ``poll`` does not exist the
    stream is iterated blocking instead: the turn ends at EOF or at the join
    backstop, so a straggler-held tail can still be lost there — but never as
    a drain failure.
    """
    if not hasattr(select, "poll"):
        yield from stream
        return

    fd = stream.fileno()
    decoder = io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True
    )
    pending = ""

    def _split(text: str) -> Iterator[str]:
        nonlocal pending
        pending += text
        lines = pending.split("\n")
        pending = lines.pop()
        for line in lines:
            yield line + "\n"

    poller = select.poll()
    poller.register(fd, select.POLLIN)
    while True:
        if not poller.poll(_PIPE_POLL_MS):
            # Re-probe the pipe *after* observing the reaped child: it may
            # have written between the probe above and its exit.
            if writer_gone() and not poller.poll(0):
                # Nothing left to read and the writer is gone: an
                # unterminated fragment here is the child's own last line.
                if pending:
                    yield pending
                return
            continue
        chunk = os.read(fd, _PIPE_CHUNK)
        if not chunk:
            yield from _split(decoder.decode(b"", True))
            if pending:
                yield pending
            return
        yield from _split(decoder.decode(chunk, False))


def _compose_env(
    profile: Dict[str, Any],
    options: RunOptions,
    subst_ctx: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Compose the child env — the single shared three-layer policy.

    (1) infra set (build_base_env), (2) profile env block (${VAR} expanded,
    env_allowlist-filtered), (3) extra_env from the CLI --env flag.
    Both the spawn path and the executor path MUST use this — no third
    composition implementation (spec: "no inherit-everything mode").
    """
    if subst_ctx is None:
        subst_ctx = {
            "message": options.message,
            "session_id": options.session_id,
            "session_name": options.session_name,
            "profile_dir": options.profile_dir or "",
        }
    env = build_base_env()
    allowlist = profile["env_allowlist"]
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
    return env


def _partial_arity(cb: Optional[Callable[..., None]]) -> int:
    """How many positional arguments ``cb`` takes (2 = it can receive a role).

    Conservative: anything not clearly callable with two positional arguments
    is treated as one-argument, which is the pre-``role`` behaviour.
    """
    if cb is None:
        return 1
    try:
        params = list(inspect.signature(cb).parameters.values())
    except (TypeError, ValueError):
        return 1
    positional = 0
    for p in params:
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            return 2
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return 2 if positional >= 2 else 1


def _emit_partial(
    cb: Callable[..., None], text: str, role: Optional[str], arity: int
) -> None:
    if arity == 2:
        cb(text, role)
    else:
        cb(text)


def run_via_executor(
    executor: Dict[str, Any],
    options: RunOptions,
    profile: Optional[Dict[str, Any]] = None,
) -> RunResult:
    """Run using a registered in-process executor (no bridge subprocess).

    Mirrors the Node SDK's ``runViaExecutor`` in runner.js. When ``profile``
    is omitted (direct calls in old tests), profile-driven fields fall back
    to their defaults — ``run()`` always passes the normalised profile.
    """
    cli_name = executor.get("cli_name", "unknown")
    result = RunResult(exit_code=EXIT_ERROR)
    permission = bool((profile or {}).get("permission"))
    partial_arity = _partial_arity(options.on_partial)

    if profile is None:
        profile = normalize_profile({"executor": cli_name})

    streaming = (
        options.streaming if options.streaming is not None else profile["streaming"]
    )
    timeout_secs = (
        options.timeout_secs if options.timeout_secs is not None else profile["timeout_secs"]
    )
    kill_grace_secs = profile["kill_grace_secs"]
    cwd = options.cwd or profile["cwd"]
    if cwd and not Path(cwd).is_absolute() and options.profile_dir:
        cwd = str(Path(options.profile_dir) / cwd)

    env = _compose_env(profile, options)

    make_handlers = executor.get("make_handlers")
    if callable(make_handlers):
        handlers = make_handlers()
    else:
        handlers = executor

    build_args_fn = handlers.get("build_args") if isinstance(handlers, dict) else getattr(handlers, "build_args", None)
    if not callable(build_args_fn):
        result.error = f"executor '{cli_name}' has no build_args"
        if options.on_error:
            options.on_error(result.error)
        return result

    try:
        argv = build_args_fn(
            options.message or "",
            options.session_id or "",
            env,
            {"permission": permission},
        )
    except Exception as exc:
        result.error = f"executor '{cli_name}' build_args raised: {exc}"
        if options.on_error:
            options.on_error(result.error)
        return result

    if not argv:
        result.error = f"executor '{cli_name}' build_args returned empty argv"
        if options.on_error:
            options.on_error(result.error)
        return result

    # Optional stdin channel: an executor whose CLI reads its prompt from stdin
    # returns the payload here, keeping the user message out of argv (see spec
    # "Message delivery and argv"). None / absent ⇒ the CLI's stdin is the
    # null device — the executor path writes nothing else.
    build_stdin_fn = (
        handlers.get("build_initial_stdin") if isinstance(handlers, dict)
        else getattr(handlers, "build_initial_stdin", None)
    )
    initial_stdin: Optional[str] = None
    if callable(build_stdin_fn):
        try:
            initial_stdin = build_stdin_fn(options.message or "", options.session_id or "")
        except Exception as exc:
            result.error = f"executor '{cli_name}' build_initial_stdin raised: {exc}"
            if options.on_error:
                options.on_error(result.error)
            return result
        if initial_stdin is not None and not isinstance(initial_stdin, str):
            result.error = f"executor '{cli_name}' build_initial_stdin returned a non-string"
            if options.on_error:
                options.on_error(result.error)
            return result

    refusal = _posture_refusal(
        cli_name,
        bool(executor.get("supports_permission")),
        permission,
        _auto_approve_enabled(),
        argv,
    )
    if refusal:
        result.error = refusal
        if options.on_error:
            options.on_error(refusal)
        return result

    import shutil
    if not shutil.which(argv[0]):
        hint = executor.get("install_hint", "")
        result.error = f"executor '{cli_name}': command not found: {argv[0]}. {hint}".strip()
        if options.on_error:
            options.on_error(result.error)
        return result

    plain = executor.get("plain", False)

    # 独立进程组：超时 killpg 时把 agent CLI 的全部子孙（bash 工具调用等）一起清掉，
    # 否则只杀直接子进程，agent 的子树会变成孤儿继续持有凭据运行（2026-09-27 实测事故）。
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if initial_stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            cwd=cwd or None,
            env=env,
        )
    except FileNotFoundError:
        hint = executor.get("install_hint", "")
        result.error = f"executor '{cli_name}': command not found: {argv[0]}. {hint}".strip()
        if options.on_error:
            options.on_error(result.error)
        return result

    if options.run_lock_key:
        # 遗言锁：worker 硬死时接管者据此定位并清理孤儿进程组。早退路径不
        # 统一清理是自愈安全的——能走到早退说明子进程已死（communicate 返回
        # 或已 killpg），残留锁下次 preflight 按 stale 清（run_lock docstring）。
        _run_lock.write_run_lock(options.run_lock_key, proc.pid, argv)

    # Streaming IO model (mirrors Node runViaExecutor observable behavior):
    # stdout is read line by line in real time (on_partial fires as each line
    # arrives, not at EOF); stderr is drained by a daemon thread so the pipe
    # can never fill and deadlock; on timeout we kill the process group and
    # drain whatever lines were already produced (salvage), so output is
    # never silently discarded.
    # streaming 默认读 profile（#29 的 profile 管道），否则聚合/剥离语义不生效。
    streaming = (
        options.streaming if options.streaming is not None else profile["streaming"]
    )

    # Set once the direct child has been reaped: every byte it wrote is then
    # already in the pipes, so the pumps can end at the "writer gone and pipe
    # drained" probe instead of blocking on a grandchild that inherited the
    # write end (see _iter_pipe_lines) — the same boundary as the spawn path.
    child_gone = threading.Event()
    stderr_parts: List[str] = []

    def _pump_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in _iter_pipe_lines(proc.stderr, child_gone.is_set):
                stderr_parts.append(line)
        except (ValueError, OSError):
            pass

    stderr_thread = threading.Thread(target=_pump_stderr, daemon=True)
    stderr_thread.start()

    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()

    def _pump_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in _iter_pipe_lines(proc.stdout, child_gone.is_set):
                line_queue.put(line)
        except (ValueError, OSError):
            pass
        finally:
            line_queue.put(None)  # end-of-turn marker

    stdout_thread = threading.Thread(target=_pump_stdout, daemon=True)
    stdout_thread.start()

    if initial_stdin is not None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(initial_stdin + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    # NDJSON state
    parse_event_fn = None
    if not plain:
        parse_event_fn = (
            handlers.get("parse_event") if isinstance(handlers, dict)
            else getattr(handlers, "parse_event", None)
        )
        if not callable(parse_event_fn):
            result.error = f"executor '{cli_name}' has no parse_event for NDJSON mode"
            if options.on_error:
                options.on_error(result.error)
            return result

    plain_lines: List[str] = []
    # Salved partials kept for the timeout / cancel fallback only — a normal
    # turn's reply is the first `result` text alone (#12; partials are never
    # concatenated onto it).
    reply_parts: List[str] = []
    final_text: Optional[str] = None
    result_seen = False
    partials_forwarded = False
    error_message: Optional[str] = None
    timed_out = False
    cancelled = False

    def _handle_line(raw: str) -> None:
        nonlocal final_text, result_seen, partials_forwarded, error_message
        line = raw.rstrip("\r\n")
        if not line:
            return
        if plain:
            plain_lines.append(line)
            if options.on_protocol_line:
                options.on_protocol_line(line)
            return
        if options.on_protocol_line:
            options.on_protocol_line(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        parsed = parse_event_fn(event)
        if not isinstance(parsed, dict):
            return
        sid = parsed.get("session_id")
        if sid and is_valid_session_id(sid) and not result.session_id:
            result.session_id = sid
            if options.on_session:
                options.on_session(sid)
        # `usage` may travel on any event, including an `error` event (#12).
        usage = parsed.get("usage")
        if usage and result.usage is None:
            result.usage = usage
        err = parsed.get("error")
        if err:
            # First error wins; `sessionId` may still be learned after it.
            if error_message is None:
                error_message = err
            return
        if error_message is not None:
            # post-error events (partial/result) are suppressed
            return
        partial = parsed.get("partial_text")
        if partial:
            if streaming and options.on_partial:
                # `ParseResult` has no `role`, so the second argument is None.
                _emit_partial(options.on_partial, partial, None, partial_arity)
                partials_forwarded = True
            if not partials_forwarded:
                reply_parts.append(partial)
        final = parsed.get("final_text")
        if final is not None and not result_seen:
            # First `result` event wins — an explicit '' counts; later result
            # events are ignored ("result: at most one").
            result_seen = True
            final_text = final

    deadline = (
        time.monotonic() + timeout_secs
        if timeout_secs and timeout_secs > 0 else None
    )
    eof = False
    # Bounded window for the pumps to reach the probe boundary after the direct
    # child is reaped; a straggler that keeps the pipe busy must not hold the
    # turn open once the child is gone (same 1 s bound as the spawn path's join
    # backstop and Node's DRAIN_GRACE_MS / Rust's DRAIN_GRACE).
    drain_deadline: Optional[float] = None
    while not eof:
        # 协作式取消：每轮轮询 cancel_event，命中即分级击杀进程组（与超时
        # 同构但语义不同——取消是控制面意图，宿主据 cancelled 判非错误）。
        if options.cancel_event is not None and options.cancel_event.is_set():
            cancelled = True
            _signal_process_group(proc, signal.SIGTERM)
            grace_deadline = time.monotonic() + kill_grace_secs
            while not eof and time.monotonic() < grace_deadline:
                try:
                    item = line_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    eof = True
                    break
                _handle_line(item)
            if not eof:
                _kill_process_group(proc, on_warning=options.on_stderr)
                # 子进程已回收：泵线程改在“写端已去 + 管道已空”的探针边界
                # 结束，不再等继承写端的孙进程（与 spawn 路径同构）。
                child_gone.set()
                salvage_deadline = time.monotonic() + 2.0
                while time.monotonic() < salvage_deadline:
                    try:
                        item = line_queue.get(timeout=0.2)
                    except queue.Empty:
                        if not stdout_thread.is_alive():
                            break
                        continue
                    if item is None:
                        eof = True
                        break
                    _handle_line(item)
            child_gone.set()
            break
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                # 与 spawn 路径一致的三段式：SIGTERM（polite）→ kill_grace_secs
                # 宽限 → killpg SIGKILL 清整个子树。宽限内继续消费排队行；
                # SIGKILL 后仍未回收时经 on_warning 告警（issue #20）。
                _signal_process_group(proc, signal.SIGTERM)
                grace_deadline = time.monotonic() + kill_grace_secs
                while not eof and time.monotonic() < grace_deadline:
                    try:
                        item = line_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if item is None:
                        eof = True
                        break
                    _handle_line(item)
                if not eof:
                    _kill_process_group(proc, on_warning=options.on_stderr)
                    # 子进程已回收：泵线程改在“写端已去 + 管道已空”的探针
                    # 边界结束，不再等继承写端的孙进程（与 spawn 路径同构）。
                    child_gone.set()
                    # Salvage: drain lines the child already wrote (its pipe
                    # buffer + anything the pump thread still holds).
                    salvage_deadline = time.monotonic() + 2.0
                    while time.monotonic() < salvage_deadline:
                        try:
                            item = line_queue.get(timeout=0.2)
                        except queue.Empty:
                            if not stdout_thread.is_alive():
                                break
                            continue
                        if item is None:
                            eof = True
                            break
                        _handle_line(item)
                child_gone.set()
                break
        # Direct-child reap detection — the no-timeout case aside, this is the
        # moment the child exits on its own. The pumps can now stop at the
        # probe boundary instead of waiting for a pipe EOF a surviving
        # grandchild holds open; the drain window below bounds the wait when a
        # straggler keeps the pipe busy.
        if not child_gone.is_set() and proc.poll() is not None:
            child_gone.set()
            drain_deadline = time.monotonic() + 1.0
        if drain_deadline is not None and time.monotonic() >= drain_deadline:
            if options.on_stderr:
                options.on_stderr(
                    "[agentproc runner] warning: agent exited but its "
                    "stdout/stderr pipe is still open (held by a surviving "
                    "grandchild); abandoning the drain"
                )
            break
        try:
            item = line_queue.get(timeout=min(0.5, remaining) if remaining else 0.5)
        except queue.Empty:
            continue
        if item is None:
            eof = True
            break
        _handle_line(item)

    exit_code: int
    if cancelled:
        try:
            exit_code = _normalise_exit_code(proc.wait(timeout=5))
        except subprocess.TimeoutExpired:
            exit_code = EXIT_CANCELLED
        else:
            # 子进程自主退出（宽限内收尾）——仍归类为取消，语义由 cancelled 决定
            exit_code = EXIT_CANCELLED
    elif timed_out:
        try:
            exit_code = _normalise_exit_code(proc.wait(timeout=5))
        except subprocess.TimeoutExpired:
            exit_code = EXIT_TIMEOUT
    else:
        exit_code = _normalise_exit_code(proc.wait())

    # The direct child is gone (reaped, or the kill path gave up): its output
    # is all in the pipes, so the pumps finish at the probe boundary instead of
    # waiting for a grandchild that inherited the write end. The 1 s join is a
    # backstop for a straggler that keeps writing; the read ends are then
    # released either way so a straggler cannot leak an fd per turn.
    child_gone.set()
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    for th, stream in ((stdout_thread, proc.stdout), (stderr_thread, proc.stderr)):
        if th.is_alive() and options.on_stderr:
            options.on_stderr(
                f"[agentproc runner] warning: {th.name} still alive; "
                "closing pipe read end to unblock"
            )
        try:
            if stream is not None and not stream.closed:
                stream.close()
        except OSError:
            pass

    # Reply assembly (spec protocol.md: result.text may be '' when the body
    # was already delivered via partials — partials are never concatenated
    # onto the final text). Mirrors the spawn path / Node runner.js:700-702:
    # with partials forwarded the non-timeout reply stays ''; streaming=false
    # aggregates partials + final because nothing was forwarded to callers.
    if plain:
        result.reply = "\n".join(plain_lines).strip()
    elif cancelled:
        # 取消 salvage：保留已产出的半轮文本（与超时同待遇，调用方不丢内容）。
        result.reply = final_text if final_text is not None else "".join(reply_parts)
    elif timed_out:
        # Timeout salvage: keep the half-turn text so the caller does not
        # lose everything produced before the kill (acceptance: reply != '').
        result.reply = final_text if final_text is not None else "".join(reply_parts)
    elif streaming and partials_forwarded:
        result.reply = ""
    elif final_text is not None:
        # First `result` event wins (an explicit '' counts). With
        # streaming=false partials are never folded into the reply (#12).
        result.reply = final_text

    stderr_text = "".join(stderr_parts).strip()

    if options.run_lock_key and not timed_out and not cancelled:
        _run_lock.clear_run_lock(options.run_lock_key)

    if cancelled:
        # 协作式取消：控制面意图，非错误。timed_out 保持 False；exit_code 用
        # EXIT_CANCELLED 让 plaita 侧判定 cancelled 而非 error。
        result.exit_code = EXIT_CANCELLED
        result.error = f"executor '{cli_name}' cancelled"
        if options.on_error:
            options.on_error(result.error)
        return result

    if timed_out:
        result.timed_out = True
        result.error = f"executor '{cli_name}' timed out after {timeout_secs}s"
        result.exit_code = EXIT_TIMEOUT
        if options.on_error:
            options.on_error(result.error)
        return result

    if error_message is not None:
        result.error = error_message
        result.exit_code = exit_code if exit_code != 0 else EXIT_ERROR
        if options.on_error:
            options.on_error(result.error)
        return result

    if exit_code != 0:
        result.error = (
            f"executor '{cli_name}' exited {exit_code}: "
            + (stderr_text[:500] or "(no stderr)")
        )
        result.exit_code = exit_code
        if options.on_error:
            options.on_error(result.error)
        return result

    if plain:
        if not result.reply:
            result.error = f"{cli_name} returned empty output"
            result.exit_code = EXIT_ERROR
            if options.on_error:
                options.on_error(result.error)
            return result
        # Plain executors that manage a session id expose get_session_id() on
        # their handlers so the runner can surface it in RunResult.session_id.
        get_session_id_fn = (
            handlers.get("get_session_id") if isinstance(handlers, dict)
            else getattr(handlers, "get_session_id", None)
        )
        if callable(get_session_id_fn):
            sid = get_session_id_fn()
            if is_valid_session_id(sid) and not result.session_id:
                result.session_id = sid
                if options.on_session:
                    options.on_session(sid)
        result.exit_code = EXIT_SUCCESS
        return result

    # An NDJSON turn that exited 0 with nothing on stdout is a SUCCESS
    # (spec protocol.md:396; scenarios.json "empty output → empty reply,
    # success") — reply stays ''.
    result.exit_code = EXIT_SUCCESS
    return result


# run() — the main entry point
# ---------------------------------------------------------------------------

def run(profile_raw: Dict[str, Any], options: RunOptions) -> RunResult:
    """Run an agent process per the AgentProc spec."""
    profile = normalize_profile(profile_raw)

    # Executor path: skip the subprocess bridge entirely.
    # Four cases (mirrors Node runner.js):
    #  (1) no executor: fall through to spawn path
    #  (2) executor present + SDK knows it: call run_via_executor, skip spawn
    #  (3) executor present + SDK unknown + command present: warn, fall back to spawn
    #  (4) executor present + SDK unknown + no command: hard fail
    executor_name = profile.get("executor")
    if executor_name:
        executor = EXECUTORS.get(executor_name)
        if executor is None:
            if not profile.get("command"):
                result = RunResult(exit_code=EXIT_ERROR)
                result.error = (
                    f"Unknown executor '{executor_name}'. "
                    f"Available: {', '.join(executor_names)}"
                )
                if options.on_error:
                    options.on_error(result.error)
                return result
            if options.on_stderr:
                options.on_stderr(
                    f"[agentproc runner] unknown executor {executor_name!r}; "
                    f"falling back to spawn (command: {profile['command']!r})"
                )
        else:
            return run_via_executor(executor, options, profile)

    streaming = (
        options.streaming if options.streaming is not None else profile["streaming"]
    )
    timeout_secs = (
        options.timeout_secs if options.timeout_secs is not None else profile["timeout_secs"]
    )
    # Inspected once per turn, not per event: a one-argument callback (the
    # documented ``lambda chunk: ...`` form) keeps working unchanged.
    partial_arity = _partial_arity(options.on_partial)

    # Event traceability: bridge-measured turn timing + opt-in journal hook.
    started_wall = time.time()
    started_mono = time.monotonic()
    seq = 0
    seq_lock = threading.Lock()

    def _journal(event: Dict[str, Any]) -> None:
        if options.on_journal is None:
            return
        options.on_journal(dict(event, ts=_utc_now_iso()))

    def _next_seq() -> int:
        nonlocal seq
        with seq_lock:
            seq += 1
            return seq

    # Time budget / absolute deadline (spec "Time budget and absolute
    # deadline"): earliest expiry of timeout_secs / budget_secs / deadline.
    effective_secs: Optional[float] = (
        float(timeout_secs) if timeout_secs and timeout_secs > 0 else None
    )
    budget = profile.get("budget_secs")
    if budget is not None and budget > 0:
        effective_secs = budget if effective_secs is None else min(effective_secs, budget)
    deadline_epoch = parse_deadline(profile.get("deadline"))
    if deadline_epoch is not None:
        budget_from_deadline = deadline_epoch - started_wall
        effective_secs = (
            budget_from_deadline
            if effective_secs is None
            else min(effective_secs, budget_from_deadline)
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

    env = _compose_env(profile, options, subst_ctx)

    # Build the turn object (wire 0.4 stdin payload). No AGENT_* env in 0.4.
    turn: Dict[str, Any] = {
        "type": "turn",
        "message": options.message,
        "session_id": options.session_id,
        "session_name": options.session_name,
        "protocol_version": PROTOCOL_VERSION,
    }
    # attachments: include the key when the caller provided a list
    # (presence-as-feature); omit otherwise.
    if options.attachments:
        turn["attachments"] = options.attachments
    if profile["permission"]:
        turn["permission"] = True

    # Same clock source as started_mono: started_at + duration must not drift.
    result = RunResult(
        started_at=datetime.fromtimestamp(started_wall, timezone.utc).isoformat(
            timespec="milliseconds"
        )
    )
    result_text: Optional[str] = None
    result_seen = False
    # Spec: once an error event arrives, subsequent partial/result events
    # MUST be discarded (they cannot contribute to a failed turn's reply).
    # session_id on events is still noted (first-non-empty), including on
    # the error event itself and on post-error partials.
    error_seen = False
    # True once at least one partial was forwarded under streaming — then
    # result.text must not become the runner reply (body already streamed).
    partials_forwarded = False
    pending_permission_ids: set = set()
    stdin_lock = threading.Lock()
    stdin_closed = False
    # Set once proc.wait() has returned: the child is reaped, so every byte it
    # wrote is already in the pipes. Drain threads use it to end the turn at
    # "writer gone and pipe drained" instead of blocking on a grandchild that
    # inherited the write end (see _iter_pipe_lines).
    child_gone = threading.Event()
    # Turn epoch guard: set once the drain phase is over (the drain threads
    # stopped, or the join backstop below gave up on them and closed their
    # pipes). A straggler line picked up from here on is dropped instead of
    # firing callbacks after run() returned.
    turn_finished = False
    # Set once the post-join backstop below force-closes the pipe read-ends.
    # A read failure after that close (ValueError / OSError, incl. EBADF) is
    # the forced unblock, not a real drain failure: on Linux closing a pipe
    # being read by another thread raises instead of returning EOF cleanly,
    # and a healthy turn whose pipes are held by a grandchild would otherwise
    # be misreported as result.error + on_error.
    pipes_closed = False
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

    def _note_session_id(sid: Any) -> None:
        """Persist the first non-empty valid session_id; warn on conflict/invalid."""
        if turn_finished:
            return
        if not isinstance(sid, str) or not sid:
            return
        if not is_valid_session_id(sid):
            if options.on_stderr:
                options.on_stderr(
                    f"[agentproc runner] ignoring invalid session id "
                    f"{sid!r} (must be non-empty, no path separators "
                    "or control chars); previous session id preserved"
                )
            return
        if not result.session_id:
            result.session_id = sid
            if options.on_session:
                options.on_session(sid)
            return
        if sid != result.session_id and options.on_stderr:
            options.on_stderr(
                f"[agentproc runner] ignoring conflicting session_id {sid!r}; "
                f"keeping first {result.session_id!r}"
            )

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,  # 独立进程组：超时 killpg 清整个子树
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
        result.duration = time.monotonic() - started_mono
        return result
    except PermissionError as e:
        if options.on_stderr:
            options.on_stderr(f"[agentproc runner] spawn error: {e}")
        if options.on_error:
            options.on_error(f"failed to start agent: {e}")
        if not result.error:
            result.error = str(e)
        result.exit_code = EXIT_ERROR
        result.duration = time.monotonic() - started_mono
        return result

    if options.run_lock_key:
        # 遗言锁（同 in-process 路径）：早退不清理是自愈安全的，见 run_lock。
        _run_lock.write_run_lock(options.run_lock_key, proc.pid, argv)

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

    # Shared drain-failure slot: a reader-thread exception must reach the
    # main loop (which owns on_error / kill) instead of dying silently and
    # leaving the turn to be misreported as a timeout.
    drain_error: List[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in _iter_pipe_lines(proc.stderr, child_gone.is_set):
                if turn_finished:
                    return
                _append_stderr(line)
                line = line.rstrip("\r\n")
                if options.on_stderr:
                    options.on_stderr(line)
        except Exception as exc:  # noqa: BLE001
            if pipes_closed and isinstance(exc, (ValueError, OSError)):
                # Forced close of the read end (post-join backstop): normal exit.
                return
            drain_error.append(f"stderr reader failed: {exc!r}")

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout is not None

    def _handle_line(raw_line: str) -> None:
        nonlocal error_seen
        nonlocal result_text, result_seen, partials_forwarded
        if turn_finished:
            return
        line = raw_line.rstrip("\r")
        c = classify_line(line)
        kind = c["kind"]
        # Event traceability: journal classified events with seq/ts. seq/ts
        # stay bridge-internal — they never enter the wire output.
        if kind != "malformed" and options.on_journal is not None:
            _journal({"seq": _next_seq(), "kind": kind, "line": line[:2000]})
        if kind == "partial":
            _note_session_id(c.get("session_id"))
            # Spec: post-error partials are discarded (not forwarded).
            # on_protocol_line still fires so debug traces stay complete.
            if not error_seen and streaming and options.on_partial:
                _emit_partial(
                    options.on_partial, c["value"], c.get("role"), partial_arity
                )
                partials_forwarded = True
            if options.on_protocol_line:
                options.on_protocol_line(line)
        elif kind == "result":
            # At most one result; post-error result discarded.
            if error_seen:
                # Still capture usage if not already set.
                if c.get("usage") and result.usage is None:
                    result.usage = c["usage"]
                if options.on_protocol_line:
                    options.on_protocol_line(line)
            elif result_seen:
                if options.on_stderr:
                    options.on_stderr(
                        '[agentproc runner] ignoring extra {"type":"result"} '
                        "(at most one per turn)"
                    )
                if options.on_protocol_line:
                    options.on_protocol_line(line)
            else:
                result_seen = True
                _note_session_id(c.get("session_id"))
                result_text = c["value"]
                if c.get("usage"):
                    result.usage = c["usage"]
                if options.on_protocol_line:
                    options.on_protocol_line(line)
        elif kind == "error":
            _note_session_id(c.get("session_id"))
            result.error = c["value"]
            error_seen = True
            if c.get("usage"):
                result.usage = c["usage"]
            if options.on_error:
                options.on_error(c["value"])
            if options.on_protocol_line:
                options.on_protocol_line(line)
            pending_permission_ids.clear()
        elif kind == "permission_request":
            if isinstance(c["value"], dict):
                _note_session_id(c["value"].get("session_id"))
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
            # malformed: log + ignore (not forwarded as body in 0.4).
            # Includes legacy {"type":"session"} / {"type":"text"}.
            if options.on_stderr:
                options.on_stderr(
                    f"[agentproc runner] ignoring malformed stdout line: {line[:200]!r}"
                )
            if options.on_protocol_line:
                options.on_protocol_line(line)

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        try:
            for raw_line in _iter_pipe_lines(proc.stdout, child_gone.is_set):
                if turn_finished:
                    return
                _handle_line(raw_line.rstrip("\n"))
        except Exception as exc:  # noqa: BLE001
            if pipes_closed and isinstance(exc, (ValueError, OSError)):
                # Forced close of the read end (post-join backstop): normal exit.
                return
            drain_error.append(f"stdout reader failed: {exc!r}")

    stdout_thread = threading.Thread(target=_drain_stdout, daemon=True)
    stdout_thread.start()

    exit_code: int
    timed_out = False
    cancelled = False
    try:
        if effective_secs is not None:
            deadline = started_mono + effective_secs
            while True:
                try:
                    exit_code = _normalise_exit_code(proc.wait(timeout=0.5))
                    break
                except subprocess.TimeoutExpired:
                    # 协作式取消（早于超时判定）：分级击杀进程组，标记 cancelled。
                    if (not cancelled and options.cancel_event is not None
                            and options.cancel_event.is_set()):
                        cancelled = True
                        for rid in list(pending_permission_ids):
                            _write_permission_response({
                                "request_id": rid,
                                "behavior": "deny",
                                "message": "cancelled",
                            })
                        _close_stdin()
                        _signal_process_group(proc, signal.SIGTERM)
                        try:
                            proc.wait(timeout=profile["kill_grace_secs"])
                        except subprocess.TimeoutExpired:
                            _signal_process_group(proc, signal.SIGKILL)
                        try:
                            exit_code = proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            exit_code = EXIT_CANCELLED
                        else:
                            exit_code = EXIT_CANCELLED
                        break
                    if drain_error:
                        # Drain-thread failure: surface it, kill the group,
                        # and end the turn with EXIT_ERROR (not a timeout).
                        _close_stdin()
                        _signal_process_group(proc, signal.SIGTERM)
                        try:
                            proc.wait(timeout=profile["kill_grace_secs"])
                        except subprocess.TimeoutExpired:
                            _signal_process_group(proc, signal.SIGKILL)
                        try:
                            exit_code = proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            exit_code = EXIT_ERROR
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        _journal({
                            "seq": _next_seq(),
                            "decision": "timeout",
                            "budget_secs": effective_secs,
                        })
                        # Spec: prefer deny with timeout message for pending
                        # permission requests, then kill.
                        for rid in list(pending_permission_ids):
                            _write_permission_response({
                                "request_id": rid,
                                "behavior": "deny",
                                "message": "permission timed out",
                            })
                        _close_stdin()
                        # 先对进程组 SIGTERM（polite shutdown），宽限后 killpg SIGKILL——
                        # 只 terminate 直接子进程会漏掉 agent 的子树（2026-09-27 事故）。
                        _signal_process_group(proc, signal.SIGTERM)
                        _journal({
                            "seq": _next_seq(),
                            "decision": "sigterm_process_group",
                        })
                        try:
                            proc.wait(timeout=profile["kill_grace_secs"])
                        except subprocess.TimeoutExpired:
                            _signal_process_group(proc, signal.SIGKILL)
                            _journal({
                                "seq": _next_seq(),
                                "decision": "sigkill_process_group",
                            })
                        try:
                            exit_code = proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            exit_code = EXIT_TIMEOUT
                            # Reap attempt + structured warning if the direct
                            # child is somehow unkillable/unwaitable.
                            try:
                                proc.wait(timeout=1)
                            except (subprocess.TimeoutExpired, ValueError, OSError):
                                if options.on_stderr:
                                    options.on_stderr(
                                        f"[agentproc runner] warning: direct child "
                                        f"pid={proc.pid} unreaped after timeout kill"
                                    )
                        break
        else:
            exit_code = _normalise_exit_code(proc.wait())
    except KeyboardInterrupt:
        # Ctrl-C must clear the whole agent subtree: a surviving grandchild
        # keeps the agent's credentials and the workspace write access, and the
        # run lock is cleared right after this — which would make the orphan
        # invisible to the next preflight's stale-run sweep (issue #8).
        if hasattr(signal, "SIGINT"):
            _signal_process_group(proc, signal.SIGINT)
        else:
            try:
                proc.terminate()
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.wait(timeout=profile["kill_grace_secs"])
        except subprocess.TimeoutExpired:
            _kill_process_group(proc, on_warning=options.on_stderr)
        exit_code = _normalise_exit_code(proc.wait())

    _close_stdin()
    # The subprocess is gone: its output is all in the pipes, so the drain
    # threads can read the rest of the turn and stop without waiting for a
    # grandchild that inherited a pipe write-end (see _iter_pipe_lines).
    child_gone.set()
    # Drain threads now finish within one poll interval of the child's exit.
    # The hard timeout here is a backstop for a straggler that keeps writing
    # (the pipe keeps them busy) — it keeps the runner from hanging
    # indefinitely without letting drain latency balloon past the
    # kill_grace_secs budget. 1s each = at most ~2s of extra latency;
    # diagnosis may be incomplete if it fires, but the post-mortem stderr
    # patterns target interpreter-startup errors that land in the first bytes
    # of stderr anyway, well within the 1MB head capture.
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    # Turn epoch ends here: whatever a straggler-driven drain thread still
    # picks up must not fire consumer callbacks for this finished turn.
    turn_finished = True
    # Set before closing: a drain thread unblocked by the close must already
    # observe the flag when it handles the resulting ValueError/OSError.
    pipes_closed = True
    # Both read ends are released before returning whether or not the threads
    # got there by themselves: a pipe kept open by a straggler must not leak
    # an fd per turn (issue #20).
    for th, stream in ((stdout_thread, proc.stdout), (stderr_thread, proc.stderr)):
        if th.is_alive() and options.on_stderr:
            options.on_stderr(
                f"[agentproc runner] warning: {th.name} still alive; "
                "closing pipe read end to unblock"
            )
        try:
            if stream is not None and not stream.closed:
                stream.close()
        except OSError:
            pass

    if drain_error:
        result.error = drain_error[0]
        if options.on_error:
            options.on_error(result.error)

    # Reply body assembly (wire 0.4):
    # - streaming true + any partial forwarded → reply stays empty (body via
    #   on_partial; do not duplicate result.text)
    # - otherwise, if a result was seen → reply = result.text
    # - streaming false → partials never forward, so reply = result.text
    if streaming and partials_forwarded:
        result.reply = ""
    elif result_text is not None:
        result.reply = result_text
    else:
        result.reply = ""

    # If the agent exited non-zero with no error event, peek at its stderr for
    # common "command/file not found" patterns and surface a friendly hint.
    # Uses the head-capped stderr_full (1 MB) — the interpreter-startup errors
    # these patterns target land in the first bytes, well within the cap.
    if not timed_out and not cancelled and not result.error and exit_code != 0:
        stderr_text = "".join(stderr_full)
        hint = diagnose_stderr_failure(stderr_text)
        if hint:
            result.error = hint
            if options.on_error:
                options.on_error(hint)

    if cancelled:
        # 协作式取消：控制面意图，非错误（与 executor 路径同语义）。
        result.exit_code = EXIT_CANCELLED
        if not result.error:
            result.error = "cancelled"
    elif timed_out:
        result.timed_out = True
        result.exit_code = EXIT_TIMEOUT
    elif result.error:
        result.exit_code = EXIT_ERROR if exit_code == 0 else exit_code
    else:
        result.exit_code = exit_code

    result.duration = time.monotonic() - started_mono
    if options.run_lock_key and not cancelled:
        _run_lock.clear_run_lock(options.run_lock_key)
    return result
