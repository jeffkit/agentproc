#!/usr/bin/env python3
"""
AgentProc bridge for the DeepSeek Harness CLI `dsh` (wire 0.4).

JSON mode (dsh >= 0.1.6-alpha.1, feature-detected via `--profile headless
--help` advertising --json) runs `dsh --profile headless --json "<task>"` and
translates the newline-delimited run-event stream:

    {"type":"session","sessionId":...}      opening frame — session id source
    {"type":"status","phase":"step_end","usage":{...}}
                                            per-step token counts in DISJOINT
                                            buckets; the bridge sums steps
    {"type":"text"|"thinking","text":...}   committed assistant content
    {"type":"final","text":...}             terminal answer — always written,
                                            even when the turn ends in error
    {"type":"error","message":...}          driver failure — no final follows

The exit code separates completed (0) from error-ended (1) turns, but a
SIGTERM'd dsh also exits 0 (launcher supervisor semantics), so the bridge
trusts frames, not the exit code.

Session continuity (JSON mode with --session-id advertised): session_id is
stamped from the opening frame and later turns resume the persisted Session.
Adoption is strict upstream (same cwd, no subagent/fork, no agent preset);
mismatches surface as error events.

Plain fallback (older dsh): `dsh --profile headless "<task>"` prints the final
assistant message to stdout; errors go to stderr with a non-zero exit code.
Stateless on the wire.

Usage mapping: dsh buckets are disjoint (billed input = inputTokens +
cacheReadTokens + cacheWriteTokens); agentproc's `input_tokens` is the
inclusive billed input, so the cache buckets fold into it.

`thinking` frames stay off the wire (reasoning projection, same posture as
the claude-code profile, which forwards text deltas only).

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
import threading

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import emit_error, emit_partial, emit_result  # noqa: E402

CLI_NAME = "dsh"
INSTALL_HINT = "Install: npm install -g @deepseek-ai/dsh"
DEFAULT_TIMEOUT_SECS = 1800
KILL_GRACE_SECS = 5
#: Wire rules: non-empty, no path separators or control characters.
SESSION_ID_RE = re.compile(r"^[^\s/\\\x00-\x1f]+$")


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


def detect_support(help_text: str) -> dict:
    """Read feature support off the headless app's own --help text."""
    text = help_text or ""
    json_mode = "--json" in text
    return {"json_mode": json_mode, "session_resume": json_mode and "--session-id" in text}


def build_args(task: str, session_id: str, support: dict) -> list[str]:
    """dsh argv. JSON mode always requests the event stream (and `--` so a
    task starting with `-` stays a positional); with session support and an
    inbound id, adopt that Session."""
    args = [CLI_NAME, "--profile", "headless"]
    if support["json_mode"]:
        args.append("--json")
        if support["session_resume"] and session_id:
            args += ["--session-id", session_id]
        args += ["--", task]
    else:
        args.append(task)
    return args


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def add_step(total: dict | None, usage) -> dict | None:
    """Sum one step_end frame's usage buckets into the turn accumulator.
    First reported step is adopted wholesale; afterwards buckets stay disjoint
    per step and an optional bucket is kept only while every step reports it."""
    if not isinstance(usage, dict):
        return total
    input_t = _num(usage.get("inputTokens"))
    output_t = _num(usage.get("outputTokens"))
    if input_t is None and output_t is None:
        return total
    if not total:
        return {
            "inputTokens": input_t or 0,
            "outputTokens": output_t or 0,
            "totalTokens": _num(usage.get("totalTokens")),
            "cacheReadTokens": _num(usage.get("cacheReadTokens")),
            "cacheWriteTokens": _num(usage.get("cacheWriteTokens")),
            "reasoningTokens": _num(usage.get("reasoningTokens")),
        }

    def _sum(a, b):
        return None if a is None or b is None else a + b

    return {
        "inputTokens": (total.get("inputTokens") or 0) + (input_t or 0),
        "outputTokens": (total.get("outputTokens") or 0) + (output_t or 0),
        "totalTokens": _sum(total.get("totalTokens"), _num(usage.get("totalTokens"))),
        "cacheReadTokens": _sum(total.get("cacheReadTokens"), _num(usage.get("cacheReadTokens"))),
        "cacheWriteTokens": _sum(total.get("cacheWriteTokens"), _num(usage.get("cacheWriteTokens"))),
        "reasoningTokens": _sum(total.get("reasoningTokens"), _num(usage.get("reasoningTokens"))),
    }


