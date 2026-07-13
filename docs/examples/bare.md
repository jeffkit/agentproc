# Bare script (no SDK)

You don't need an SDK. Any script that reads a `{"type":"turn",...}` object from stdin and writes NDJSON events to stdout is a valid AgentProc agent.

## Echo agent

::: code-group

```bash [bash]
#!/usr/bin/env bash
# Wire 0.3: the turn arrives on stdin as one NDJSON line.
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"text","text":"You said: '"$message"'"},separators=(",",":"))+"\n")'
```

```python [python]
#!/usr/bin/env python3
import json, sys
turn = json.loads(sys.stdin.readline() or "{}")
msg = turn.get("message", "") if isinstance(turn.get("message"), str) else ""
sys.stdout.write(json.dumps({"type": "text", "text": f"You said: {msg}"}, separators=(",", ":")) + "\n")
```

```js [node]
#!/usr/bin/env node
const fs = require('node:fs');
const raw = fs.readFileSync(0, 'utf8');
const turn = JSON.parse(raw.split('\n')[0] || '{}');
process.stdout.write(JSON.stringify({ type: 'text', text: `You said: ${turn.message || ''}` }) + '\n');
```

:::

## With streaming

Emit `{"type":"partial"}` events as chunks arrive. The runner forwards them in real time when the profile's `streaming: true`.

```python
#!/usr/bin/env python3
import json, sys, time

turn = json.loads(sys.stdin.readline() or "{}")
message = turn.get("message", "") if isinstance(turn.get("message"), str) else ""

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for i, word in enumerate(message.split()):
    chunk = word + (" " if i < len(message.split()) - 1 else "")
    emit({"type": "partial", "text": chunk})
    time.sleep(0.05)
```

If all content was delivered via `partial` events, emit no `{"type":"text"}` event — the runner treats an empty reply as "already delivered".

## With session continuity

Emit a `{"type":"session"}` event to declare an id; the bridge passes it back as `session_id` on the next turn.

```python
#!/usr/bin/env python3
import json, sys, uuid

turn = json.loads(sys.stdin.readline() or "{}")
message = turn.get("message", "") if isinstance(turn.get("message"), str) else ""
session_id = turn.get("session_id", "") if isinstance(turn.get("session_id"), str) else ""

if not session_id:
    session_id = str(uuid.uuid4())

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

emit({"type": "session", "id": session_id})
emit({"type": "text", "text": f"[session {session_id[:8]}] You said: {message}"})
```

## Profile YAML

```yaml
command: python3                     # or bash, node, …
args: ["./my_agent.py"]
timeout_secs: 30
```
