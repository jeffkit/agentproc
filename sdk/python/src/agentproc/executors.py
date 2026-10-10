"""Built-in executor registry for the AgentProc Python SDK.

An executor is a named, in-process implementation of the bridge side of the
AgentProc protocol.  Instead of spawning a bridge subprocess (which then forks
the target CLI), the runner calls the executor directly — eliminating the
bridge-process fork overhead while reusing the same build_args + parse_event
logic that the standalone bridge scripts use.

Each executor is a dict (or object with the same keys) containing:

    cli_name:     str   — CLI binary name (for error messages)
    install_hint: str   — how to install the CLI
    plain:        bool  — True = CLI emits plain text (not NDJSON);
                          False (default) = CLI emits NDJSON, use parse_event
    build_args:   (message: str, session_id: str, env: dict, ctx: dict | None)
                  -> list[str]
    supports_permission: bool — True only when the executor has a mid-turn
                  approval channel (default False). The runner refuses to run
                  a profile with `permission: true` against an executor that
                  does not declare it.
    build_initial_stdin: (message: str, session_id: str) -> str | None
                  Optional. Called once per turn before spawn. A returned
                  string makes the runner pipe the CLI's stdin, write it
                  followed by "\\n", and close it — the way an executor keeps
                  the user message out of argv. Absent / None leaves the CLI's
                  stdin on the null device.
    parse_event:  (event: dict) -> ParseResult | None
                  (omitted / irrelevant when plain: True)
    make_handlers: () -> {"build_args": ..., "parse_event"?: ..., "get_session_id"?: ...,
                          "build_initial_stdin"?: ...}
                  — optional factory for stateful executors (e.g. kimi-code)
                  that need fresh per-turn state shared between build_args and
                  parse_event.  When present, the runner calls make_handlers()
                  once per turn; the returned dict is used for that turn only —
                  build_initial_stdin included: the runner reads it off the
                  factory result when the factory is used, else off the
                  executor itself.
                  For plain executors that generate or reuse a session id in
                  build_args, make_handlers may expose a get_session_id()
                  callable.  The runner calls get_session_id() after the process
                  exits to populate RunResult.session_id.
                  Executors without make_handlers use build_args / parse_event
                  directly (they must be stateless / re-entrant).

ParseResult shape:
    {
        "partial_text":  str | None,   — streaming chunk
        "final_text":    str | None,   — terminal reply body
        "session_id":    str | None,   — session id to persist
        "error":         str | None,   — error message (turn fails)
        "usage":         dict | None,  — token/cost stats
    }
"""

from __future__ import annotations

import json
import shutil
import uuid
from typing import Any, Callable, Dict, List, Optional

__all__ = ["EXECUTORS", "executor_names"]


# ---------------------------------------------------------------------------
# claude-code
# ---------------------------------------------------------------------------

