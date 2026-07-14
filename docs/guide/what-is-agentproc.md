# What is AgentProc?

AgentProc is a minimal protocol for connecting any Agent CLI to a messaging platform through a process-based interface.

## The problem it solves

You have an AI agent — maybe Claude Code, Codex, a custom LLM wrapper, or anything else that runs as a CLI. You want users to talk to it through a messaging app (WeChat, Slack, Telegram, etc.). The glue layer between the two is the hard part:

- How does the message get to the agent?
- How does the reply get back?
- How do you keep conversation context across multiple turns?
- How do you stream responses so users don't wait?

AgentProc answers all of these with the simplest possible interface: **a turn object in on stdin, NDJSON events out on stdout**.

## How it works

```
Messaging Platform
      │
      ▼
   Bridge                ← reads your profile YAML, spawns the agent process
      │   one {"type":"turn",...} line on stdin
      ▼
 Your Script             ← reads the turn, does something, writes NDJSON events
      │   one JSON object per line on stdout
      ▼
   Bridge                ← forwards the reply to the user
```

The bridge writes a single `{"type":"turn",...}` line to the agent's stdin (message, session id, attachments, …), then closes stdin. Your script reads that line, calls whatever AI system it wants, and writes the response as NDJSON events (`{"type":"partial"}` to stream, `{"type":"result"}` for the final body with optional `session_id`). That's the entire protocol.

## What it is not

- **Not an HTTP API.** There's no server to run or endpoint to implement.
- **Not a framework.** You don't subclass anything or implement interfaces.
- **Not platform-specific.** AgentProc doesn't know about WeChat, Slack, or any specific messaging app.
- **Not opinionated about AI.** You can call Claude, GPT, Gemini, a local model, or a simple rule-based system.

## Comparison with related protocols

AgentProc occupies a specific niche. The neighboring protocols are similar in *shape* (subprocess + stdio) but different in *purpose*.

### MCP — Model Context Protocol

[MCP](https://modelcontextprotocol.io) connects an LLM application to **tools and data sources** over JSON-RPC. The direction is **reversed**: in MCP, the AI is the client and the tool provider is the subprocess; in AgentProc, the bridge is the client and the AI wrapper is the subprocess. They compose naturally — your AgentProc agent may internally use MCP tools.

### ACP — Agent Client Protocol

[ACP](https://agentclientprotocol.com) (Zed Industries) connects a code editor to an AI coding agent over long-lived, bidirectional JSON-RPC. It assumes an interactive IDE session with tool calls, file diffs, and mode switching. AgentProc assumes a single chat turn per process invocation. Use ACP if you're building an IDE; use AgentProc if you're bridging a chat bot to a CLI.

### NDJSON / JSON Lines

[NDJSON](https://jsonlines.org) is one JSON object per line — the wire format used internally by Claude Code, Codex, Gemini CLI streaming, and MCP. AgentProc 0.4 *is* NDJSON both directions: the turn arrives as one NDJSON line on stdin, and every line the agent emits is a typed JSON event (`partial`, `result`, `error`; optional `session_id` on events). The vocabulary is closed and small, so a bridge can classify each line in one line of code. The cost is that every emitted line must be valid JSON — a bare `echo "You said: hi"` is no longer a valid agent; the SDKs absorb that boilerplate for you.

### What AgentProc is *not*

- **Not a bot framework.** Hubot, Errbot, BotKit, and Microsoft Bot Framework operate on the *consumer* side of the bridge (in-process adapters, HTTP connectors). AgentProc defines the contract *between* the bridge and the agent, and is orthogonal to those frameworks.
- **Not an agent-to-agent protocol.** A2A / AGNTCY solve a different problem (agents talking to each other).
- **Not an IDE protocol.** Use ACP for that.
- **Not a tool protocol.** Use MCP for that.

See the [full spec](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.md) for design rationale and additional comparisons with SSE and LSP.
