"""
agentproc — AgentProc Protocol SDK (Python)

Implements the AgentProc P0 protocol so you can write a single async handler
instead of manually reading the turn from stdin and formatting stdout.

Protocol contract (spec/protocol.md, wire protocol 0.3, NDJSON both directions):
  Input  — stdin: one {"type":"turn",...} line (message, session_id,
                   session_name, from_user, attachments, permission,
                   protocol_version). Secrets/config stay in env.
  Output — stdout (one JSON object per line, discriminated by `type`):
             {"type":"partial","text":...}    — streaming chunk
             {"type":"text","text":...}       — final reply body
             {"type":"session","id":...}      — declare session id (last wins)
             {"type":"error","message":...}   — error message to forward to user
  Exit   — 0 success, 1 error, 124 timeout, 130 SIGINT, 143 SIGTERM

Example::

    from agentproc import create_profile

    async def handler(ctx):
        reply = await my_llm(ctx.message)
        return reply

    create_profile(handler)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union

__all__ = [
    "AgentContext",
    "AgentResult",
    "HistoryEntry",
    "ProtocolError",
    "create_profile",
    "load_history",
    "append_history",
    "session_file_path",
    "__version__",
]


def _read_version() -> str:
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("agentproc")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    try:
        from pathlib import Path
        toml_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if toml_path.exists():
            import re
            text = toml_path.read_text(encoding="utf-8")
            m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "0.0.0+unknown"


__version__ = _read_version()

# Single source of truth: the wire-protocol version lives in runner.py
# (the canonical bridge-side engine). Re-exported here so
# `agentproc.PROTOCOL_VERSION` stays in lockstep without copy-pasted literals.
from .runner import PROTOCOL_VERSION, is_valid_session_id  # noqa: E402


class ProtocolError(Exception):
    """Raised by the handler to signal an error that should reach the user.

    The SDK emits a ``{"type":"error",...}`` line with the message and exits
    non-zero.
    """


class _NoopAwaitable:
    """An awaitable that does nothing. Returned by the sync ``send_partial`` /
    ``send_error`` so ``await ctx.send_partial(...)`` keeps working in async
    handlers while a bare ``ctx.send_partial(...)`` (sync handler) also writes
    — the write happens at call time, the await is a no-op."""

    def __await__(self):
        return
        yield  # pragma: no cover — makes this a generator-based awaitable


@dataclass
class AgentContext:
    """Input context passed to the agent handler."""

    message: str
    """User message text (turn.message)."""

    session_id: str
    """Session ID from the previous turn (turn.session_id). Empty = new session."""

    session_name: str
    """Human-readable session name (turn.session_name)."""

    from_user: str
    """Sender identifier (turn.from_user)."""

    protocol_version: str
    """Protocol version the bridge implements (turn.protocol_version)."""

    attachments: List[Dict[str, Any]]
    """Attachment list (turn.attachments). Empty list = no attachments."""

    permission: bool
    """True when the bridge enabled the optional permission channel."""

    def send_partial(self, text: str, role: Optional[str] = None):
        """Send a streaming chunk to the user immediately.

        Writes a ``{"type":"partial","text":...}`` line to stdout and flushes
        at call time. The bridge forwards it to the user without waiting for
        the process to exit. ``role`` (e.g. ``"output"`` or ``"thinking"``)
        is optional and MAY be rendered differently by the bridge.

        Returns a no-op awaitable so ``await ctx.send_partial(...)`` keeps
        working in async handlers; a sync handler may call it bare. Either way
        the write happens at call time.
        """
        if not text:
            return _NoopAwaitable()
        evt: Dict[str, Any] = {"type": "partial", "text": text}
        if role:
            evt["role"] = role
        sys.stdout.write(json.dumps(evt, ensure_ascii=False, separators=(',', ':')) + "\n")
        sys.stdout.flush()
        return _NoopAwaitable()

    def send_error(self, text: str):
        """Send an error message to the user.

        Writes a ``{"type":"error","message":...}`` line to stdout and flushes
        at call time. Honored regardless of ``streaming`` mode. After calling
        this, the handler should typically raise ProtocolError or return — any
        reply body produced alongside will be discarded by the bridge.

        Returns a no-op awaitable so ``await ctx.send_error(...)`` keeps working
        in async handlers; a sync handler may call it bare.
        """
        if not text:
            return _NoopAwaitable()
        sys.stdout.write(
            json.dumps({"type": "error", "message": text}, ensure_ascii=False, separators=(',', ':')) + "\n"
        )
        sys.stdout.flush()
        return _NoopAwaitable()

    def send_permission_request(self, req: Dict[str, Any]) -> None:
        """Send a tool-permission request to the bridge.

        Only valid when ``ctx.permission`` is True (the profile set
        ``permission: true`` and the bridge enabled the channel). The bridge
        surfaces the request to the user; the matching decision arrives on
        stdin as a ``{"type":"permission_response",...}`` frame that
        :meth:`read_permission_response` decodes.

        ``req`` MUST include ``request_id`` (unique within the turn),
        ``tool_name``, and ``input`` (object). ``description`` and
        ``tool_use_id`` are optional.
        """
        if not self.permission:
            raise RuntimeError(
                "ctx.send_permission_request() requires profile.permission: "
                "true — the bridge would otherwise not honor the request"
            )
        if not isinstance(req, dict):
            raise TypeError("req must be a dict")
        rid = req.get("request_id")
        if not isinstance(rid, str) or not rid:
            raise ValueError("req['request_id'] is required")
        payload: Dict[str, Any] = {"type": "permission_request"}
        for k in ("request_id", "tool_name", "input"):
            payload[k] = req.get(k)
        for k in ("description", "tool_use_id"):
            if k in req:
                payload[k] = req[k]
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        sys.stdout.flush()

    def read_permission_response(self) -> Optional[Dict[str, Any]]:
        """Read the next ``{"type":"permission_response",...}`` frame from stdin.

        Blocks until a frame arrives (or EOF). Returns the parsed object, or
        ``None`` at EOF.

        Only meaningful when ``ctx.permission`` is True — in the default
        ``permission: false`` mode the bridge closes stdin after the turn
        line, so this returns ``None`` immediately.
        """
        try:
            line = sys.stdin.readline()
        except Exception:
            return None
        if not line:
            return None
        try:
            v = json.loads(line.rstrip("\r\n"))
        except json.JSONDecodeError:
            return None
        if isinstance(v, dict):
            return v
        return None


@dataclass
class AgentResult:
    """Return value from the agent handler."""

    response: str = ""
    """Final reply text. Can be empty if all content was sent via send_partial."""

    session_id: str = ""
    """Session ID to persist. Bridge will pass it back next turn as session_id."""


@dataclass
class HistoryEntry:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# History helpers (JSONL, stored per session)
# ---------------------------------------------------------------------------

def session_file_path(session_id: str, base_dir: Optional[str] = None) -> Path:
    """Resolve the JSONL history file path for a session.

    Returns the path even if the file does not yet exist. Raises ``ValueError``
    when ``session_id`` is empty — callers should guard with ``if session_id``.

    In wire 0.3 a session id is an arbitrary JSON string on the wire, but the
    SDK stores each session as ``<id>.jsonl``; an id that is not a storage-safe
    filename component (path separators, control chars, ``.``/``..``) would
    path-traverse out of the sessions directory and is rejected here. See
    ``is_valid_session_id`` in runner.py.
    """
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if not is_valid_session_id(session_id):
        raise ValueError(f"session_id is not a safe filename component: {session_id!r}")
    root = Path(base_dir) if base_dir else Path.home() / ".agentproc" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.jsonl"


def load_history(session_id: str, base_dir: Optional[str] = None) -> List[HistoryEntry]:
    """Load conversation history for a session. Returns ``[]`` if no session
    or no file exists."""
    if not session_id:
        return []
    try:
        path = session_file_path(session_id, base_dir)
    except ValueError:
        return []
    if not path.exists():
        return []
    entries: List[HistoryEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append(HistoryEntry(
            role=d.get("role", ""),
            content=d.get("content", ""),
            timestamp=d.get("timestamp", ""),
        ))
    return entries


def append_history(
    session_id: str,
    entries: Sequence[HistoryEntry],
    base_dir: Optional[str] = None,
) -> None:
    """Append entries to a session's JSONL history file. No-op if no session_id."""
    if not session_id or not entries:
        return
    path = session_file_path(session_id, base_dir)
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(
                {"role": e.role, "content": e.content, "timestamp": e.timestamp},
                ensure_ascii=False,
            ))
            f.write("\n")


