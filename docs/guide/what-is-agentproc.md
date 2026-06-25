# What is AgentProc?

AgentProc is a minimal protocol for connecting any Agent CLI to a messaging platform through a process-based interface.

## The problem it solves

You have an AI agent — maybe Claude Code, Codex, a custom LLM wrapper, or anything else that runs as a CLI. You want users to talk to it through a messaging app (WeChat, Slack, Telegram, etc.). The glue layer between the two is the hard part:

- How does the message get to the agent?
- How does the reply get back?
- How do you keep conversation context across multiple turns?
- How do you stream responses so users don't wait?

AgentProc answers all of these with the simplest possible interface: **environment variables in, stdout out**.

## How it works

```
Messaging Platform
      │
      ▼
   Bridge                ← reads your profile YAML, manages the process
      │   env vars
      ▼
 Your Script             ← reads AGENT_MESSAGE, does something, writes to stdout
      │   stdout
      ▼
   Bridge                ← forwards the reply to the user
```

The bridge injects context as environment variables before starting your process. Your script reads them, calls whatever AI system it wants, and writes the response to stdout. That's the entire protocol.

## What it is not

- **Not an HTTP API.** There's no server to run or endpoint to implement.
- **Not a framework.** You don't subclass anything or implement interfaces.
- **Not platform-specific.** AgentProc doesn't know about WeChat, Slack, or any specific messaging app.
- **Not opinionated about AI.** You can call Claude, GPT, Gemini, a local model, or a simple rule-based system.

## Comparison with MCP

[MCP (Model Context Protocol)](https://modelcontextprotocol.io) defines how AI models call external tools. AgentProc defines how a messaging platform calls an AI agent. They solve different problems and complement each other — your AgentProc script can internally use MCP tools.
