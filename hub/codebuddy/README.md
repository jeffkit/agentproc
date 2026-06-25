# codebuddy

Wraps the [`codebuddy`](https://copilot.tencent.com) CLI (Tencent CodeBuddy) as an AgentProc agent with streaming and session continuity.

CodeBuddy's `--output-format stream-json` produces a schema compatible with Anthropic's `claude` CLI, so this bridge is structurally identical to the [claude-code bridge](../claude-code/) — only the command name, the resume flag (`-r` instead of `--resume`), and the auth flow differ.

## Setup

1. Install `codebuddy` (distributed by Tencent — see your internal docs).
2. Authenticate by running `codebuddy` once interactively and following the login flow.
3. Copy `profile.yaml` and one bridge script into your project:

   ```bash
   cp hub/codebuddy/profile.yaml   ./profile.yaml
   cp hub/codebuddy/bridge.py      ./bridge.py    # Python
   # or
   cp hub/codebuddy/bridge.js      ./bridge.js    # Node.js
   ```

4. Edit `cwd:` in `profile.yaml` to point at the directory `codebuddy` should work in.

## Profile

```yaml
command: python3 ./bridge.py          # or: node ./bridge.js
cwd: ~/your-project
timeout_secs: 600
streaming: true
env:
  # Auth handled by codebuddy's own login.
  # Optional overrides:
  # CODEBUDDY_MODEL: "claude-sonnet-4.6"
  # CODEBUDDY_DISALLOW_TOOLS: "AskUserQuestion,WebSearch"
```

## Local test

```bash
cd hub/codebuddy
AGENT_MESSAGE="reply with exactly: codebuddy ok" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="0" \
python3 bridge.py
```

Expected output:

```
AGENT_SESSION:53bb6352-4b47-43fc-bce6-eaf808d419da
codebuddy ok
```

## How it works

Same shape as [claude-code](../claude-code/#how-it-works), with these deltas:

| Aspect | claude-code | codebuddy |
|--------|-------------|-----------|
| Command | `claude` | `codebuddy` |
| Resume flag | `--resume <id>` | `-r <id>` |
| Disallowed-tools flag | `--disallowed-tools` | `--disallowedTools` (camelCase) |
| Auth env | `ANTHROPIC_API_KEY` | (via `codebuddy` login) |
| Model env | `CLAUDE_MODEL` | `CODEBUDDY_MODEL` |

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `CODEBUDDY_MODEL` | no | Model override. |
| `CODEBUDDY_DISALLOW_TOOLS` | no | Comma-separated disallowed tools (default: `AskUserQuestion`). |

## Caveats

- Same `--dangerously-skip-permissions` trade-off as claude-code; sandbox if concerned.
- `codebuddy` is currently Tencent-internal distribution; this profile is provided for users who already have access. Open-source users without a CodeBuddy account should use the [claude-code](../claude-code/) profile instead.

## License

MIT. The `codebuddy` CLI is © Tencent and licensed separately.
