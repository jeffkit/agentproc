"""
agentproc — AgentProc Protocol SDK (Python)

Implements the AgentProc P0 protocol so you can write a single async handler
instead of manually reading env vars and formatting stdout.

Protocol contract (spec/protocol.md, wire protocol 0.1):
  Input  — env vars: AGENT_MESSAGE, AGENT_SESSION_ID, AGENT_SESSION_NAME,
                     AGENT_FROM_USER, AGENT_STREAMING, AGENT_PROTOCOL_VERSION,
                     AGENT_IMAGE_URL, AGENT_FILE_URL
  Output — stdout (sentinel-prefixed lines):
             AGENT_SESSION:<opaque-id>     — declare session id (last wins)
             AGENT_PARTIAL:<json-string>   — streaming chunk
             AGENT_ERROR:<json-string>     — error message to forward to user
             everything else               = final reply body
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
from typing import Awaitable, Callable, List, Optional, Sequence, Union

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

    The SDK emits an ``AGENT_ERROR:`` line with the message and exits non-zero.
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
    """User message text (AGENT_MESSAGE)."""

    session_id: str
    """Session ID from the previous turn (AGENT_SESSION_ID). Empty = new session."""

    session_name: str
    """Human-readable session name (AGENT_SESSION_NAME)."""

    from_user: str
    """Sender identifier (AGENT_FROM_USER)."""

    streaming: bool
    """Whether the bridge expects streaming output (AGENT_STREAMING == "1")."""

    protocol_version: str
    """Protocol version the bridge implements (AGENT_PROTOCOL_VERSION)."""

    image_url: str
    """Image attachment URL (AGENT_IMAGE_URL). Empty if no image."""

    file_url: str
    """File attachment URL (AGENT_FILE_URL). Empty if no file."""

    def send_partial(self, text: str):
        """Send a streaming chunk to the user immediately.

        Writes an ``AGENT_PARTIAL:<json>`` line to stdout and flushes at call
        time. The bridge forwards it to the user without waiting for the
        process to exit. Has no effect when ``streaming`` is False (the bridge
        will ignore the line).

        Returns a no-op awaitable so ``await ctx.send_partial(...)`` keeps
        working in async handlers; a sync handler may call it bare. Either way
        the write happens at call time.
        """
        if not text:
            return _NoopAwaitable()
        line = f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}\n"
        sys.stdout.write(line)
        sys.stdout.flush()
        return _NoopAwaitable()

    def send_error(self, text: str):
        """Send an error message to the user.

        Writes an ``AGENT_ERROR:<json>`` line to stdout and flushes at call
        time. Honored regardless of ``streaming`` mode. After calling this, the
        handler should typically raise ProtocolError or return — any reply body
        produced alongside will be discarded by the bridge.

        Returns a no-op awaitable so ``await ctx.send_error(...)`` keeps working
        in async handlers; a sync handler may call it bare.
        """
        if not text:
            return _NoopAwaitable()
        line = f"AGENT_ERROR:{json.dumps(text, ensure_ascii=False)}\n"
        sys.stdout.write(line)
        sys.stdout.flush()
        return _NoopAwaitable()


@dataclass
class AgentResult:
    """Return value from the agent handler."""

    response: str = ""
    """Final reply text. Can be empty if all content was sent via send_partial."""

    session_id: str = ""
    """Session ID to persist. Bridge will pass it back next turn as AGENT_SESSION_ID."""


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

    Rejects any id that is not a spec-compliant session id (see
    ``is_valid_session_id`` in runner.py — non-empty, no whitespace / control
    chars / colons / path separators), plus the literal ``.`` and ``..`` even
    though those pass the charset (the regex allows ``.``). A handler can call
    this with any string, and we store each session as ``<id>.jsonl`` — a
    separator-bearing or ``..`` id would path-traverse out of the sessions
    directory. Valid ids like ``a..b`` are accepted (no traversal).
    """
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if not is_valid_session_id(session_id) or session_id in (".", ".."):
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
# Env parsing helpers
# ---------------------------------------------------------------------------

def _context_from_env() -> AgentContext:
    return AgentContext(
        message=os.environ.get("AGENT_MESSAGE", ""),
        session_id=os.environ.get("AGENT_SESSION_ID", ""),
        session_name=os.environ.get("AGENT_SESSION_NAME", "default"),
        from_user=os.environ.get("AGENT_FROM_USER", ""),
        streaming=os.environ.get("AGENT_STREAMING", "1") != "0",
        protocol_version=os.environ.get("AGENT_PROTOCOL_VERSION", PROTOCOL_VERSION),
        image_url=os.environ.get("AGENT_IMAGE_URL", ""),
        file_url=os.environ.get("AGENT_FILE_URL", ""),
    )


# ---------------------------------------------------------------------------
# Core entrypoint
# ---------------------------------------------------------------------------

Handler = Callable[[AgentContext], Union[Awaitable[Union[str, AgentResult, None]], Union[str, AgentResult, None]]]


def create_profile(handler: Handler) -> None:
    """Run the handler as an AgentProc-compliant process.

    Reads AGENT_* env vars, calls the handler, and writes the P0 output to stdout.
    Call this at the bottom of your script — it blocks until the handler completes
    and then exits the process.

    Args:
        handler: A function taking an :class:`AgentContext` and returning either
            a plain string (treated as ``AgentResult(response=...)``), an
            :class:`AgentResult`, or ``None`` (when everything was signalled
            via ``send_partial`` / ``send_error``). The handler may be ``async``
            or a plain sync function — a sync handler is run directly, an async
            one (or any handler returning a coroutine) is awaited via
            :func:`asyncio.run`. This mirrors the Node SDK, which accepts both.
    """
    ctx = _context_from_env()

    try:
        ret = handler(ctx)
        # An async handler (or a sync one returning a coroutine) is awaited to
        # completion; a sync handler's plain return value is used as-is.
        result = asyncio.run(ret) if inspect.iscoroutine(ret) else ret
    except ProtocolError as e:
        # Handler already-signaled error surfaced via exception.
        sys.stdout.write(
            f"AGENT_ERROR:{json.dumps(str(e) or 'unknown error', ensure_ascii=False)}\n"
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

    # session line — emitted last in the typical "I just learned it" flow,
    # but the spec says last wins, so emitting it at the end is correct.
    if result.session_id:
        sys.stdout.write(f"AGENT_SESSION:{result.session_id}\n")
        sys.stdout.flush()

    if result.response:
        sys.stdout.write(result.response)
        if not result.response.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    sys.exit(0)
