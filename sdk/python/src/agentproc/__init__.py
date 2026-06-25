"""
agentproc — AgentProc Protocol SDK (Python)

Implements the AgentProc P0 protocol so you can write a single async handler
instead of manually reading env vars and formatting stdout.

Protocol contract:
  Input  — env vars: AGENT_MESSAGE, AGENT_SESSION_ID, AGENT_SESSION_NAME,
                     AGENT_FROM_USER, AGENT_STREAMING
  Output — stdout:
             optional first line  "AGENT_SESSION:<uuid>"
             optional partial lines "AGENT_PARTIAL:<json-string>"
             remaining lines = final reply text
  Exit   — 0 = success, non-zero = error

Example::

    from agentproc import create_profile

    async def handler(ctx):
        reply = await my_llm(ctx.message)
        return reply

    create_profile(handler)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Union

__all__ = [
    "AgentContext",
    "AgentResult",
    "create_profile",
    "load_history",
    "append_history",
    "HistoryEntry",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentContext:
    """Input context passed to the agent handler."""

    message: str
    """User message text (AGENT_MESSAGE)."""

    session_id: str
    """Session UUID from the previous turn (AGENT_SESSION_ID). Empty = new session."""

    session_name: str
    """Human-readable session name (AGENT_SESSION_NAME)."""

    from_user: str
    """Sender identifier (AGENT_FROM_USER)."""

    streaming: bool
    """Whether the bridge expects streaming output (AGENT_STREAMING == "1")."""

    image_url: str
    """Image attachment URL (AGENT_IMAGE_URL). Empty if no image."""

    file_url: str
    """File attachment URL (AGENT_FILE_URL). Empty if no file."""

    async def send_partial(self, text: str) -> None:
        """Send a streaming chunk to the user immediately.

        Writes an ``AGENT_PARTIAL:<json>`` line to stdout and flushes.
        The bridge forwards it to the user without waiting for the process to exit.

        Has no effect when ``streaming`` is False (bridge will ignore the line).
        """
        if not text:
            return
        line = f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}\n"
        sys.stdout.write(line)
        sys.stdout.flush()


@dataclass
class AgentResult:
    """Return value from the agent handler."""

    response: str = ""
    """Final reply text. Can be empty if all content was sent via send_partial."""

    session_id: str = ""
    """CLI session UUID to persist. Bridge will pass it back next turn as AGENT_SESSION_ID."""


@dataclass
class HistoryEntry:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# History helpers (JSONL, stored per session)
# ---------------------------------------------------------------------------

def session_file_path(session_id: str, base_dir: Optional[str] = None) -> Path:
    if not session_id:
        raise ValueError("session_id must be non-empty")
    root = Path(base_dir) if base_dir else Path.home() / ".agentproc" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.jsonl"


def load_history(session_id: str, base_dir: Optional[str] = None) -> List[HistoryEntry]:
    if not session_id:
        return []
    path = session_file_path(session_id, base_dir)
    if not path.exists():
        return []
    entries: List[HistoryEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            entries.append(HistoryEntry(
                role=d.get("role", ""),
                content=d.get("content", ""),
                timestamp=d.get("timestamp", ""),
            ))
        except json.JSONDecodeError:
            continue
    return entries


def append_history(
    session_id: str,
    entries: List[HistoryEntry],
    base_dir: Optional[str] = None,
) -> None:
    if not session_id or not entries:
        return
    path = session_file_path(session_id, base_dir)
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps({"role": e.role, "content": e.content, "timestamp": e.timestamp}, ensure_ascii=False))
            f.write("\n")


# ---------------------------------------------------------------------------
# Core entrypoint
# ---------------------------------------------------------------------------

Handler = Callable[[AgentContext], Awaitable[Union[str, AgentResult]]]


def create_profile(handler: Handler) -> None:
    """Run the handler as an AgentProc-compliant process.

    Reads AGENT_* env vars, calls the handler, and writes the P0 output to stdout.
    Call this at the bottom of your script — it blocks until the handler completes
    and then exits the process.

    Args:
        handler: An async function that takes an AgentContext and returns either
                 a plain string or an AgentResult.
    """
    ctx = AgentContext(
        message=os.environ.get("AGENT_MESSAGE", ""),
        session_id=os.environ.get("AGENT_SESSION_ID", ""),
        session_name=os.environ.get("AGENT_SESSION_NAME", "default"),
        from_user=os.environ.get("AGENT_FROM_USER", ""),
        streaming=os.environ.get("AGENT_STREAMING", "1") != "0",
        image_url=os.environ.get("AGENT_IMAGE_URL", ""),
        file_url=os.environ.get("AGENT_FILE_URL", ""),
    )

    try:
        result = asyncio.run(handler(ctx))
    except Exception as e:
        sys.stderr.write(f"[agentproc] handler error: {e}\n")
        sys.exit(1)

    if isinstance(result, str):
        result = AgentResult(response=result)

    # Emit session line first if we have one
    if result.session_id:
        sys.stdout.write(f"AGENT_SESSION:{result.session_id}\n")
        sys.stdout.flush()

    # Emit final reply body
    if result.response:
        sys.stdout.write(result.response)
        if not result.response.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    sys.exit(0)
