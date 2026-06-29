#!/usr/bin/env python3
"""
AgentProc bridge for the `recursive` CLI (self-improving Rust coding agent).

recursive emits its lifecycle as NDJSON `AgentEvent` objects when run with
`--json`. This bridge wraps:

    recursive --json [--stream] ... run <message>              # first turn
    recursive --json [--stream] ... replay <transcript> \\
        --resume-from <N> <message>                            # subsequent turns

and translates the event stream to AgentProc protocol output.

Multi-turn continuity
---------------------
recursive's CLI does NOT expose its internal session id in the `--json` event
stream, and `recursive resume` re-runs the ORIGINAL goal instead of accepting
a new user message. So the bridge manages continuity itself:

  - Turn 1: mint an opaque session id; tell recursive to persist the turn's
    transcript to <state_dir>/<id>.json via `--transcript-out`.
  - Turn N: load that transcript, feed it back via
    `replay <path> --resume-from <N> <message>` (which seeds recursive's
    runtime with the prior conversation and appends the new message as the
    next user turn), then rewrite the file with the extended transcript.

System messages are stripped from the stored transcript between turns —
recursive re-prepends its system prompt on every invocation, so keeping them
would duplicate the system prompt once per turn.

Env vars (in addition to the AGENT_* vars injected by the runner):
    AGENT_MESSAGE             User message (required, or an attachment)
    AGENT_SESSION_ID          Opaque id from the previous turn (empty = new)
    AGENT_STREAMING           "1" streaming, "0" one-shot
    RECURSIVE_API_KEY         Optional → `recursive --api-key`
    RECURSIVE_PROVIDER        Optional → `recursive --provider` (openai|anthropic)
    RECURSIVE_API_BASE        Optional → `recursive --api-base`
    RECURSIVE_MODEL           Optional → `recursive --model`
    RECURSIVE_WORKSPACE       Optional → `recursive --workspace` (default: cwd)
    RECURSIVE_MAX_STEPS       Optional → `recursive --max-steps`
    RECURSIVE_PERMISSION_MODE Optional → `recursive --permission-mode` (default: auto)
    RECURSIVE_STATE_DIR       Optional → where transcripts persist (default: tmpdir)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from typing import Optional

CLI_NAME = "recursive"
INSTALL_HINT = (
    "Install: cargo install --locked --path .  (then `recursive init` to configure a provider)"
)


# ---------------------------------------------------------------------------
# Emission helpers
# ---------------------------------------------------------------------------

def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_session(session_id: str) -> None:
    emit(f"AGENT_SESSION:{session_id}")


def emit_partial(text: str) -> None:
    emit(f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}")


def emit_error(text: str) -> None:
    emit(f"AGENT_ERROR:{json.dumps(text, ensure_ascii=False)}")


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


def transcript_path(sid: str) -> str:
    return os.path.join(state_dir(), f"{sid}.json")


def count_messages(path: str) -> Optional[int]:
    """Number of messages in a TranscriptFile JSON. None if unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list):
        return None
    return len(msgs)


