# Quick Start

Get an AgentProc-compatible agent running in 5 minutes.

::: tip Two paths — pick one
- **Just want to use a popular AI CLI (claude, codex, codebuddy, …)?** You don't need this page. Go to the [homepage](/) and use `agentproc hub run <name>` — it's zero-config.
- **Want to write your own agent script from scratch?** This page is for you. You'll write a tiny script, a 2-line profile YAML, and run it through the same `agentproc` CLI.
:::

## Step 1: Write your agent script

The simplest possible agent reads the `{"type":"turn",...}` object from stdin and writes one NDJSON event to stdout.

::: code-group

```bash [bash]
#!/usr/bin/env bash
# echo_agent.sh — reads the turn from stdin, echoes the message back
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"result","text":"You said: '"$message"'"},separators=(",",":"))+"\n")'
```

```python [python]
#!/usr/bin/env python3
# echo_agent.py
import json, sys
turn = json.loads(sys.stdin.readline() or "{}")
msg = turn.get("message", "") if isinstance(turn.get("message"), str) else ""
sys.stdout.write(json.dumps({"type": "result", "text": f"You said: {msg}"}, separators=(",", ":")) + "\n")
```

```js [node]
#!/usr/bin/env node
// echo_agent.js
const fs = require('node:fs');
const raw = fs.readFileSync(0, 'utf8');
const turn = JSON.parse(raw.split('\n')[0] || '{}');
process.stdout.write(JSON.stringify({ type: 'result', text: `You said: ${turn.message || ''}` }) + '\n');
```

:::

## Step 2: Create a profile YAML

```yaml
# myagent.yaml
command: bash
args: ["./echo_agent.sh"]
timeout_secs: 10
```

## Step 3: Test it locally with the agentproc CLI

Run your profile through the same CLI the hub uses — this is the most faithful test of what a real bridge will see:

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
# → You said: hello
```

NDJSON events (`{"type":"partial"}`, `{"type":"result"}`, `{"type":"error"}`; optional `session_id`) appear on stderr; the reply body (from `{"type":"result"}` / streaming `partial`s) on stdout. The CLI's exit code matches what a bridge would see: `0` success, `1` error, `124` timeout.

<details>
<summary>Prefer to test without the CLI?</summary>

You can also drive the script directly by piping the turn object yourself. This is what the CLI does internally:

```bash
echo '{"type":"turn","message":"hello","session_id":"","protocol_version":"0.4"}' | bash ./echo_agent.sh
```

Useful when debugging the script in isolation, but for end-to-end behavior prefer the `agentproc --profile ...` form above.
</details>

## Step 4: Connect to a bridge

Point your bridge at the profile YAML. The exact steps depend on which bridge you're using — refer to the bridge's documentation. The [Node SDK's `run()` function](/sdk/node) is the canonical reference for what a bridge does.

---

## Error handling

When something goes wrong, emit a `{"type":"error"}` event to send the user a readable message. The bridge forwards it as an error reply and discards any reply body emitted alongside.

::: code-group

```bash [bash]
#!/usr/bin/env bash
# error_agent.sh
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
if [ -z "$message" ]; then
  echo '{"type":"error","message":"message is required"}'
  exit 1
fi
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"result","text":"You said: '"$message"'"},separators=(",",":"))+"\n")'
```

```python [python]
#!/usr/bin/env python3
# error_agent.py
from agentproc import create_profile, ProtocolError

async def handler(ctx):
    if not ctx.message.strip():
        raise ProtocolError("message is required")
    return f"You said: {ctx.message}"

create_profile(handler)
```

```js [node]
#!/usr/bin/env node
// error_agent.js
const { createProfile, protocolError } = require('agentproc');

createProfile(async (ctx) => {
  if (!ctx.message.trim()) {
    throw protocolError('message is required');
  }
  return { response: `You said: ${ctx.message}` };
});
```

:::

Both the SDK form (raising `ProtocolError` / throwing `protocolError(...)`) and the bare `echo '{"type":"error","message":"..."}'` form produce the same wire output. Either way, exit non-zero after emitting the event.

---

## Next steps

- [Read the full protocol spec](/spec/) to understand all the features
- [Use an SDK](/sdk/) to skip the boilerplate
- [See examples](/examples/claude) for connecting real AI agents like claude CLI

::: tip Stuck?
See [Troubleshooting](/guide/troubleshooting) for the most common errors (rate limits, `spawn ENOENT`, model-not-found, timeouts) and their exact fixes.
:::
