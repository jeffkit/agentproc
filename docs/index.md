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

::: tip macOS users: `pip`/`pipx` not found?
Homebrew's Python ships without `pip` exposed. Either run `python3 -m ensurepip && python3 -m pip install --user pipx`, or just use the `npm` tab above — Node is required for the `agentproc` CLI anyway (it ships from the npm package).
:::

Verify it works:

```bash
agentproc --version
# agentproc 0.3.0 (protocol 0.1)
```

## ② Browse the hub

```bash
agentproc hub list
#   claude-code   official    Connect the claude CLI (Anthropic) as an AgentProc agent
#   codex         official    Connect the codex CLI (OpenAI) as an AgentProc agent
#   codebuddy     official    Connect the codebuddy CLI (Tencent) as an AgentProc agent
#   agy           community   Connect the agy CLI as an AgentProc agent
#   echo-agent    official    Minimal hello-world agent
```

The [Profile Hub](/hub/) is a curated set of drop-in profiles for popular AI CLIs. No clone, no copy, no YAML editing — the CLI fetches them from GitHub on first use and caches them at `~/.agentproc/cache/hub/<name>/` (24h TTL).

::: tip Hit a GitHub rate limit?
Anonymous fetches are capped at ~60/hour. Raise it to 5,000/hour with a token:

```bash
export GITHUB_TOKEN=$(gh auth token)   # or any personal access token
```

If you'd rather skip the network entirely, run against a local checkout: `agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"`.
:::

## ③ Run it in one line

Start with the smoke test (no API key needed):

```bash
agentproc hub run echo-agent -p "hello"
# → You said: hello
```

Then go real. With `claude-code`, you get streaming output and multi-turn session continuity:

```bash
cd ~/projects/my-app          # the agent runs against whatever dir you're in
agentproc hub run claude-code \
  -p "what is this codebase?" \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
```

::: tip No need to edit any profile YAML
`agentproc hub run` automatically uses **your current directory** as the agent's `cwd`, and locates the bundled bridge script via a `{{PROFILE_DIR}}` placeholder. Just `cd` into the project you want the agent to work on, and run.
:::

You'll see protocol lines stream on stderr in real time, and the final reply on stdout:

```
AGENT_PARTIAL:"This codebase is..."
AGENT_SESSION:13c2f6ec-1f97-42c4-be9e-9475129e243c
agentproc:session:13c2f6ec-1f97-42c4-be9e-9475129e243c
```

Capture that session id and continue the conversation:

```bash
agentproc hub run claude-code \
  -p "tell me about the auth module" \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --session 13c2f6ec-1f97-42c4-be9e-9475129e243c
```

::: tip Short replies may not show AGENT_PARTIAL:
Some agents emit the whole reply in one shot when the answer is short — you'll see just `AGENT_SESSION:` and the reply body, no `AGENT_PARTIAL:` lines. That's normal; streaming only fragments longer replies.
:::

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
- **[Troubleshooting](/guide/troubleshooting)** — stuck? Common errors and their fixes

</div>