def strip_system_messages(path: str) -> None:
    """Rewrite a TranscriptFile JSON, dropping role=='system' messages.

    recursive re-prepends its system prompt on every run, so keeping stored
    system messages would duplicate it once per turn. We keep every other
    message (and the rest of the file structure) intact.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return
    data["messages"] = [m for m in data["messages"] if m.get("role") != "system"]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


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


def build_args(message: str, path: str, resume_n: Optional[int]) -> list[str]:
    args: list[str] = [CLI_NAME, "--json", "-H", "--no-session"]
    # --transcript-out MUST be a global flag (before the subcommand).
    args += ["--transcript-out", path]
    if os.environ.get("AGENT_STREAMING", "1") != "0":
        args.append("--stream")
    # Permission mode: default to auto (headless). The bridge runs
    # non-interactively, so prompt-based approval would hang.
    args += ["--permission-mode", _env("RECURSIVE_PERMISSION_MODE") or "auto"]
    if _env("RECURSIVE_MAX_STEPS"):
        args += ["--max-steps", _env("RECURSIVE_MAX_STEPS")]
    if _env("RECURSIVE_WORKSPACE"):
        args += ["--workspace", _env("RECURSIVE_WORKSPACE")]
    args += provider_args()
    if resume_n is not None:
        args += ["replay", path, "--resume-from", str(resume_n), message]
    else:
        args += ["run", message]
    return args


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _has_attachment() -> bool:
    if _env("AGENT_IMAGE_URL") or _env("AGENT_FILE_URL"):
        return True
    raw = _env("AGENT_ATTACHMENTS")
    return bool(raw) and raw != "[]"


def main() -> int:
    message = os.environ.get("AGENT_MESSAGE", "")
    if not message and not _has_attachment():
        emit_error(
            "AGENT_MESSAGE env var is required (or set AGENT_ATTACHMENTS / "
            "AGENT_IMAGE_URL / AGENT_FILE_URL)"
        )
        return 1

    streaming = os.environ.get("AGENT_STREAMING", "1") != "0"

    # Resolve session id + resume target.
    given_sid = _env("AGENT_SESSION_ID")
    resume_n: Optional[int] = None
    if given_sid:
        path = transcript_path(given_sid)
        n = count_messages(path)
        if n is not None and n > 0:
            sid = given_sid
            resume_n = n
        else:
            # Stale id (state dir cleared / prior turn crashed before writing
            # the transcript). Start fresh with a new id.
            sid = "rc-" + uuid.uuid4().hex
    else:
        sid = "rc-" + uuid.uuid4().hex

    path = transcript_path(sid)
    args = build_args(message, path, resume_n)

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

    saw_partial = False
    steps_with_partials: set[int] = set()
    assistant_texts: list[str] = []  # collected for non-streaming fallback
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
                if streaming:
                    emit_partial(text)
                    saw_partial = True
                else:
                    assistant_texts.append(text)
            continue

        if etype == "assistant_text":
            text = event.get("text", "")
            if not text:
                continue
            step = event.get("step")
            # In streaming mode recursive emits partial_token deltas AND a
            # final assistant_text with the full step text — skip the
            # duplicate unless this step had no deltas (provider didn't
            # stream), in which case the full text is our only chance.
            if streaming:
                if step in steps_with_partials:
                    continue
                emit_partial(text)
                saw_partial = True
            else:
                assistant_texts.append(text)
            continue

        if etype == "turn_finished":
            # Terminal event — keep reading until EOF anyway.
            continue

    proc.wait()
    stderr_output = proc.stderr.read() if proc.stderr else ""

    # Non-streaming: emit collected assistant text as the reply body (plain
    # lines, no AGENT_ prefix — the runner treats them as the reply).
    if not streaming and assistant_texts:
        body = "".join(assistant_texts).strip()
        if body:
            emit(body)
            saw_partial = True

    # Fallback: if recursive produced no streamed/body text at all, try to
    # recover the last assistant message from the transcript file.
    if not saw_partial and not error_message:
        recovered = _last_assistant_text(path)
        if recovered:
            if streaming:
                emit_partial(recovered)
            else:
                emit(recovered)
            saw_partial = True

    # Rewrite the transcript so the next turn's seed has no system messages.
    strip_system_messages(path)

    if error_message:
        emit_error(error_message)
        return 1
    if proc.returncode != 0 and not saw_partial:
        msg = f"{CLI_NAME} exited with {proc.returncode}"
        stderr_tail = stderr_output.strip()
        if stderr_tail:
            msg += f": {stderr_tail[:500]}"
        emit_error(msg)
        return 1
    if not saw_partial:
        emit_error(f"{CLI_NAME} produced no reply text")
        return 1
    return 0


def _last_assistant_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return None
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("content"):
            return m["content"]
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(1)
