#!/usr/bin/env python3
"""
Example: connect claude CLI to AgentProc bridge (Python)

This script wraps the `claude` CLI to implement the AgentProc P0 protocol.
The bridge calls this script; it reads AGENT_* env vars, calls claude, and
writes the P0 output to stdout.

Profile YAML:
    command: python3 ./claude_bridge.py
    cwd: /path/to/your/project
    timeout_secs: 600
    streaming: true

Or install agentproc and use create_profile for cleaner code (see below).
"""

import json
import os
import subprocess
import sys


def main():
    message = os.environ["AGENT_MESSAGE"]
    session_id = os.environ.get("AGENT_SESSION_ID", "")
    streaming = os.environ.get("AGENT_STREAMING", "1") != "0"

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
    last_partial = None

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "assistant":
            text = "".join(
                b.get("text", "")
                for b in (event.get("message") or {}).get("content", [])
                if b.get("type") == "text"
            )
            if text.strip() and streaming:
                # Emit streaming chunk
                print(f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}", flush=True)
                last_partial = text

        elif event.get("type") == "result":
            found_session_id = event.get("session_id")
            result_text = event.get("result", "")
            if result_text.strip() and result_text != last_partial:
                if streaming:
                    print(f"AGENT_PARTIAL:{json.dumps(result_text, ensure_ascii=False)}", flush=True)
                else:
                    # Non-streaming: emit session line then full text
                    if found_session_id:
                        print(f"AGENT_SESSION:{found_session_id}", flush=True)
                    print(result_text, flush=True)
                    sys.exit(0)

    proc.wait()

    if proc.returncode != 0 and not found_session_id:
        stderr = proc.stderr.read()
        sys.stderr.write(f"claude exited with {proc.returncode}: {stderr}\n")
        sys.exit(1)

    # Emit session line at the end (streaming mode: all text already sent as partials)
    if found_session_id:
        print(f"AGENT_SESSION:{found_session_id}", flush=True)


if __name__ == "__main__":
    main()
