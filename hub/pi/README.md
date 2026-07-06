# pi

Wraps the [`pi` coding agent CLI](https://github.com/earendil-works/pi) as an AgentProc agent. **One-shot mode only** — pi's `-p` (print) mode returns the full reply as plain text and does not expose a session id, so this bridge forwards the final reply as a single AgentProc message body.

## Quick test (zero config)

```bash
# 1. Install pi:
npm install -g @earendil-works/pi-coding-agent

# 2. Set your provider API key (e.g. Anthropic):
export ANTHROPIC_API_KEY=sk-ant-...

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run pi -p "what is this codebase?"
```

## Setup (if you can't use `hub run`)

1. Install `pi` and set your API key (see above).
2. Copy the profile into your project:

   ```bash
   agentproc hub install pi    # creates ./pi/
   # or, from a repo checkout:
   cp -r hub/pi ./pi
   ```

3. Run against the local copy:

   ```bash
   agentproc --profile ./pi/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
timeout_secs: 600
streaming: false                      # pi -p returns full text at end
env:
  PI_MODEL: "${PI_MODEL}"             # optional model override
env_allowlist: [PI_MODEL, ANTHROPIC_API_KEY, OPENAI_API_KEY]
```

## Local test

```bash
cd hub/pi
AGENT_MESSAGE="reply with exactly: pi ok" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="0" \
python3 bridge.py
```

Expected output:

```
pi ok
```

## How it works

```
AGENT_MESSAGE
  ↓
bridge.py / bridge.js
  ↓ pi -p <message> --approve --no-extensions [--model <m>]
pi CLI
  ↓ plain text reply on stdout (after the turn completes)
bridge.py / bridge.js
  ↓ reply body (no AGENT_SESSION: line, no AGENT_PARTIAL: lines)
```

## Session continuity

**pi's `-p` mode does not expose a session id on stdout.** The bridge therefore emits no `AGENT_SESSION:` line. Each AgentProc turn spawns a fresh pi process.

If your messaging bridge needs multi-turn context, use the [AgentProc SDK](https://agentproc.dev/sdk/python)'s history helpers (`load_history` / `append_history`) to maintain context in a JSONL file.

## Known issue: process hanging with extensions

pi's `-p` mode may hang after responding when extensions (telegram, mcp, etc.) are loaded — they keep the Node.js event loop alive. The bridge adds `--no-extensions` by default to work around this ([upstream issue #4617](https://github.com/earendil-works/pi/issues/4617)). Set `PI_NO_EXTENSIONS=0` to disable this flag if needed.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | depends | Anthropic API key (if using Anthropic models). |
| `OPENAI_API_KEY` | depends | OpenAI API key (if using OpenAI models). |
| `PI_MODEL` | no | Model override passed as `--model` (e.g. `anthropic/claude-opus-4-5`). |
| `PI_NO_EXTENSIONS` | no | `"1"` (default) adds `--no-extensions`. Set to `"0"` to disable. |
| `PI_TIMEOUT` | no | Process timeout in seconds (default: 600). |

## Caveats

- **No streaming.** pi's `-p` returns the full reply after the turn. The user waits for the whole response.
- **No multi-turn by default.** See "Session continuity" above.
- **Requires `--no-extensions` for reliable exit.** See "Known issue" above.

## License

MIT. The `pi` CLI retains its own license.
