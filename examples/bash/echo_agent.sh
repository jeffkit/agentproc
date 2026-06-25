#!/usr/bin/env bash
# Minimal AgentProc-compliant agent (Bash)
# Echoes the user message back — useful for testing your bridge setup.
#
# Profile YAML:
#   command: bash ./echo_agent.sh
#   timeout_secs: 10

echo "You said: $AGENT_MESSAGE"
