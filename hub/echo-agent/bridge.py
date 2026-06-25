#!/usr/bin/env python3
"""
Minimal AgentProc echo agent (Python).

Reads AGENT_MESSAGE and writes it back. No external dependencies, no AI calls.
Use this to verify your messaging bridge speaks the protocol correctly.
"""

import os
import sys


def main() -> int:
    message = os.environ.get("AGENT_MESSAGE", "")
    sys.stdout.write(f"You said: {message}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
