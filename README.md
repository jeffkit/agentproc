# AgentProc

A minimal protocol for connecting any Agent CLI to a messaging platform through a process-based interface.

**[Documentation](https://agentproc.dev/) · **[中文文档](https://agentproc.dev/zh/) · **[Protocol Spec](./spec/protocol.md) · **[Profile Hub](./hub/)**

---

## What is it?

AgentProc defines how a bridge (the platform adapter) talks to an agent process (your script). The interface is intentionally minimal:

- **Input:** one `{"type":"turn",...}` NDJSON line on stdin (message, session id, attachments, …)
- **Output:** NDJSON events on stdout (`{"type":"partial"}`, `{"type":"result"}`, `{"type":"error"}`; optional `session_id` on events)

No HTTP, no sockets. Any process that reads a turn from stdin and writes NDJSON events to stdout is a valid agent.

```
Messaging Platform → Bridge → Your Script → Bridge → User
                       ↑ stdin turn    stdout NDJSON ↑
```

## Profile Hub

Ready-to-use profiles for popular AI CLIs — drop one in and any conformant bridge can drive it. See [`hub/`](./hub/) for the full list:

- **[claude-code](./hub/claude-code/)** (Anthropic) — official, Python + Node
- **[codex](./hub/codex/)** (OpenAI) — official, Python + Node
- **[codebuddy](./hub/codebuddy/)** (Tencent) — official, Python + Node
- **[agy](./hub/agy/)** — community, Python + Node
- **[echo-agent](./hub/echo-agent/)** — minimal hello-world for testing your bridge

## Quick example

```bash
#!/usr/bin/env bash
# echo_agent.sh — reads the turn from stdin, echoes the message back
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"result","text":"You said: '"$message"'"},separators=(",",":"))+"\n")'
```

```yaml
# profile.yaml
command: bash
args: ["./echo_agent.sh"]
timeout_secs: 10
```

```bash
# Test locally
echo '{"type":"turn","message":"hello","session_id":"","protocol_version":"0.4"}' | bash ./echo_agent.sh
# → {"type":"result","text":"You said: hello"}
```

## Streaming + errors + session continuity

```bash
#!/usr/bin/env bash
# A streaming agent with session continuity
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
sid="my-session-$(date +%s)"
printf '{"type":"partial","text":"Let me think... ","session_id":"%s"}\n' "$sid"
printf '{"type":"partial","text":"done.","session_id":"%s"}\n' "$sid"
printf '{"type":"result","text":"","session_id":"%s"}\n' "$sid"
```

If something goes wrong, emit a `{"type":"error"}` event — the bridge forwards it to the user regardless of streaming mode:

```bash
printf '{"type":"error","message":"Upstream API rate limited. Try again in 60s."}\n'
exit 1
```

## Repository structure

```
agentproc/
├── spec/              # Protocol specification (EN + ZH)
├── sdk/
│   ├── python/        # pip install agentproc
│   └── node/          # npm install agentproc — also ships the CLI
├── hub/               # Drop-in profiles for claude/codex/codebuddy/agy
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

The Node.js package also ships the `agentproc` CLI — a canonical bridge runner that drives any profile against a message:

```bash
npm install -g agentproc
agentproc --profile hub/claude-code/profile.yaml --prompt "hello"
```

See the [CLI docs](https://agentproc.dev/cli/) for details.

```python
from agentproc import create_profile

async def handler(ctx):
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

## How it differs from neighboring protocols

- **Not MCP.** MCP connects an LLM app to *tools* (the LLM is the client, tools are subprocesses). AgentProc connects a bridge to an *agent* (reverse direction). They compose: your AgentProc agent can internally use MCP tools.
- **Not ACP.** ACP is an interactive, bidirectional JSON-RPC protocol for IDEs. AgentProc is one process per turn, NDJSON on stdout.
- **Is NDJSON.** AgentProc 0.3 *is* NDJSON both directions: one turn line in, typed event lines out. The vocabulary is closed and small, so a bridge classifies each line in one line of code.

See the [full spec](./spec/protocol.md#comparison-with-related-protocols) for the complete comparison.

## Status

Wire protocol `0.3`, document revision `1.0` (Draft). The on-the-wire contract (stdin turn object + NDJSON stdout events) is stable enough to implement against; the spec document is revised independently and may clarify wording without bumping the wire version. See [CHANGELOG](./CHANGELOG.md).

## License

MIT