# ---------------------------------------------------------------------------
# Turn parsing — read the {"type":"turn",...} line from stdin
# ---------------------------------------------------------------------------

def _read_turn() -> Dict[str, Any]:
    """Read exactly one line from stdin (the turn object) and JSON-decode it.

    Returns an empty dict on any failure (best-effort, fail-soft per spec).
    """
    try:
        line = sys.stdin.readline()
    except Exception:
        return {}
    if not line:
        return {}
    line = line.rstrip("\r\n")
    try:
        v = json.loads(line)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    return {}


def _context_from_turn() -> AgentContext:
    t = _read_turn()
    return AgentContext(
        message=t.get("message") if isinstance(t.get("message"), str) else "",
        session_id=t.get("session_id") if isinstance(t.get("session_id"), str) else "",
        session_name=t.get("session_name") if isinstance(t.get("session_name"), str) else "default",
        from_user=t.get("from_user") if isinstance(t.get("from_user"), str) else "",
        protocol_version=t.get("protocol_version") if isinstance(t.get("protocol_version"), str) else PROTOCOL_VERSION,
        attachments=t.get("attachments") if isinstance(t.get("attachments"), list) else [],
        permission=t.get("permission") is True,
    )


# ---------------------------------------------------------------------------
# Core entrypoint
# ---------------------------------------------------------------------------

