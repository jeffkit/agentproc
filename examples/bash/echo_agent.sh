#!/usr/bin/env bash
# Minimal AgentProc-compliant agent (Bash, wire 0.3).
# Echoes the user message back — useful for testing your bridge setup.
#
# Profile YAML:
#   command: bash
#   args: ["./echo_agent.sh"]
#   timeout_secs: 10
#
# Wire 0.3: the turn object arrives on stdin as one NDJSON line; the agent
# replies with a single {"type":"text"} event on stdout.
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"text","text":"You said: '"$message"'"},separators=(",",":"))+"\n")'
