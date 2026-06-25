# AgentProc

A minimal protocol for connecting any Agent CLI to a messaging platform through a process-based interface.

**[Documentation](https://agentproc.dev/) · **[中文文档](https://agentproc.dev/zh/) · **[Protocol Spec](./spec/protocol.md)**

---

## What is it?

AgentProc defines how a bridge (the platform adapter) talks to an agent process (your script). The interface is intentionally minimal:

- **Input:** environment variables (`AGENT_MESSAGE`, `AGENT_SESSION_ID`, …)
- **Output:** stdout lines (`AGENT_SESSION:`, `AGENT_PARTIAL:`, `AGENT_ERROR:`, reply text)

No HTTP, no sockets. Any process that reads env vars and writes to stdout is a valid agent.

```
Messaging Platform → Bridge → Your Script → Bridge → User
                       ↑ env vars    stdout ↑
```

## Quick example

```bash
#!/usr/bin/env bash
# echo_agent.sh
echo "You said: $AGENT_MESSAGE"
```

```yaml
# profile.yaml
command: bash ./echo_agent.sh
timeout_secs: 10
```

```bash
# Test locally
AGENT_MESSAGE="hello" bash ./echo_agent.sh
# → You said: hello
```

## Streaming + errors + session continuity

```bash
#!/usr/bin/env bash
# A streaming agent with session continuity
echo "AGENT_SESSION:my-session-$(date +%s)"
echo 'AGENT_PARTIAL:"Let me think... "'
echo 'AGENT_PARTIAL:"done."'
```

If something goes wrong, emit an `AGENT_ERROR:` line — the bridge forwards it to the user regardless of streaming mode:

```bash
echo 'AGENT_ERROR:"Upstream API rate limited. Try again in 60s."'
exit 1
```

## Repository structure

```
agentproc/
├── spec/              # Protocol specification (EN + ZH)
├── sdk/
│   ├── python/        # pip install agentproc
│   └── node/          # npm install agentproc
├── examples/          # Ready-to-use agent scripts
│   ├── python/
│   ├── node/
│   └── bash/
└── docs/              # VitePress documentation site
```

## SDKs

| Language | Package | Install |
|----------|---------|---------|
| Python | `agentproc` | `pip install agentproc` |
| Node.js | `agentproc` | `npm install agentproc` |

```python
from agentproc import create_profile

async def handler(ctx):
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

## How it differs from neighboring protocols

- **Not MCP.** MCP connects an LLM app to *tools* (the LLM is the client, tools are subprocesses). AgentProc connects a bridge to an *agent* (reverse direction). They compose: your AgentProc agent can internally use MCP tools.
- **Not ACP.** ACP is an interactive, bidirectional JSON-RPC protocol for IDEs. AgentProc is one process per turn, plain text on stdout.
- **Not NDJSON.** NDJSON requires every line to be valid JSON. AgentProc uses sentinel-prefixed lines so `echo "You said: $AGENT_MESSAGE"` is a valid agent.

See the [full spec](./spec/protocol.md#comparison-with-related-protocols) for the complete comparison.

## Status

v0.1.0 — Draft. The protocol is stable enough to implement against, but expect refinements as real bridges and agents are built. See [CHANGELOG](./CHANGELOG.md).

## License

MIT
