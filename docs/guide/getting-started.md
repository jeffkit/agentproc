# Quick Start

Get an AgentProc-compatible agent running in 5 minutes.

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

## Step 3: Test it locally

You can test your agent without a running bridge by setting the env vars manually:

```bash
AGENT_MESSAGE="hello" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
bash ./echo_agent.sh
```

Expected output:

```
You said: hello
```

## Step 4: Connect to a bridge

Point your bridge at the profile YAML. The exact steps depend on which bridge you're using — refer to the bridge's documentation.

---

## Next steps

- [Read the full protocol spec](/spec/) to understand all the features
- [Use an SDK](/sdk/) to skip the boilerplate
- [See examples](/examples/) for connecting real AI agents like claude CLI
