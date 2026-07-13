# cursor

Wraps the Cursor Agent CLI (binary name: `agent`) as an AgentProc agent with full streaming and multi-turn session continuity.

> **Note:** the Cursor Agent CLI binary is named **`agent`**, not `cursor`. It is a standalone download separate from the Cursor IDE.

## Quick test (zero config)

```bash
# 1. Install Cursor Agent (Homebrew):
brew install cursor-agent
#    Or download from https://cursor.com/downloads

# 2. Authenticate (one-time):
agent login

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run cursor -p "what is this codebase?"
```

No YAML editing. `agentproc hub run` uses your current directory as the agent's `cwd`, and locates the bundled `bridge.py` via `{{PROFILE_DIR}}` — so `agent` runs against your project, no matter where you invoke from.

## Setup (if you can't use `hub run`)

1. Install the Cursor Agent CLI:

   ```bash
   brew install cursor-agent
   # Or: download the binary from https://cursor.com/downloads
   ```

2. Authenticate:

   ```bash
   agent login                              # OAuth flow
   # Or:
   export CURSOR_API_KEY="..."              # API key (alternative)
   ```

3. Copy the profile into your project:

   ```bash
   agentproc hub install cursor
   # or, from a repo checkout:
   cp -r hub/cursor ./cursor
   ```

4. Run against the local copy:

   ```bash
   agentproc --profile ./cursor/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project \
             --env CURSOR_API_KEY="$CURSOR_API_KEY"
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory.
timeout_secs: 600
streaming: true
env:
  CURSOR_API_KEY: "${CURSOR_API_KEY}"
  # Optional overrides:
  # CURSOR_MODEL: "gpt-5"
  # CURSOR_FORCE: "1"          # "1" (default) adds --yolo; "0" omits it
```

## Local test

```bash
cd ~/projects/my-app
agentproc hub run cursor -p "say hi in 5 words"
```

Expected output (streaming mode):

```
{"type":"session","id":"f47ac10b-58cc-4372-a567-0e02b2c3d479"}
{"type":"partial","text":"Hi"}
{"type":"partial","text":" there"}
{"type":"partial","text":", how can I help?"}
```

The session id is emitted by `agent`'s `system/init` event up-front, so `{"type":"session"}` typically appears before the partials.

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/cursor
echo '{"type":"turn","message":"say hi in 5 words","session_id":"","from_user":"u1","protocol_version":"0.3"}' | python3 bridge.py
```

</details>

## How it works

```
turn.message, turn.session_id
  ↓
bridge.py / bridge.js
  ↓ agent -p <message> --output-format stream-json --stream-partial-output --yolo [--resume <id>]
agent CLI
  ↓ NDJSON stream: system/init / assistant / result events
bridge.py / bridge.js
  ↓ {"type":"session","id":"<id>"}   (session_id from system/init event)
  ↓ {"type":"partial","text":"..."}   (assistant text deltas)
  ↓ {"type":"error","message":"..."}      (on result.is_error)
```

The session ID is opaque — `agent` emits it in the `system/init` event on its first turn, and the bridge forwards it via `{"type":"session"}`. On subsequent turns, your bridge passes it back as `turn.session_id`, and this bridge replays it as `--resume <id>`. Cursor calls sessions "chats" but the id format is the same UUID.

### Duplicate-suppression

When `--stream-partial-output` is on, Cursor streams N delta chunks AND THEN emits a final `assistant` event with the **full assembled text** — which would duplicate what was already streamed. The bridge tracks the accumulated emitted text and drops any `assistant` event whose text equals the accumulation. This keeps `{"type":"partial"}` clean without losing the final assembled text (which is still captured from the `result` event for non-streaming mode).

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `CURSOR_API_KEY` | yes* | Auth. (*Or run `agent login` once.) |
| `CURSOR_MODEL` | no | Model name (`gpt-5`, `sonnet-4-thinking`, etc.) |
| `CURSOR_FORCE` | no | `1` (default) adds `--yolo`; `0` omits it |

## Caveats

- `--yolo` (auto-approve tool calls) is set by default — `agent` would otherwise prompt interactively, which a non-interactive AgentProc bridge can't satisfy. Run the bridge in a sandbox or set `CURSOR_FORCE=0` if you want explicit approvals (but the bridge will hang on the first tool call).
- The session ID forwarded by `agent` is the **chat ID**, not an upstream API conversation ID. `--resume` knows how to use it.
- `streaming: false` falls back to one-shot mode: the bridge waits for the `result` event and emits the full `result` text at once.
- `tool_call` events (`subtype: started` / `completed`) are ignored — only `assistant` text is forwarded to the user.

## License

MIT. The Cursor Agent CLI is © Cursor, Inc. and licensed separately.
