# grok-build

Wraps the [`grok`](https://x.ai/cli) CLI (xAI Grok Build) as an AgentProc agent with full streaming and multi-turn session continuity.

Source: [xai-org/grok-build](https://github.com/xai-org/grok-build) (Apache 2.0). Docs: [docs.x.ai/build](https://docs.x.ai/build/overview).

## Quick test (zero config)

```bash
# 1. Install grok once:
curl -fsSL https://x.ai/cli/install.sh | bash

# 2. Auth either via login OR by exporting a key:
grok login
# or: export XAI_API_KEY="xai-..."

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run grok-build -p "what is this codebase?"
```

No YAML editing. `agentproc hub run` uses your current directory as the agent's `cwd`, and locates the bundled `bridge.py` via `{{PROFILE_DIR}}`.

## Setup (if you can't use `hub run`)

1. Install the `grok` CLI:

   ```bash
   curl -fsSL https://x.ai/cli/install.sh | bash
   ```

2. Authenticate:

   ```bash
   grok login                 # browser, or
   grok login --device-code   # SSH / headless, or
   export XAI_API_KEY="xai-..."
   ```

3. Copy the profile into your project:

   ```bash
   agentproc hub install grok-build
   # or, from a repo checkout:
   cp -r hub/grok-build ./grok-build
   ```

4. Run against the local copy:

   ```bash
   agentproc --profile ./grok-build/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project \
             --env XAI_API_KEY="$XAI_API_KEY"
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory.
timeout_secs: 600
streaming: true
env:
  XAI_API_KEY: "${XAI_API_KEY}"
  # Optional overrides:
  # GROK_MODEL: "grok-4.5"
```

## Local test

```bash
cd ~/projects/my-app
agentproc hub run grok-build -p "say hi in 5 words"
```

Expected output (streaming mode — block-shaped partials, not per-token):

```
{"type":"partial","text":"Cats like to nap in the sun, and chase small toys."}
{"type":"partial","text":" They walk quietly but still get your attention."}
{"type":"result","text":"Cats like to nap in the sun, and chase small toys. They walk quietly but still get your attention.","session_id":"019f691a-769c-7a33-85e2-5b98100b7716"}
```

Note: grok's raw `streaming-json` is near token-sized. The bridge coalesces `text`
events into Claude-like blocks (sentence/paragraph boundaries, or ~40–80 chars)
before emitting `partial`. The session id arrives on the terminal `end` event
(`sessionId`); early partials may omit it until then.

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/grok-build
echo '{"type":"turn","message":"say hi in 5 words","session_id":"","protocol_version":"0.4"}' | python3 bridge.py
```

</details>

## How it works

```
turn.message, turn.session_id
  ↓
bridge.py / bridge.js
  ↓ grok -p <message> --output-format streaming-json --always-approve --no-auto-update [-r <id>]
grok CLI
  ↓ NDJSON stream: text / thought / end / error events
bridge.py / bridge.js
  ↓ coalesce token-sized text → block-sized {"type":"partial"}
  ↓ {"type":"result","text":"<full>","session_id":"<id>"}
  ↓ {"type":"error","message":"..."}
  ↓ exit code from grok
```

The session ID is opaque — `grok` emits it as `sessionId` on the `end` event, and the bridge forwards it as `session_id`. On subsequent turns, your bridge passes it back as `turn.session_id`, and this bridge replays it as `-r <id>`.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `XAI_API_KEY` | yes* | Auth. (*Or run `grok login` / `grok login --device-code` once.) |
| `GROK_MODEL` | no | Model id / alias passed as `-m` |

## Caveats

- `--always-approve` (auto-approve tool calls) is set unconditionally — `grok` would otherwise prompt interactively, which a non-interactive AgentProc bridge can't satisfy. Run the bridge in a sandbox if you're concerned about tool execution.
- `--no-auto-update` is always passed so headless turns don't race a background self-update.
- `thought` events (reasoning tokens) are dropped; only `text` chunks become AgentProc `partial`s.
- Token-sized `text` events are coalesced into block-sized partials (≈ Claude Code assistant blocks): flush on newline / sentence punctuation once ≥40 chars, or hard-flush at 80 chars, and drain the remainder on `end`.
- The official install script also creates an `agent` → `grok` symlink. That **shadows** the Cursor Agent CLI binary named `agent`. If you use hub/cursor, keep `~/.local/bin/agent` → `cursor-agent` and remove any `~/.grok/bin/agent` / `~/.local/bin/agent` link that points at grok.
- `streaming: false` falls back to one-shot mode: the shared runner still concatenates via the last `final_text` / partial fallback; prefer streaming for this CLI.

## License

MIT (this profile). The `grok` CLI is © SpaceXAI / xAI and licensed separately under the Apache License 2.0.