def _claude_code_build_args(
    message: str,
    session_id: str,
    env: Dict[str, str],
    ctx: Optional[Dict[str, Any]] = None,
) -> List[str]:
    disallow = env.get("CLAUDE_DISALLOW_TOOLS", "AskUserQuestion").strip()
    model = env.get("CLAUDE_MODEL", "").strip()
    if ctx and ctx.get("permission"):
        # Bidirectional stream-json + stdio permission tool. The user message
        # is delivered via stdin, not argv.
        args = [
            "claude", "--print",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-prompt-tool", "stdio",
            "--permission-mode", "default",
        ]
        if disallow:
            args += ["--disallowed-tools", disallow]
        if model:
            args += ["--model", model]
        if session_id:
            args += ["--resume", session_id]
        return args
    # Unattended: same stream-json input channel as the permission mode, so
    # the message never lands in argv (see `_claude_code_build_initial_stdin`).
    args = [
        "claude", "--print",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        # claude CLI 硬要求：--print + stream-json 必须配 --verbose（rust SDK 已修，此处补齐）
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if disallow:
        args += ["--disallowed-tools", disallow]
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    return args


def _claude_code_build_initial_stdin(message: str, session_id: str) -> str:
    """The stream-json user turn Claude reads from stdin in both modes.

    The same frame the Node and Rust SDKs build (see `ClaudeCodeTurn`'s
    `build_initial_stdin`) — JSON object order is not significant to the CLI,
    the fields are.
    """
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": message},
            "parent_tool_use_id": None,
            "session_id": session_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _claude_code_parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            return {"session_id": sid}
        return None
    if etype == "assistant":
        text = "".join(
            b.get("text", "") for b in (event.get("message") or {}).get("content", [])
            if b.get("type") == "text"
        )
        return {"partial_text": text} if text else None
    if etype == "result":
        sid = event.get("session_id")
        if event.get("is_error"):
            return {"session_id": sid, "error": event.get("result") or "claude reported an error"}
        return {"session_id": sid, "final_text": event.get("result") or None}
    return None


CLAUDE_CODE = {
    "cli_name": "claude",
    "install_hint": "Install: npm install -g @anthropic-ai/claude-code",
    "plain": False,
    "supports_permission": True,
    "build_args": _claude_code_build_args,
    "build_initial_stdin": _claude_code_build_initial_stdin,
    "parse_event": _claude_code_parse_event,
}

# ---------------------------------------------------------------------------
# codebuddy
# ---------------------------------------------------------------------------

def _codebuddy_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    args = [
        "codebuddy", "-p", message,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    disallow = env.get("CODEBUDDY_DISALLOW_TOOLS", "AskUserQuestion").strip()
    if disallow:
        args += ["--disallowedTools", disallow]
    model = env.get("CODEBUDDY_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["-r", session_id]
    return args


def _codebuddy_parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    etype = event.get("type")
    if etype == "assistant":
        text = "".join(
            b.get("text", "") for b in (event.get("message") or {}).get("content", [])
            if b.get("type") == "text"
        )
        return {"partial_text": text} if text else None
    if etype == "result":
        sid = event.get("session_id")
        if event.get("is_error"):
            return {"session_id": sid, "error": event.get("result") or "codebuddy reported an error"}
        return {"session_id": sid, "final_text": event.get("result") or None}
    return None


CODEBUDDY = {
    "cli_name": "codebuddy",
    "install_hint": "See your internal CodeBuddy installation docs.",
    "plain": False,
    "build_args": _codebuddy_build_args,
    "parse_event": _codebuddy_parse_event,
}

# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------

def _codex_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    model = env.get("CODEX_MODEL", "").strip()
    # "--" keeps a message that starts with "-" a positional, not a flag; every
    # flag must therefore precede it.
    if session_id:
        args = ["codex", "exec", "resume", "--json", session_id]
        if model:
            args += ["-c", f'model="{model}"']
        args += ["--", message]
        return args
    args = ["codex", "exec", "--json"]
    if model:
        args += ["-c", f'model="{model}"']
    args += ["--", message]
    return args


def _codex_parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    etype = event.get("type")
    if etype == "thread.started":
        return {"session_id": event.get("thread_id")}
    if etype == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            text = item.get("text", "")
            return {"partial_text": text} if text else None
        return None
    if etype == "turn.failed":
        return {"error": str(event.get("error") or "codex turn failed")}
    return None


CODEX = {
    "cli_name": "codex",
    "install_hint": "Install: npm install -g @openai/codex",
    "plain": False,
    "build_args": _codex_build_args,
    "parse_event": _codex_parse_event,
}

# ---------------------------------------------------------------------------
# cursor
# ---------------------------------------------------------------------------
# cursor emits a duplicate full-text assistant event at the end of a streamed
# turn; parse_event must track accumulated text per-turn to suppress it.
# build_args is stateless, so only parse_event uses per-turn factory state.

def _make_cursor_handlers() -> Dict[str, Any]:
    accumulated: List[str] = []

    def _resolve_cursor_cli(env: Dict[str, str]) -> str:
        """择优解析 cursor-agent 的可执行名。

        为什么不能直接用裸 ``agent``（2026-10-10 生产实证）：
        cursor-agent 官方安装后同时提供 ``cursor-agent`` 与 ``agent`` 两个
        入口，而 ``agent`` 是**泛化名**——本机 ``~/.grok/bin/agent``（grok
        CLI）同名且常在 PATH 更前位，``shutil.which("agent")`` 会命中 grok，
        报出与真实原因无关的错误：

            error: unexpected argument '--stream-partial-output' found

        这在 14 个 executor 里是**唯一**用泛化名的（其余均为专属名）。
        优先用专属名 ``cursor-agent``；仅当它不存在时才回退 ``agent``
        （保持对只装了官方 ``agent`` 入口的环境兼容）。可用
        ``CURSOR_CLI`` 显式指定覆盖。
        """
        override = (env.get("CURSOR_CLI") or "").strip()
        if override:
            return override
        for name in ("cursor-agent", "agent"):
            if shutil.which(name):
                return name
        return "cursor-agent"        # 都没有 → 交 runner 报 command not found

    def build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
        args = [
            _resolve_cursor_cli(env), "-p", message,
            "--output-format", "stream-json",
            "--stream-partial-output",
        ]
        # `--trust`：cursor-agent 对**未信任目录**先弹交互式
        # 「⚠ Workspace Trust Required」；非交互（-p）下拿不到答复，
        # 表现为**模型退化为受限列表**——实测报
        # `Cannot use this model: claude-4.6-sonnet-medium. Available models:
        # auto, composer-2.5, cursor-grok-4.5-high, …`，是**误导性报错**，
        # 真因是目录未信任（2026-10-10 实测：全新目录直接跑弹 trust 提示；
        # 同目录加 `--trust` 立即正常，日志显示 `model: Claude Sonnet 4.6 1M`）。
        # 默认信任（与 `--yolo` 同档：非交互执行本就要求无人值守），
        # 可用 `CURSOR_TRUST=0` 关闭。
        if (env.get("CURSOR_TRUST") or "1") == "1":
            args.append("--trust")
        if (env.get("CURSOR_FORCE") or "1") == "1":
            args.append("--yolo")
        model = env.get("CURSOR_MODEL", "").strip()
        if model:
            args += ["--model", model]
        if session_id:
            args += ["--resume", session_id]
        return args

    def parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            return {"session_id": event.get("session_id")}
        if etype == "assistant":
            msg = event.get("message") or {}
            text = "".join(
                b.get("text", "") for b in msg.get("content", [])
                if b.get("type") == "text"
            )
            if not text:
                return None
            if text == "".join(accumulated):
                return None
            accumulated.append(text)
            return {"partial_text": text}
        if etype == "result":
            sid = event.get("session_id")
            if event.get("is_error") or event.get("subtype") == "error":
                return {"session_id": sid, "error": event.get("result") or "cursor agent reported an error"}
            return {"session_id": sid, "final_text": event.get("result") or None}
        return None

    return {"build_args": build_args, "parse_event": parse_event}


CURSOR = {
    "cli_name": "agent",
    "install_hint": "Install: brew install cursor-agent  (then run `agent login`)",
    "plain": False,
    "make_handlers": _make_cursor_handlers,
}

# ---------------------------------------------------------------------------
# gemini-cli
# ---------------------------------------------------------------------------

def _gemini_cli_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    args = ["gemini", "-p", message, "--output-format", "stream-json", "--yolo"]
    if (env.get("GEMINI_SANDBOX") or "").strip().lower() == "false":
        args += ["--sandbox", "false"]
    model = env.get("GEMINI_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    return args


def _gemini_cli_parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    etype = event.get("type")
    if etype == "init":
        return {"session_id": event.get("session_id")}
    if etype == "message":
        if event.get("role") != "assistant":
            return None
        text = event.get("content", "")
        if not text:
            return None
        return {"partial_text": text} if event.get("delta") else {"final_text": text}
    if etype == "error":
        if event.get("severity") == "error":
            return {"error": event.get("message") or "gemini reported an error"}
        return None
    if etype == "result" and event.get("status") == "error":
        err = event.get("error") or {}
        return {"error": err.get("message") or "gemini turn failed"}
    return None


GEMINI_CLI = {
    "cli_name": "gemini",
    "install_hint": "Install: npm install -g @google/gemini-cli",
    "plain": False,
    "build_args": _gemini_cli_build_args,
    "parse_event": _gemini_cli_parse_event,
}

# ---------------------------------------------------------------------------
# kimi-code
# ---------------------------------------------------------------------------

def _make_kimi_code_handlers() -> Dict[str, Any]:
    session: Dict[str, Optional[str]] = {"id": None}

    def build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
        session["id"] = session_id or str(uuid.uuid4())
        args = [
            "kimi", "--print", "-p", message,
            "--output-format=stream-json",
            "--session", session["id"],
        ]
        model = env.get("KIMI_MODEL", "").strip()
        if model:
            args += ["--model", model]
        return args

    def parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if event.get("role") == "assistant":
            content = event.get("content", "")
            if content:
                return {"partial_text": content, "final_text": content, "session_id": session["id"]}
        return None

    return {"build_args": build_args, "parse_event": parse_event}


KIMI_CODE = {
    "cli_name": "kimi",
    "install_hint": "See https://moonshotai.github.io/kimi-cli for installation",
    "plain": False,
    "make_handlers": _make_kimi_code_handlers,
}

# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------

def _opencode_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    # Flags precede "--": everything after it is positional, so a message that
    # starts with "-" cannot be parsed as a flag.
    args = ["opencode", "run", "--auto", "--format", "json"]
    if session_id:
        args += ["--session", session_id]
    model = env.get("OPENCODE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    args += ["--", message]
    return args


def _opencode_parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    etype = event.get("type")
    sid = event.get("sessionID") or None
    part = event.get("part") or {}

    if etype == "text":
        text = part.get("text", "")
        if text:
            return {"session_id": sid, "partial_text": text}
        return {"session_id": sid} if sid else None
    if etype in ("step_start", "step_finish", "tool_use"):
        return {"session_id": sid} if sid else None
    if etype == "error":
        err = part.get("message") or (event.get("error") or {}).get("message") or "opencode reported an error"
        return {"session_id": sid, "error": err}
    return None


OPENCODE = {
    "cli_name": "opencode",
    "install_hint": "Install: npm install -g opencode-ai  (or: curl -fsSL https://opencode.ai/install | bash)",
    "plain": False,
    "build_args": _opencode_build_args,
    "parse_event": _opencode_parse_event,
}

# ---------------------------------------------------------------------------
# qwen-code
# ---------------------------------------------------------------------------

def _qwen_code_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    args = ["qwen", "-p", message, "--output-format", "stream-json", "--yolo"]
    if (env.get("QWEN_SANDBOX") or "").strip().lower() == "false":
        args += ["--sandbox", "false"]
    model = env.get("QWEN_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    return args


def _qwen_code_parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    etype = event.get("type")
    if etype == "init":
        return {"session_id": event.get("session_id")}
    if etype == "message":
        if event.get("role") != "assistant":
            return None
        text = event.get("content", "")
        if not text:
            return None
        return {"partial_text": text} if event.get("delta") else {"final_text": text}
    if etype == "error":
        if event.get("severity") == "error":
            return {"error": event.get("message") or "qwen reported an error"}
        return None
    if etype == "result" and event.get("status") == "error":
        err = event.get("error") or {}
        return {"error": err.get("message") or "qwen turn failed"}
    return None


QWEN_CODE = {
    "cli_name": "qwen",
    "install_hint": "Install: npm install -g @qwen-code/qwen-code",
    "plain": False,
    "build_args": _qwen_code_build_args,
    "parse_event": _qwen_code_parse_event,
}

# ---------------------------------------------------------------------------
# Plain-text bridges (no NDJSON; full stdout is the reply body)
# ---------------------------------------------------------------------------

# agy supports --conversation <id> for resuming prior conversations.
# make_handlers generates or reuses the session id so it can be returned
# in RunResult.session_id after the process exits.

def _make_agy_handlers() -> Dict[str, Any]:
    session: Dict[str, Optional[str]] = {"id": None}

    def build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
        session["id"] = session_id or str(uuid.uuid4())
        # Message last, after "--", so a message starting with "-" stays positional.
        args = ["agy", "--print", "--conversation", session["id"]]
        if (env.get("AGY_DANGEROUSLY_SKIP_PERMISSIONS") or "1") == "1":
            args.append("--dangerously-skip-permissions")
        model = env.get("AGY_MODEL", "").strip()
        if model:
            args += ["--model", model]
        args += ["--", message]
        return args

    def get_session_id() -> Optional[str]:
        return session["id"]

    return {"build_args": build_args, "get_session_id": get_session_id}


AGY = {
    "cli_name": "agy",
    "install_hint": "See the agy project for installation instructions.",
    "plain": True,
    "make_handlers": _make_agy_handlers,
}


def _aider_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    args = ["aider", "--message", message, "--yes-always", "--no-show-release-notes", "--no-stream"]
    model = env.get("AIDER_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


AIDER = {
    "cli_name": "aider",
    "install_hint": "Install: pip install aider-chat",
    "plain": True,
    "build_args": _aider_build_args,
}


def _deepseek_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    args = ["deepseek", "exec", "-p", message]
    model = env.get("DEEPSEEK_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


DEEPSEEK = {
    "cli_name": "deepseek",
    "install_hint": "Install from https://deepseek.com/downloads or: brew install deepseek",
    "plain": True,
    "build_args": _deepseek_build_args,
}


def _dsh_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    # "--" so a task starting with "-" stays a positional (hub/dsh does the same).
    return ["dsh", "--profile", "headless", "--", message]


DSH = {
    "cli_name": "dsh",
    "install_hint": "Install: npm install -g @deepseek-ai/dsh",
    "plain": True,
    "build_args": _dsh_build_args,
}


def _pi_build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
    args = ["pi", "-p", message, "--approve"]
    if (env.get("PI_NO_EXTENSIONS") or "1") != "0":
        args.append("--no-extensions")
    model = env.get("PI_MODEL", "").strip()
    if model:
        args += ["--model", model]
    return args


PI = {
    "cli_name": "pi",
    "install_hint": "Install: npm install -g @earendil-works/pi-coding-agent",
    "plain": True,
    "build_args": _pi_build_args,
}

# ---------------------------------------------------------------------------
# grok-build
# ---------------------------------------------------------------------------

_GROK_SOFT_CHARS = 40
_GROK_HARD_CHARS = 80
_GROK_BOUNDARY = frozenset("\n。！？；.!?;")


def _grok_should_flush(buf: str) -> bool:
    if not buf:
        return False
    if len(buf) >= _GROK_HARD_CHARS:
        return True
    if buf[-1] in _GROK_BOUNDARY and len(buf) >= _GROK_SOFT_CHARS:
        return True
    if buf[-1] == "\n":
        return True
    return False


def _make_grok_build_handlers() -> Dict[str, Any]:
    full: List[str] = []
    pending = ""

    def flush_pending() -> Optional[str]:
        nonlocal pending
        if not pending:
            return None
        chunk = pending
        pending = ""
        return chunk

    def build_args(message: str, session_id: str, env: Dict[str, str], _ctx: Optional[Dict[str, Any]] = None) -> List[str]:
        args = [
            "grok", "-p", message,
            "--output-format", "streaming-json",
            "--always-approve",
            "--no-auto-update",
        ]
        model = env.get("GROK_MODEL", "").strip()
        if model:
            args += ["-m", model]
        if session_id:
            args += ["-r", session_id]
        return args

    def parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        nonlocal pending
        etype = event.get("type")
        if etype == "text":
            data = event.get("data") or ""
            if not data:
                return None
            full.append(data)
            pending += data
            if _grok_should_flush(pending):
                return {"partial_text": flush_pending()}
            return None
        if etype == "thought":
            return None
        if etype == "end":
            sid = event.get("sessionId")
            leftover = flush_pending()
            out: Dict[str, Any] = {"final_text": "".join(full)}
            if leftover:
                out["partial_text"] = leftover
            if isinstance(sid, str) and sid:
                out["session_id"] = sid
            return out
        if etype == "error":
            sid = event.get("sessionId")
            pending = ""
            out = {"error": event.get("message") or "grok reported an error"}
            if isinstance(sid, str) and sid:
                out["session_id"] = sid
            return out
        return None

    return {"build_args": build_args, "parse_event": parse_event}


GROK_BUILD = {
    "cli_name": "grok",
    "install_hint": "Install: curl -fsSL https://x.ai/cli/install.sh | bash",
    "plain": False,
    "make_handlers": _make_grok_build_handlers,
}

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

EXECUTORS: Dict[str, Dict[str, Any]] = {
    "claude-code": CLAUDE_CODE,
    "codebuddy": CODEBUDDY,
    "codex": CODEX,
    "cursor": CURSOR,
    "gemini-cli": GEMINI_CLI,
    "grok-build": GROK_BUILD,
    "kimi-code": KIMI_CODE,
    "opencode": OPENCODE,
    "qwen-code": QWEN_CODE,
    "agy": AGY,
    "aider": AIDER,
    "deepseek": DEEPSEEK,
    "dsh": DSH,
    "pi": PI,
}

executor_names: List[str] = list(EXECUTORS.keys())
