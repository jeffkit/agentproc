#!/usr/bin/env python3
"""
Example: connect claude CLI to an AgentProc bridge (Python, wire 0.4).

This script wraps the `claude` CLI to implement the AgentProc P0 protocol.
The bridge spawns this script, writes a {"type":"turn",...} object to its
stdin, and reads NDJSON events from its stdout.

Profile YAML:
    command: python3
    args: ["./claude_bridge.py"]
    cwd: /path/to/your/project
    timeout_secs: 600
    streaming: true

Or install agentproc and use create_profile for cleaner code (see the SDK
guide). This bare script shows the raw wire format with no SDK dependency.
"""

import json
import subprocess
import sys


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    try:
        turn = json.loads(sys.stdin.readline().rstrip("\r\n") or "{}")
    except (json.JSONDecodeError, OSError):
        turn = {}
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    session_id = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""

    args = [
        "claude",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--disallowed-tools", "AskUserQuestion",
        "-p", message,
    ]
    if session_id:
        args += ["--resume", session_id]

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    found_session_id = None
    last_final = None
    last_partial = None
    error_message = None

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                found_session_id = sid

        elif event.get("type") == "assistant":
            text = "".join(
                b.get("text", "")
                for b in (event.get("message") or {}).get("content", [])
                if b.get("type") == "text"
            )
            if text:
                partial = {"type": "partial", "text": text}
                if found_session_id:
                    partial["session_id"] = found_session_id
                emit(partial)
                last_partial = text

        elif event.get("type") == "result":
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                found_session_id = sid
            if event.get("is_error"):
                error_message = event.get("result", "claude reported an error")
            else:
                result_text = event.get("result", "")
                if result_text:
                    last_final = result_text

    proc.wait()

    if error_message:
        err = {"type": "error", "message": error_message}
        if found_session_id:
            err["session_id"] = found_session_id
        emit(err)
        sys.exit(1)

    if proc.returncode != 0 and not found_session_id:
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        emit({"type": "error", "message": f"claude exited with {proc.returncode}: {stderr[:500]}"})
        sys.exit(1)

    reply = last_final if last_final is not None else last_partial
    out = {"type": "result", "text": reply or ""}
    if found_session_id:
        out["session_id"] = found_session_id
    emit(out)
    sys.exit(0)


if __name__ == "__main__":
    main()
