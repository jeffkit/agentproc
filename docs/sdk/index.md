# SDK Overview

AgentProc SDKs remove the boilerplate of reading env vars and writing the stdout protocol, so you can focus on your agent logic.

## Available SDKs

| Language | Package | Install |
|----------|---------|---------|
| Python | `agentproc` | `pip install agentproc` |
| Node.js | `@agentproc/sdk` | `npm install @agentproc/sdk` |

## Without an SDK

You don't need an SDK. Any script that reads `AGENT_*` env vars and writes to stdout works. See [Bare script examples](/examples/bare).

## With an SDK

Write one async function. The SDK handles the rest.

::: code-group

```python [Python]
from agentproc import create_profile

async def handler(ctx):
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

```js [Node.js]
const { createProfile } = require('@agentproc/sdk');

createProfile(async ({ message }) => {
  const reply = await myLLM(message);
  return { response: reply };
});
```

:::
