# Quick Start

Get an AgentProc-compatible agent running in 5 minutes.

::: tip Two paths — pick one
- **Just want to use a popular AI CLI (claude, codex, codebuddy, …)?** You don't need this page. Go to the [homepage](/) and use `agentproc hub run <name>` — it's zero-config.
- **Want to write your own agent script from scratch?** This page is for you. You'll write a 3-line script, a 2-line profile YAML, and run it through the same `agentproc` CLI.
:::

## Step 1: Write your agent script

The simplest possible agent reads `AGENT_MESSAGE` and writes a reply to stdout.

::: code-group

```bash [bash]
#!/usr/bin/env bash
# echo_agent.sh — replies with whatever the user sent
echo "You said: $AGENT_MESSAGE"
```

```python [python]
#!/usr/bin/env python3
# echo_agent.py
import os
print(f"You said: {os.environ['AGENT_MESSAGE']}")
```

```js [node]
#!/usr/bin/env node
// echo_agent.js
console.log(`You said: ${process.env.AGENT_MESSAGE}`);
```

:::

## Step 2: Create a profile YAML

```yaml
# myagent.yaml
command: bash ./echo_agent.sh
timeout_secs: 10
```

## Step 3: Test it locally with the agentproc CLI

Run your profile through the same CLI the hub uses — this is the most faithful test of what a real bridge will see:

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
# → You said: hello
```

Protocol lines (if any) appear on stderr; the reply body on stdout. The CLI's exit code matches what a bridge would see: `0` success, `1` error, `124` timeout.

<details>
<summary>Prefer to test without the CLI?</summary>

You can also drive the script directly by setting the env vars yourself. This is what the CLI does internally:

```bash
AGENT_MESSAGE="hello" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
bash ./echo_agent.sh
```

Useful when debugging the script in isolation, but for end-to-end behavior prefer the `agentproc --profile ...` form above.
</details>

## Step 4: Connect to a bridge

Point your bridge at the profile YAML. The exact steps depend on which bridge you're using — refer to the bridge's documentation. The [Node SDK's `run()` function](/sdk/node) is the canonical reference for what a bridge does.

---

## Error handling

When something goes wrong, emit an `AGENT_ERROR:` line to send the user a readable message. The bridge forwards it as an error reply and discards any reply body emitted alongside.

::: code-group

```bash [bash]
#!/usr/bin/env bash
# error_agent.sh
if [ -z "$AGENT_MESSAGE" ]; then
  echo 'AGENT_ERROR:"message is required"'
  exit 1
fi
echo "You said: $AGENT_MESSAGE"
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
    throw await protocolError('message is required');
  }
  return { response: `You said: ${ctx.message}` };
});
```

:::

Both the SDK form (raising `ProtocolError` / throwing `protocolError(...)`) and the bare `echo 'AGENT_ERROR:"..."'` form produce the same wire output. Either way, exit non-zero after emitting the line.

---

## Next steps

- [Read the full protocol spec](/spec/) to understand all the features
- [Use an SDK](/sdk/) to skip the boilerplate
- [See examples](/examples/claude) for connecting real AI agents like claude CLI

::: tip Stuck?
See [Troubleshooting](/guide/troubleshooting) for the most common errors (rate limits, `spawn ENOENT`, model-not-found, timeouts) and their exact fixes.
:::
