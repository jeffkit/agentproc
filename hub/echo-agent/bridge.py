#!/usr/bin/env python3
"""
Minimal AgentProc echo agent (Python, wire 0.3).

Reads the {"type":"turn",...} object from stdin and writes the message back
as a single {"type":"text"} event. No external dependencies, no AI calls.
Use this to verify your messaging bridge speaks the protocol correctly.
"""

import json
import sys


def main() -> int:
    try:
        line = sys.stdin.readline()
        turn = json.loads(line.rstrip("\r\n")) if line else {}
    except (json.JSONDecodeError, OSError):
        turn = {}
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    sys.stdout.write(
        json.dumps({"type": "text", "text": f"You said: {message}"}, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
