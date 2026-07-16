# gemini-cli

Wraps the [`gemini`](https://github.com/google-gemini/gemini-cli) CLI (Google Gemini CLI) as an AgentProc agent with full streaming and multi-turn session continuity.

## Quick test (zero config)

```bash
# 1. Install gemini once:
npm install -g @google/gemini-cli

# 2. Auth either via gemini's own OAuth flow OR by exporting a key:
export GEMINI_API_KEY="AIza..."

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run gemini-cli -p "what is this codebase?"
```

No YAML editing. `agentproc hub run` uses your current directory as the agent's `cwd`, and locates the bundled `bridge.py` via `{{PROFILE_DIR}}` — so `gemini` runs against your project, no matter where you invoke from.

## Setup (if you can't use `hub run`)

1. Install the `gemini` CLI:

   ```bash
   npm install -g @google/gemini-cli
   ```

2. Authenticate:

   ```bash
   gemini    # interactive prompt → login with Google, or
   export GEMINI_API_KEY="AIza..."   # alternatively
   ```

3. Copy the profile into your project:

   ```bash
   agentproc hub install gemini-cli
   # or, from a repo checkout:
   cp -r hub/gemini-cli ./gemini-cli
   ```

4. Run against the local copy:

   ```bash
   agentproc --profile ./gemini-cli/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project \
             --env GEMINI_API_KEY="$GEMINI_API_KEY"
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory.
timeout_secs: 600
streaming: true
env:
  GEMINI_API_KEY: "${GEMINI_API_KEY}"
  # Optional overrides:
  # GEMINI_MODEL: "gemini-2.5-pro"
  # GEMINI_SANDBOX: "false"
```

## Local test

```bash
cd ~/projects/my-app
agentproc hub run gemini-cli -p "say hi in 5 words" --env GEMINI_API_KEY=$GEMINI_API_KEY
```

Expected output (streaming mode):

```
{"type":"partial","text":"Hi","session_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479"}
{"type":"partial","text":" there","session_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479"}
{"type":"partial","text":", how can I help?","session_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479"}
{"type":"result","text":"","session_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479"}
```

Note: gemini emits the session id **up-front** in its `init` event, so `session_id` is stamped on partials early — unlike claude/codex where it may arrive late on the terminal result.

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/gemini-cli
echo '{"type":"turn","message":"say hi in 5 words","session_id":"","protocol_version":"0.4"}' | GEMINI_API_KEY="$GEMINI_API_KEY" python3 bridge.py
```

</details>

## How it works

```
turn.message, turn.session_id
  ↓
bridge.py / bridge.js
  ↓ gemini -p <message> --output-format stream-json --yolo [--resume <id>]
gemini CLI
  ↓ NDJSON stream: init / message / error / result events
bridge.py / bridge.js
  ↓ {"type":"partial","text":"...","session_id":"<id>"}   (deltas; session_id from init)
  ↓ {"type":"result","text":"","session_id":"<id>"}
  ↓ {"type":"error","message":"..."}      (on error or result.status=error)
  ↓ exit code from gemini
```

The session ID is opaque — `gemini` emits it in the `init` event on its first turn, and the bridge forwards it as `session_id` on `partial`/`result` events. On subsequent turns, your bridge passes it back as `turn.session_id`, and this bridge replays it as `--resume <id>`.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `GEMINI_API_KEY` | yes* | Auth. (*Or run `gemini` once for OAuth login.) |
| `GEMINI_MODEL` | no | Model alias (`flash`) or full name (`gemini-2.5-pro`) |
| `GEMINI_SANDBOX` | no | Set to `false` to disable `--sandbox` |

## Caveats

- `--yolo` (auto-approve tool calls) is set unconditionally — `gemini` would otherwise prompt interactively, which a non-interactive AgentProc bridge can't satisfy. Run the bridge in a sandbox if you're concerned about tool execution.
- The session ID forwarded by `gemini` is the **CLI session ID**, not an upstream API conversation ID. `--resume` knows how to use it.
- `streaming: false` falls back to one-shot mode: the bridge waits for the terminal `message` event (with `delta=false` or absent) and emits the full text at once.
- `error` events with `severity: warning` are ignored (recoverable). Only `severity: error`, or a `result` event with `status: error`, surfaces as `{"type":"error"}`.

## License

MIT. The `gemini` CLI is © Google LLC and licensed separately under the Apache License 2.0.
