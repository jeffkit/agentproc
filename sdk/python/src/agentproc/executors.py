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
    build_args:   (message: str, session_id: str, env: dict) -> list[str]
    parse_event:  (event: dict) -> ParseResult | None
                  (omitted / irrelevant when plain: True)
    make_handlers: () -> {"build_args": ..., "parse_event"?: ..., "get_session_id"?: ...}
                  — optional factory for stateful executors (e.g. kimi-code)
                  that need fresh per-turn state shared between build_args and
                  parse_event.  When present, the runner calls make_handlers()
                  once per turn; the returned dict is used for that turn only.
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

import uuid
from typing import Any, Callable, Dict, List, Optional

__all__ = ["EXECUTORS", "executor_names"]


# ---------------------------------------------------------------------------
# claude-code
# ---------------------------------------------------------------------------

def _claude_code_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
    args = [
        "claude", "-p", message,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    disallow = env.get("CLAUDE_DISALLOW_TOOLS", "AskUserQuestion").strip()
    if disallow:
        args += ["--disallowed-tools", disallow]
    model = env.get("CLAUDE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    return args


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
    "build_args": _claude_code_build_args,
    "parse_event": _claude_code_parse_event,
}

# ---------------------------------------------------------------------------
# codebuddy
# ---------------------------------------------------------------------------

def _codebuddy_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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

def _codex_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
    model = env.get("CODEX_MODEL", "").strip()
    if session_id:
        args = ["codex", "exec", "resume", "--json", session_id, message]
        if model:
            args += ["-c", f'model="{model}"']
        return args
    args = ["codex", "exec", "--json", message]
    if model:
        args += ["-c", f'model="{model}"']
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

    def build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
        args = [
            "agent", "-p", message,
            "--output-format", "stream-json",
            "--stream-partial-output",
        ]
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

def _gemini_cli_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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

    def build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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

def _opencode_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
    args = ["opencode", "run", message, "--auto", "--format", "json"]
    if session_id:
        args += ["--session", session_id]
    model = env.get("OPENCODE_MODEL", "").strip()
    if model:
        args += ["--model", model]
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

def _qwen_code_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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

    def build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
        session["id"] = session_id or str(uuid.uuid4())
        args = ["agy", "--print", message, "--conversation", session["id"]]
        if (env.get("AGY_DANGEROUSLY_SKIP_PERMISSIONS") or "1") == "1":
            args.append("--dangerously-skip-permissions")
        model = env.get("AGY_MODEL", "").strip()
        if model:
            args += ["--model", model]
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


def _aider_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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


def _deepseek_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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


def _pi_build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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

    def build_args(message: str, session_id: str, env: Dict[str, str]) -> List[str]:
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
    "pi": PI,
}

executor_names: List[str] = list(EXECUTORS.keys())
