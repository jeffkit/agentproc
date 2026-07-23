# SDK Overview

AgentProc SDKs remove the boilerplate of reading the turn object from stdin and writing NDJSON events to stdout, so you can focus on your agent logic.

## Available SDKs

| Language | Package | Install |
|----------|---------|---------|
| Python | `agentproc` | `pip install agentproc` |
| Node.js | `agentproc` | `npm install agentproc` |
| Rust | `agentproc` | `cargo add agentproc` |

## Without an SDK

You don't need an SDK. Any script that reads a `{"type":"turn",...}` object from stdin and writes NDJSON events to stdout works. See [Bare script examples](/examples/bare).

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
const { createProfile } = require('agentproc');

createProfile(async ({ message }) => {
  const reply = await myLLM(message);
  return { response: reply };
});
```

```rust [Rust]
use agentproc::{run, Profile, RunOptions};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let profile = Profile::from_path("profile.yaml")?;
    let result = run(&profile, RunOptions::new("hello")).await?;
    println!("{}", result.reply);
    Ok(())
}
```

:::

- [Python SDK docs](/sdk/python)
- [Node.js SDK docs](/sdk/node)
- [Rust SDK docs](/sdk/rust)
