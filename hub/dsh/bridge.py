#!/usr/bin/env python3
"""
AgentProc bridge for the DeepSeek Harness CLI `dsh` (wire 0.4).

Uses `dsh --profile headless "<task>"` for non-interactive output. dsh
headless prints the final assistant message to stdout (plain text); errors
go to stderr ("dsh: CODE: message") with a non-zero exit code.

Session continuity is feature-detected: when the installed dsh supports
`--resume <id>` and `--print-session-id`, the bridge stamps `session_id`
on events and resumes the persisted session on later turns — true
multi-turn for process bridges. Older dsh builds stay stateless on the
wire, exactly as before.

Per-CLI config (read from the process env the runner injects):
    DEEPSEEK_API_KEY    API key passthrough (alternative: store it once via
                        the web UI's Models page; empty falls through)
    DSH_PERMISSION_MODE danger-full-access (bridge default, auto-approve) |
                        workspace-write | read-only
    DSH_TOOLS_MODE      optional dsh Code Mode opt-in passthrough
    DSH_TIMEOUT         process timeout in seconds (default: 1800)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import emit_error, emit_result  # noqa: E402

CLI_NAME = "dsh"
INSTALL_HINT = "Install: npm install -g @deepseek-ai/dsh"
DEFAULT_TIMEOUT_SECS = 1800
KILL_GRACE_SECS = 5
#: stderr line through which a resume-capable dsh reports the session id.
SESSION_ID_LINE = re.compile(r"^dsh: session-id: (\S+)\s*$", re.M)
#: Wire rules: non-empty, no path separators or control characters.
SESSION_ID_VALID = re.compile(r"^[^\s/\\\x00-\x1f]+$")


def read_turn() -> dict:
    """Read exactly one NDJSON turn object from stdin (mirror of _read_turn)."""
    line = sys.stdin.readline()
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def compose_task(message: str, turn: dict) -> str:
    """Compose the dsh task text: message plus attachments as reference URLs."""
    atts = turn.get("attachments")
    if not isinstance(atts, list) or not atts:
        return message
    lines = []
    for i, a in enumerate(atts):
        if not isinstance(a, dict):
            lines.append(f"- [unknown] attachment-{i + 1}")
            continue
        kind = a.get("kind") if isinstance(a.get("kind"), str) and a.get("kind") else "file"
        name = a.get("filename") if isinstance(a.get("filename"), str) and a.get("filename") else f"attachment-{i + 1}"
        url = a.get("url") if isinstance(a.get("url"), str) else ""
        lines.append(f"- [{kind}] {name}" + (f": {url}" if url else ""))
    base = message or "Please look at the following attachments."
    return base + "\n\nAttachments (referenced by URL):\n" + "\n".join(lines)


def build_args(task: str, session_id: str, supports_resume: bool) -> list[str]:
    """dsh argv; with continuity support, print the id and resume a known session."""
    args = [CLI_NAME, "--profile", "headless"]
    if supports_resume:
        args.append("--print-session-id")
        if session_id:
            args += ["--resume", session_id]
    args.append(task)
    return args


def parse_session_id(stderr: str) -> str:
    """Extract the id a resume-capable dsh printed; '' when absent or invalid."""
    m = SESSION_ID_LINE.search(stderr or "")
    if m and SESSION_ID_VALID.match(m.group(1)):
        return m.group(1)
    return ""


_resume_support: bool | None = None


def probe_resume_support() -> bool:
    """Probe (once per process) via the headless app's --help output."""
    global _resume_support
    if _resume_support is None:
        try:
            r = subprocess.run(
                [CLI_NAME, "--profile", "headless", "--help"],
                capture_output=True, text=True, timeout=30,
            )
            help_text = (r.stdout or "") + (r.stderr or "")
            _resume_support = "--resume" in help_text and "--print-session-id" in help_text
        except Exception:
            _resume_support = False
    return _resume_support


def child_env() -> dict:
    """Unattended default: auto-approve tools (see bridge.js for rationale)."""
    env = dict(os.environ)
    for key in ("DEEPSEEK_API_KEY", "DSH_PERMISSION_MODE", "DSH_TOOLS_MODE"):
        if env.get(key) == "":
            del env[key]
    if not env.get("DSH_PERMISSION_MODE"):
        env["DSH_PERMISSION_MODE"] = "danger-full-access"
    return env


def main() -> int:
    turn = read_turn()
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    inbound_session = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""
    atts = turn.get("attachments")
    has_att = isinstance(atts, list) and len(atts) > 0
    if not message and not has_att:
        emit_error("turn.message is required (or include turn.attachments)")
        return 1

    supports_resume = probe_resume_support()
    args = build_args(compose_task(message, turn), inbound_session, supports_resume)
    timeout_secs = int(os.environ.get("DSH_TIMEOUT") or DEFAULT_TIMEOUT_SECS)
    try:
        child = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env(),
            text=True,
        )
    except FileNotFoundError:
        emit_error(f"{CLI_NAME} CLI not found. {INSTALL_HINT}")
        return 1
    except OSError as e:
        emit_error(f"{CLI_NAME} failed to start: {e}")
        return 1

    try:
        stdout, stderr = child.communicate(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        child.send_signal(signal.SIGTERM)
        try:
            child.communicate(timeout=KILL_GRACE_SECS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
        emit_error(f"{CLI_NAME} timed out after {timeout_secs}s")
        return 124

    # The id line is bridge bookkeeping, not a user-facing error; stamp it on
    # every outcome (error turns included) so continuity survives failures.
    session_id = parse_session_id(stderr) if supports_resume else ""

    if child.returncode != 0:
        # dsh headless reports errors on stderr with the exit code; prefer
        # that text ("dsh: MISSING_CREDENTIAL: ...") for an actionable message.
        s = SESSION_ID_LINE.sub("", stderr or "").strip()
        msg = f"{CLI_NAME} exited with {child.returncode}"
        if s:
            msg += f": {s[:500]}"
        emit_error(msg, session_id or None)
        return 1

    text = (stdout or "").strip()
    if not text:
        emit_error(f"{CLI_NAME} returned empty output (task completed with no assistant message)", session_id or None)
        return 1
    emit_result(text, session_id or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