def to_usage(u: dict | None) -> dict | None:
    """Map the summed dsh buckets onto agentproc's recommended usage keys.
    dsh counts input/cache buckets disjointly; agentproc's `input_tokens` is
    the inclusive billed input, so the cache buckets fold into it."""
    if not isinstance(u, dict):
        return None
    out: dict = {}
    if _num(u.get("inputTokens")) is not None:
        out["input_tokens"] = u["inputTokens"] + (u.get("cacheReadTokens") or 0) + (u.get("cacheWriteTokens") or 0)
    if _num(u.get("outputTokens")) is not None:
        out["output_tokens"] = u["outputTokens"]
    if _num(u.get("totalTokens")) is not None:
        out["total_tokens"] = u["totalTokens"]
    if _num(u.get("cacheReadTokens")) is not None:
        out["cache_read_input_tokens"] = u["cacheReadTokens"]
    if _num(u.get("cacheWriteTokens")) is not None:
        out["cache_creation_input_tokens"] = u["cacheWriteTokens"]
    if _num(u.get("reasoningTokens")) is not None:
        out["reasoning_tokens"] = u["reasoningTokens"]
    return out or None


_support_cache: dict | None = None


def probe_support() -> dict:
    """Probe (once per process) via the headless app's --help output."""
    global _support_cache
    if _support_cache is None:
        try:
            r = subprocess.run(
                [CLI_NAME, "--profile", "headless", "--help"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            _support_cache = detect_support((r.stdout or "") + (r.stderr or ""))
        except Exception:
            _support_cache = detect_support("")
    return _support_cache


def _auto_approve_enabled() -> bool:
    """AGENTPROC_AUTO_APPROVE=0 / false disables the injected auto-approve default.

    Read from the bridge's own process environment (spec: a process-side knob,
    not a profile field) — pass it through the profile `env:` block or `--env`.
    """
    return os.environ.get("AGENTPROC_AUTO_APPROVE", "").strip().lower() not in ("0", "false")


def child_env() -> dict:
    """Unattended default: auto-approve tools (see bridge.js for rationale).

    The default is skipped entirely under AGENTPROC_AUTO_APPROVE=0, leaving
    dsh's own "ask" default in place (fail-closed).
    """
    env = dict(os.environ)
    for key in ("DEEPSEEK_API_KEY", "DSH_PERMISSION_MODE", "DSH_TOOLS_MODE"):
        if env.get(key) == "":
            del env[key]
    if not env.get("DSH_PERMISSION_MODE") and _auto_approve_enabled():
        env["DSH_PERMISSION_MODE"] = "danger-full-access"
    return env


def _stderr_hint(stderr: str) -> str:
    """First actionable line of a dsh stderr diagnostic ("dsh: CODE: msg")."""
    s = (stderr or "").strip()
    if not s:
        return ""
    return re.sub(r"^dsh:\s*", "", s, count=1)[:500]


def run_json(child: subprocess.Popen, timeout_secs: int) -> dict:
    """Translate the run-event stream; returns the finish_json input dict."""
    stderr_buf: list[str] = []

    def _drain_stderr():
        stderr_buf.append(child.stderr.read() or "")

    threading.Thread(target=_drain_stderr, daemon=True).start()

    state = {"timed_out": False}

    def _on_timeout():
        state["timed_out"] = True
        child.send_signal(signal.SIGTERM)

    timer = threading.Timer(timeout_secs, _on_timeout)
    timer.start()

    session_id = ""
    final_text = None
    error_msg = None
    usage = None
    assert child.stdout is not None
    for raw in child.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(frame, dict):
            continue
        ftype = frame.get("type")
        if ftype == "session":
            sid = frame.get("sessionId")
            if isinstance(sid, str) and SESSION_ID_RE.match(sid):
                session_id = sid
        elif ftype == "status":
            if frame.get("phase") == "step_end":
                usage = add_step(usage, frame.get("usage"))
        elif ftype == "text":
            text = frame.get("text")
            if isinstance(text, str) and text:
                emit_partial(text, session_id or None)
        elif ftype == "final":
            text = frame.get("text")
            if isinstance(text, str):
                final_text = text
        elif ftype == "error":
            msg = frame.get("message")
            if isinstance(msg, str) and msg:
                error_msg = msg
        # thinking / tool_call / tool_result stay off the wire

    child.wait()
    timer.cancel()
    if state["timed_out"] and child.poll() is None:
        child.kill()
        child.wait()
    if child.stderr:
        child.stderr.close()

    return {
        "code": child.returncode,
        "stderr": stderr_buf[0] if stderr_buf else "",
        "session_id": session_id,
        "final_text": final_text,
        "error_msg": error_msg,
        "usage": to_usage(usage),
        "timed_out": state["timed_out"],
    }


def finish_json(outcome: dict, timeout_secs: int) -> int:
    """Frame-driven outcome: success requires a `final` frame AND exit 0."""
    if outcome["timed_out"]:
        emit_error(f"{CLI_NAME} timed out after {timeout_secs}s", outcome["session_id"] or None, outcome["usage"])
        return 124
    if outcome["error_msg"]:
        emit_error(outcome["error_msg"], outcome["session_id"] or None, outcome["usage"])
        return 1
    if outcome["final_text"] is not None:
        if outcome["code"] == 0:
            emit_result(outcome["final_text"], outcome["session_id"] or None, outcome["usage"])
            return 0
        # The turn ended in an error reason after committing text; stderr has
        # the actionable "dsh: CODE: message" diagnostic.
        hint = _stderr_hint(outcome["stderr"])
        msg = f"{CLI_NAME}: {hint}" if hint else f"{CLI_NAME} turn ended with an error (exit {outcome['code']})"
        emit_error(msg, outcome["session_id"] or None, outcome["usage"])
        return 1
    # No final: killed mid-run (dsh maps SIGTERM to exit 0) or crashed early.
    msg = f"{CLI_NAME} exited with {outcome['code']} without a final message"
    hint = _stderr_hint(outcome["stderr"])
    if hint:
        msg += f": {hint}"
    emit_error(msg, outcome["session_id"] or None, outcome["usage"])
    return 1


def finish_plain(child: subprocess.Popen, timeout_secs: int) -> int:
    """Plain fallback: stateless one-shot, exactly the pre-0.1.6 behavior."""
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

    if child.returncode != 0:
        msg = f"{CLI_NAME} exited with {child.returncode}"
        hint = _stderr_hint(stderr)
        if hint:
            msg += f": {hint}"
        emit_error(msg)
        return 1
    text = (stdout or "").strip()
    if not text:
        emit_error(f"{CLI_NAME} returned empty output (task completed with no assistant message)")
        return 1
    emit_result(text)
    return 0


def main() -> int:
    turn = read_turn()
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    inbound_session = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""
    atts = turn.get("attachments")
    has_att = isinstance(atts, list) and len(atts) > 0
    if not message and not has_att:
        emit_error("turn.message is required (or include turn.attachments)")
        return 1

    support = probe_support()
    args = build_args(compose_task(message, turn), inbound_session, support)
    timeout_secs = int(os.environ.get("DSH_TIMEOUT") or DEFAULT_TIMEOUT_SECS)
    try:
        child = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        emit_error(f"{CLI_NAME} CLI not found. {INSTALL_HINT}")
        return 1
    except OSError as e:
        emit_error(f"{CLI_NAME} failed to start: {e}")
        return 1

    if support["json_mode"]:
        return finish_json(run_json(child, timeout_secs), timeout_secs)
    return finish_plain(child, timeout_secs)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(1)
