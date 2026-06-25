---
layout: home

hero:
  name: AgentProc
  text: Connect any Agent CLI to any messaging platform
  tagline: A minimal process-based protocol. No HTTP, no sockets — just stdin, stdout, and env vars.
  actions:
    - theme: brand
      text: Quick Start
      link: /guide/getting-started
    - theme: alt
      text: Read the Spec
      link: /spec/

features:
  - icon: 🚀
    title: 5-minute setup
    description: Install the CLI, point it at a hub profile, talk to your agent. No servers to run, no frameworks to learn.
  - icon: 🤖
    title: Works with claude, codex, codebuddy
    description: Drop-in profiles for the AI CLIs you already use. Pick one from the Profile Hub, run a single command.
  - icon: 🔌
    title: Any messaging platform
    description: WeChat, Slack, Telegram, Discord — the bridge adapts AgentProc to wherever your users are.
  - icon: 📜
    title: Open spec, zero lock-in
    description: A 1-page protocol with reference implementations in Node and Python. Read it in 5 minutes, implement it in an afternoon.
---

<div class="get-started">

# Get started in 5 minutes

## ① Install the CLI

::: code-group

```bash [npm]
npm install -g agentproc
```

```bash [pipx]
pipx install agentproc
```

```bash [pip]
pip install agentproc
```

:::

Verify it works:

```bash
agentproc --version
# agentproc 0.2.0 (protocol 0.1)
```

## ② Pick a profile

Browse the [Profile Hub](/hub/) — every profile is a directory containing `profile.yaml`, a bridge script, and a README. Five official profiles to start with:

| Profile | CLI | Status |
|---------|-----|--------|
| [claude-code](https://github.com/jeffkit/agentproc/tree/main/hub/claude-code) | `claude` (Anthropic) | official |
| [codex](https://github.com/jeffkit/agentproc/tree/main/hub/codex) | `codex` (OpenAI) | official |
| [codebuddy](https://github.com/jeffkit/agentproc/tree/main/hub/codebuddy) | `codebuddy` (Tencent) | official |
| [agy](https://github.com/jeffkit/agentproc/tree/main/hub/agy) | `agy` | community |
| [echo-agent](https://github.com/jeffkit/agentproc/tree/main/hub/echo-agent) | (hello world) | official |

## ③ Run it

Clone the repo and try `echo-agent` first (no API key needed):

```bash
git clone https://github.com/jeffkit/agentproc
cd agentproc

agentproc --profile hub/echo-agent/profile.yaml \
          --prompt "hello" \
          --cwd hub/echo-agent
# → You said: hello
```

Now a real one. With `claude-code`, you get streaming output and multi-turn session continuity:

```bash
agentproc --profile hub/claude-code/profile.yaml \
          --prompt "what is this codebase?" \
          --cwd ~/projects/my-app \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
```

You'll see protocol lines stream on stderr in real time, and the final reply on stdout:

```
AGENT_PARTIAL:"This codebase is..."
AGENT_SESSION:13c2f6ec-1f97-42c4-be9e-9475129e243c
agentproc:session:13c2f6ec-1f97-42c4-be9e-9475129e243c
```

Capture that session id and continue the conversation:

```bash
agentproc --profile hub/claude-code/profile.yaml \
          --prompt "tell me about the auth module" \
          --cwd ~/projects/my-app \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
          --session 13c2f6ec-1f97-42c4-be9e-9475129e243c
```

## ④ Connect to your messaging platform

AgentProc agents don't talk to WeChat or Slack directly — that's the **bridge's** job. The bridge is a small program that:

1. Receives a message from the messaging platform (via webhook, polling, etc.)
2. Spawns your agent with `AGENT_MESSAGE` env var set
3. Reads the agent's stdout (per the AgentProc protocol)
4. Forwards the reply back to the user

Here's a complete working bridge in ~30 lines of Node.js that wires `agentproc` to anything:

```js
// bridge.js — a minimal AgentProc bridge
const { run } = require('agentproc');
const fs = require('fs');

async function handleMessage(message, sessionId) {
  const profile = JSON.parse(fs.readFileSync('./profile.json'));
  const result = await run(profile, {
    message,
    sessionId,
    onPartial: (chunk) => console.log(`[streaming] ${chunk}`),
  });
  console.log(`Reply: ${result.reply}`);
  console.log(`Session: ${result.sessionId}`);  // pass this back next turn
}

// Replace with your platform's SDK:
// yourMessagingPlatform.onMessage(handleMessage);
handleMessage(process.argv[2] || 'hello', '');
```

Save as `bridge.js`, point it at a profile, and wire it to your messaging platform's webhook. The [`runner.js` source](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js) is the spec in code form — read it as the canonical reference.

## Where to go next

- **[Read the protocol spec](/spec/)** — 1 page, defines everything
- **[Profile Hub](/hub/)** — drop-in profiles for popular CLIs
- **[CLI reference](/cli/)** — every flag and option
- **[Python SDK](/sdk/python) / [Node SDK](/sdk/node)** — embed AgentProc in your bridge
- **[Examples](/examples/)** — claude_code bridge, bare scripts, more

</div>
