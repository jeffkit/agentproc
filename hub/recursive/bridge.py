#!/usr/bin/env python3
"""
AgentProc bridge for the `recursive` CLI (self-improving Rust coding agent,
wire 0.3).

recursive emits its lifecycle as NDJSON `AgentEvent` objects when run with
`--json`. This bridge wraps:

    recursive --json --stream ... run <message>                    # turn 1
    recursive --json --stream ... resume --from-file <session-dir> \\
        -p <message>                                               # turn N+

and translates the event stream to AgentProc NDJSON output. The bridge always
passes `--stream` and always emits {"type":"partial"} events; the runner
forwards them only when the profile's streaming is true (and drops them
otherwise). A single {"type":"text"} event with the assembled reply is emitted
at the end so the reply body is populated regardless of streaming mode.

Multi-turn continuity (native session resume)
----------------------------------------------
recursive records each run as a session directory (emitted on stderr as
`session: recording to <dir>`). `recursive resume --from-file <dir> -p <msg>`
continues that session by appending `<msg>` as the next user turn — the
orthodox session-id resume. So the bridge:

  - Turn 1: mint an opaque AgentProc session id; run `recursive run`; capture
    the recursive session directory from stderr and persist it keyed by the
    opaque id.
  - Turn N: load the stored session directory and run
    `recursive resume --from-file <dir> -p <message>`.

Per-CLI config (read from the process env the runner injects):
    RECURSIVE_API_KEY         Optional → `recursive --api-key`
    RECURSIVE_PROVIDER        Optional → `recursive --provider` (openai|anthropic)
    RECURSIVE_API_BASE        Optional → `recursive --api-base`
    RECURSIVE_MODEL           Optional → `recursive --model`
    RECURSIVE_WORKSPACE       Optional → `recursive --workspace` (default: cwd)
    RECURSIVE_MAX_STEPS       Optional → `recursive --max-steps`
    RECURSIVE_PERMISSION_MODE Optional → `recursive --permission-mode` (default: auto)
    RECURSIVE_STATE_DIR       Optional → where session-dir links persist (default: tmpdir)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from typing import Dict, List, Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import read_turn  # noqa: E402

CLI_NAME = "recursive"
INSTALL_HINT = (
    "Install: cargo install --locked --path .  (then `recursive init` to configure a provider)"
)

# `session: recording to <abs-dir>` — recursive logs this to stderr when it
# creates a session writer. The directory is the 4th whitespace token.
_SESSION_RECORDING_RE = re.compile(r"session: recording to (\S+)")


# ---------------------------------------------------------------------------
# NDJSON emission helpers
# ---------------------------------------------------------------------------

def _emit_obj(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit_session(session_id: str) -> None:
    _emit_obj({"type": "session", "id": session_id})


def emit_partial(text: str) -> None:
    _emit_obj({"type": "partial", "text": text})


def emit_text(text: str) -> None:
    _emit_obj({"type": "text", "text": text})


def emit_error(text: str) -> None:
    _emit_obj({"type": "error", "message": text})


# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def state_dir() -> str:
    d = _env("RECURSIVE_STATE_DIR")
    if not d:
        d = os.path.join(tempfile.gettempdir(), "agentproc-recursive")
    os.makedirs(d, exist_ok=True)
    return d


def session_link_path(sid: str) -> str:
    """File that stores the recursive session directory for opaque id `sid`."""
    return os.path.join(state_dir(), f"{sid}.session")


def read_session_dir(sid: str) -> Optional[str]:
    """Return the stored recursive session directory, or None if missing/stale."""
    try:
        with open(session_link_path(sid), "r", encoding="utf-8") as f:
            d = f.read().strip()
    except OSError:
        return None
    if d and os.path.isdir(d):
        return d
    return None


def write_session_dir(sid: str, session_dir: str) -> None:
    try:
        with open(session_link_path(sid), "w", encoding="utf-8") as f:
            f.write(session_dir)
    except OSError:
        pass


def extract_session_dir(stderr: str) -> Optional[str]:
    m = _SESSION_RECORDING_RE.search(stderr)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Argument building
# ---------------------------------------------------------------------------

def provider_args() -> list[str]:
    args: list[str] = []
    if _env("RECURSIVE_API_KEY"):
        args += ["--api-key", _env("RECURSIVE_API_KEY")]
    if _env("RECURSIVE_PROVIDER"):
        args += ["--provider", _env("RECURSIVE_PROVIDER")]
    if _env("RECURSIVE_API_BASE"):
        args += ["--api-base", _env("RECURSIVE_API_BASE")]
    if _env("RECURSIVE_MODEL"):
        args += ["--model", _env("RECURSIVE_MODEL")]
    return args


def _global_args() -> list[str]:
    args: list[str] = [CLI_NAME, "--json", "-H", "--stream"]
    # Permission mode: default to auto (headless). The bridge runs
    # non-interactively, so prompt-based approval would hang.
    args += ["--permission-mode", _env("RECURSIVE_PERMISSION_MODE") or "auto"]
    if _env("RECURSIVE_MAX_STEPS"):
        args += ["--max-steps", _env("RECURSIVE_MAX_STEPS")]
    if _env("RECURSIVE_WORKSPACE"):
        args += ["--workspace", _env("RECURSIVE_WORKSPACE")]
    args += provider_args()
    return args


def build_run_args(message: str) -> list[str]:
    return _global_args() + ["run", message]


def build_resume_args(session_dir: str, message: str) -> list[str]:
    # `resume --from-file <dir> -p <msg>` continues the session by appending
    # <msg> as the next user turn (native session-id resume).
    return _global_args() + ["resume", "--from-file", session_dir, "-p", message]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    turn = read_turn()
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    attachments = turn.get("attachments") if isinstance(turn.get("attachments"), list) else []
    if not message and not attachments:
        emit_error("turn.message is required (or include turn.attachments)")
        return 1

    # Resolve opaque session id + whether we resume an existing recursive session.
    given_sid = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""
    resume_dir: Optional[str] = None
    if given_sid:
        d = read_session_dir(given_sid)
        if d:
            sid = given_sid
            resume_dir = d
        else:
            # Stale id (state dir cleared / prior turn crashed before the
            # session was recorded). Start fresh with a new id.
            sid = "rc-" + uuid.uuid4().hex
    else:
        sid = "rc-" + uuid.uuid4().hex

    args = (
        build_resume_args(resume_dir, message)
        if resume_dir
        else build_run_args(message)
    )

    # Emit the session id FIRST so the runner captures it even if recursive
    # later fails to produce any output.
    emit_session(sid)

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        emit_error(f"{CLI_NAME} CLI not found. {INSTALL_HINT}")
        return 1

    # Per-step text buffers (ordered) so we can assemble the final reply.
    step_order: List[int] = []
    step_buffers: Dict[int, List[str]] = {}
    steps_with_partials: set[int] = set()
    error_message: Optional[str] = None

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")

        if etype == "partial_token":
            text = event.get("text", "")
            if text:
                step = event.get("step")
                if step is not None:
                    steps_with_partials.add(step)
                    if step not in step_buffers:
                        step_buffers[step] = []
                        step_order.append(step)
                    step_buffers[step].append(text)
                emit_partial(text)
            continue

        if etype == "assistant_text":
            text = event.get("text", "")
            if not text:
                continue
            step = event.get("step")
            # If this step already streamed deltas, they compose the step text —
            # skip the duplicate full text. Otherwise this is our only chance
            # for the step text.
            if step is not None and step in steps_with_partials:
                continue
            if step is not None:
                if step not in step_buffers:
                    step_buffers[step] = []
                    step_order.append(step)
                step_buffers[step].append(text)
            emit_partial(text)
            continue

        if etype == "turn_finished":
            # Terminal event — keep reading until EOF anyway.
            continue

    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    # For a fresh run, record the recursive session directory so the next
    # turn can resume it. (resume reuses the same dir — no update needed.)
    if resume_dir is None:
        captured = extract_session_dir(stderr_output)
        if captured:
            write_session_dir(sid, captured)

    reply_text = "".join("".join(step_buffers[s]) for s in step_order).strip()

    # Fallback: if recursive produced no streamed text at all, try to recover
    # the last assistant message from the session transcript.
    if not reply_text and not error_message:
        sess_dir = resume_dir or read_session_dir(sid)
        recovered = _last_assistant_text(sess_dir) if sess_dir else None
        if recovered:
            reply_text = recovered.strip()

    if error_message:
        emit_error(error_message)
        return 1
    if proc.returncode != 0 and not reply_text:
        msg = f"{CLI_NAME} exited with {proc.returncode}"
        stderr_tail = stderr_output.strip()
        if stderr_tail:
            msg += f": {stderr_tail[:500]}"
        emit_error(msg)
        return 1
    if not reply_text:
        emit_error(f"{CLI_NAME} produced no reply text")
        return 1
    emit_text(reply_text)
    return 0


def _last_assistant_text(session_dir: Optional[str]) -> Optional[str]:
    """Read the last assistant text from a session's transcript.jsonl."""
    if not session_dir:
        return None
    path = os.path.join(session_dir, "transcript.jsonl")
    last: Optional[str] = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("role") == "assistant" and entry.get("content"):
                    last = entry["content"]
    except OSError:
        return None
    return last


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(1)
