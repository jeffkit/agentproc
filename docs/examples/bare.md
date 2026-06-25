# Bare script (no SDK)

You don't need an SDK. Any script that reads `AGENT_*` env vars and writes to stdout is a valid AgentProc agent.

## Echo agent

::: code-group

```bash [bash]
#!/usr/bin/env bash
echo "You said: $AGENT_MESSAGE"
```

```python [python]
#!/usr/bin/env python3
import os
print(f"You said: {os.environ['AGENT_MESSAGE']}")
```

```js [node]
#!/usr/bin/env node
console.log(`You said: ${process.env.AGENT_MESSAGE}`);
```

:::

## With streaming

```python
#!/usr/bin/env python3
import json, os, sys, time

message = os.environ["AGENT_MESSAGE"]

# Simulate streaming by sending chunks
for i, word in enumerate(message.split()):
    chunk = word + (" " if i < len(message.split()) - 1 else "")
    print(f"AGENT_PARTIAL:{json.dumps(chunk)}", flush=True)
    time.sleep(0.05)
```

## With session continuity

```python
#!/usr/bin/env python3
import json, os, sys, uuid

message = os.environ["AGENT_MESSAGE"]
session_id = os.environ.get("AGENT_SESSION_ID", "")

# Create a new session ID if we don't have one
if not session_id:
    session_id = str(uuid.uuid4())

# Declare our session on the first line
print(f"AGENT_SESSION:{session_id}", flush=True)

# Then write the reply
print(f"[session {session_id[:8]}] You said: {message}")
```

## Profile YAML

```yaml
command: python3 ./my_agent.py   # or bash, node, etc.
timeout_secs: 30
```
