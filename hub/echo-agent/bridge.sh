#!/usr/bin/env bash
# Minimal AgentProc echo agent (Bash, wire 0.4).
# Reads the {"type":"turn",...} object from stdin and writes the message back
# as a single {"type":"result"} event. Uses python3 for JSON handling.
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"result","text":"You said: '"$message"'"},separators=(",",":"))+"\n")'
