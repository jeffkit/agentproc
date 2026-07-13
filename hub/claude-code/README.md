# claude-code

Wraps the [`claude`](https://docs.claude.com/en/docs/claude-code) CLI (Anthropic Claude Code) as an AgentProc agent with full streaming and multi-turn session continuity.

## Quick test (zero config)

```bash
# 1. Install claude once:
npm install -g @anthropic-ai/claude-code

# 2. Auth either via claude's own login OR by exporting a key:
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run claude-code -p "what is this codebase?"
```

No YAML editing. `agentproc hub run` uses your current directory as the agent's `cwd`, and locates the bundled `bridge.py` via `{{PROFILE_DIR}}` — so `claude` runs against your project, no matter where you invoke from.

## Setup (if you can't use `hub run`)

If you prefer to keep a local copy of the profile (e.g. offline, custom edits, or a non-hub bridge):

1. Install the `claude` CLI:

   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. Authenticate:

   ```bash
   claude setup-token        # or set ANTHROPIC_API_KEY in your environment
   ```

3. Copy the profile into your project:

   ```bash
   agentproc hub install claude-code    # creates ./claude-code/
   # or, from a repo checkout:
   cp -r hub/claude-code ./claude-code
   ```

4. Run against the local copy. Use `--cwd` to tell `claude` which project to work on:

   ```bash
   agentproc --profile ./claude-code/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project \
             --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory.
#       With --profile, pass --cwd explicitly.
timeout_secs: 600
streaming: true
env:
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
  # Optional overrides:
  # CLAUDE_MODEL: "sonnet"            # any alias or full name claude accepts
  # CLAUDE_DISALLOW_TOOLS: "AskUserQuestion,WebSearch"
```

## Local test

```bash
cd ~/projects/my-app
agentproc hub run claude-code -p "say hi in 5 words" --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
```

Expected output (streaming mode):

```
{"type":"partial","text":"Hi"}
{"type":"partial","text":" there"}
{"type":"partial","text":", how can I help?"}
{"type":"session","id":"13c2f6ec-1f97-42c4-be9e-9475129e243c"}
```

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/claude-code
echo '{"type":"turn","message":"say hi in 5 words","session_id":"","from_user":"u1","protocol_version":"0.3"}' | ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" python3 bridge.py
```

</details>

## How it works

```
turn.message, turn.session_id
  ↓
bridge.py / bridge.js
  ↓ claude -p <message> --output-format stream-json [--resume <session_id>]
claude CLI
  ↓ NDJSON stream: system / assistant / result events
bridge.py / bridge.js
  ↓ {"type":"partial","text":"..."}   (assistant text blocks)
  ↓ {"type":"session","id":"<id>"}    (session_id from result event, forwarded next turn)
  ↓ exit code from claude
```

The session ID is opaque — `claude` generates a UUID on its first turn, and the bridge forwards it via `{"type":"session"}`. On subsequent turns, your bridge passes it back as `turn.session_id`, and this bridge replays it as `--resume <id>`. Multi-turn continuity without the messaging bridge needing to know anything about Claude.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | yes* | Auth. (*Or run `claude setup-token` once and omit.) |
| `CLAUDE_MODEL` | no | Model alias or full name (e.g. `sonnet`, `claude-sonnet-4-6`) |
| `CLAUDE_DISALLOW_TOOLS` | no | Comma-separated disallowed tools (default: `AskUserQuestion`) |

Mid-turn tool authorization is enabled by the profile's `permission: true` (carried in the turn object as `permission`), not by an env var. See "Optional tool permission" below.

## Optional tool permission

By default this profile runs unattended with `--dangerously-skip-permissions`.

To require IM / CLI approval before tools run, set in the profile:

```yaml
permission: true
```

Then the bridge switches to:

```text
claude --print --input-format stream-json --output-format stream-json \
  --verbose --permission-prompt-tool stdio --permission-mode default
```

and translates Claude Code `control_request` (`can_use_tool`) ↔ AgentProc `{"type":"permission_request"}` / `{"type":"permission_response"}` events. `AskUserQuestion` stays disallowed — clarifying questions belong in the reply body / next IM turn.

`agentproc` on a TTY prompts `Allow? [y/N]` for each request; without a TTY it denies.

## Caveats

- Default path still uses `--dangerously-skip-permissions`. Prefer a sandbox, or enable `permission: true`.
- The session ID forwarded by `claude` is the **CLI session ID**, not an upstream API conversation ID. `--resume` knows how to use it.
- `streaming: false` falls back to one-shot mode: the bridge waits for the `result` event and emits the full text at once.

## License

MIT. The `claude` CLI is © Anthropic and licensed separately.