Handler = Callable[[AgentContext], Union[Awaitable[Union[str, AgentResult, None]], Union[str, AgentResult, None]]]


def create_profile(handler: Handler) -> None:
    """Run the handler as an AgentProc-compliant process.

    Reads the turn object from stdin, calls the handler, and writes the P0
    output to stdout. Call this at the bottom of your script — it blocks until
    the handler completes and then exits the process.

    Args:
        handler: A function taking an :class:`AgentContext` and returning either
            a plain string (treated as ``AgentResult(response=...)``), an
            :class:`AgentResult`, or ``None`` (when everything was signalled
            via ``send_partial`` / ``send_error``). The handler may be ``async``
            or a plain sync function — a sync handler is run directly, an async
            one (or any handler returning a coroutine) is awaited via
            :func:`asyncio.run`. This mirrors the Node SDK, which accepts both.
    """
    ctx = _context_from_turn()

    try:
        ret = handler(ctx)
        # An async handler (or a sync one returning a coroutine) is awaited to
        # completion; a sync handler's plain return value is used as-is.
        result = asyncio.run(ret) if inspect.iscoroutine(ret) else ret
    except ProtocolError as e:
        # Handler already-signaled error surfaced via exception.
        sys.stdout.write(
            json.dumps(
                {"type": "error", "message": str(e) or "unknown error"},
                ensure_ascii=False, separators=(',', ':'),
            ) + "\n"
        )
        sys.stdout.flush()
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"[agentproc] handler error: {e}\n")
        sys.exit(1)

    # Handler may return None when it has already signaled everything via
    # send_partial / send_error. Treat None as an empty AgentResult.
    if result is None:
        sys.exit(0)

    if isinstance(result, str):
        result = AgentResult(response=result)

    # session event — emitted last in the typical "I just learned it" flow,
    # but the spec says last wins, so emitting it at the end is correct.
    if result.session_id:
        sys.stdout.write(
            json.dumps({"type": "session", "id": result.session_id}, ensure_ascii=False, separators=(',', ':')) + "\n"
        )
        sys.stdout.flush()

    if result.response:
        sys.stdout.write(
            json.dumps({"type": "text", "text": result.response}, ensure_ascii=False, separators=(',', ':')) + "\n"
        )
        sys.stdout.flush()

    sys.exit(0)
