# claude-code

Wraps the [`claude`](https://docs.claude.com/en/docs/claude-code) CLI (Anthropic Claude Code) as an AgentProc agent with full streaming and multi-turn session continuity.

## Setup

1. Install the `claude` CLI:

   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. Authenticate:

   ```bash
   claude setup-token        # or set ANTHROPIC_API_KEY in your environment
   ```

3. Copy `profile.yaml` and one bridge script into your project:

   ```bash
   cp hub/claude-code/profile.yaml     ./profile.yaml
   cp hub/claude-code/bridge.py        ./bridge.py    # Python
   # or
   cp hub/claude-code/bridge.js        ./bridge.js    # Node.js
   ```

4. Edit `cwd:` in `profile.yaml` to point at the directory `claude` should work in.

## Profile

```yaml
command: python3 ./bridge.py          # or: node ./bridge.js
cwd: ~/your-project
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
cd hub/claude-code
AGENT_MESSAGE="say hi in 5 words" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="1" \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
python3 bridge.py
```

Expected output (streaming mode):

```
AGENT_PARTIAL:"Hi"
AGENT_PARTIAL:" there"
AGENT_PARTIAL,", how can I help?"
AGENT_SESSION:13c2f6ec-1f97-42c4-be9e-9475129e243c
```

## How it works

```
AGENT_MESSAGE, AGENT_SESSION_ID
  ↓
bridge.py / bridge.js
  ↓ claude -p <message> --output-format stream-json [--resume <session_id>]
claude CLI
  ↓ NDJSON stream: system / assistant / result events
bridge.py / bridge.js
  ↓ AGENT_PARTIAL:"..."   (assistant text blocks)
  ↓ AGENT_SESSION:<id>    (session_id from result event, forwarded next turn)
  ↓ exit code from claude
```

The session ID is opaque — `claude` generates a UUID on its first turn, and the bridge forwards it via `AGENT_SESSION:`. On subsequent turns, your bridge passes it back as `AGENT_SESSION_ID`, and this bridge replays it as `--resume <id>`. Multi-turn continuity without the messaging bridge needing to know anything about Claude.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | yes* | Auth. (*Or run `claude setup-token` once and omit.) |
| `CLAUDE_MODEL` | no | Model alias or full name (e.g. `sonnet`, `claude-sonnet-4-6`) |
| `CLAUDE_DISALLOW_TOOLS` | no | Comma-separated disallowed tools (default: `AskUserQuestion`) |

## Caveats

- `--dangerously-skip-permissions` is set unconditionally — `claude` would otherwise prompt interactively, which a non-interactive AgentProc bridge can't satisfy. Run the bridge in a sandbox if you're concerned about tool execution.
- The session ID forwarded by `claude` is the **CLI session ID**, not an upstream API conversation ID. `--resume` knows how to use it.
- `streaming: false` falls back to one-shot mode: the bridge waits for the `result` event and emits the full text at once.

## License

MIT. The `claude` CLI is © Anthropic and licensed separately.
