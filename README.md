# AgentProc

A minimal protocol for connecting any Agent CLI to a messaging platform through a process-based interface.

**[Documentation](https://jeffkit.github.io/agentproc)** · **[中文文档](https://jeffkit.github.io/agentproc/zh/)** · **[Protocol Spec](./spec/protocol.md)**

---

## What is it?

AgentProc defines how a bridge (the platform adapter) talks to an agent process (your script). The interface is intentionally minimal:

- **Input:** environment variables (`AGENT_MESSAGE`, `AGENT_SESSION_ID`, …)
- **Output:** stdout lines (`AGENT_SESSION:`, `AGENT_PARTIAL:`, reply text)

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

## Repository structure

```
agentproc/
├── spec/              # Protocol specification (EN + ZH)
├── sdk/
│   ├── python/        # pip install agentproc
│   └── node/          # npm install @agentproc/sdk
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
| Node.js | `@agentproc/sdk` | `npm install @agentproc/sdk` |

## License

MIT
