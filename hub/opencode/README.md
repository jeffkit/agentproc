# opencode

Wraps the [`opencode` CLI](https://github.com/anomalyco/opencode) as an AgentProc agent with **streaming** and **session continuity**. opencode emits NDJSON events via `--format json`; this bridge parses those events and re-emits them as `{"type":"partial"}` and `{"type":"session"}` lines.

## Quick test (zero config)

```bash
# 1. Install opencode:
npm install -g opencode-ai
# or: curl -fsSL https://opencode.ai/install | bash

# 2. Authenticate (stores creds in ~/.local/share/opencode/auth.json):
opencode auth login

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run opencode -p "what is this codebase?"
```

## Setup (if you can't use `hub run`)

1. Install opencode and authenticate (see above).
2. Copy the profile:

   ```bash
   agentproc hub install opencode    # creates ./opencode/
   # or, from a repo checkout:
   cp -r hub/opencode ./opencode
   ```

3. Run against the local copy:

   ```bash
   agentproc --profile ./opencode/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
timeout_secs: 600
streaming: true
env:
  OPENCODE_MODEL: "${OPENCODE_MODEL}"         # optional model override
env_allowlist: [OPENCODE_MODEL, ANTHROPIC_API_KEY, OPENAI_API_KEY]
```

## Local test

```bash
cd hub/opencode
echo '{"type":"turn","message":"reply with exactly: opencode ok","session_id":"","from_user":"u1","protocol_version":"0.3"}' | python3 bridge.py
```

Expected output (streaming):

```
{"type":"partial","text":"opencode ok"}
{"type":"session","id":"ses_<opaque-id>"}
```

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/opencode
echo '{"type":"turn","message":"reply with exactly: opencode ok","session_id":"","from_user":"u1","protocol_version":"0.3"}' | python3 bridge.py
```

</details>

## How it works

```
turn.message / turn.session_id
  ↓
bridge.py / bridge.js
  ↓ opencode run <message> --auto --format json [--session <id>] [--model <m>]
opencode CLI  (emits NDJSON events on stdout)
  ↓ step_start → sessionID captured
  ↓ text       → {"type":"partial"} (streaming) or accumulated for reply body
  ↓ step_finish → turn complete
bridge.py / bridge.js
  ↓ {"type":"session","id":"<id>"}   (replayed as --session on next turn)
  ↓ reply body            (non-streaming) or nothing (streaming used partials)
```

## Session continuity

opencode's `--format json` emits a `sessionID` field (format `ses_XXX`) on every event. The bridge captures it and forwards it as `{"type":"session"}`. On the next turn, the SDK passes it back as `turn.session_id`, and the bridge replays it as `--session <id>` — enabling native multi-turn continuity backed by opencode's own session database.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | depends | Anthropic API key (alternative to `opencode auth login`). |
| `OPENAI_API_KEY` | depends | OpenAI API key (alternative to `opencode auth login`). |
| `OPENCODE_MODEL` | no | Model override in `provider/model` format (e.g. `anthropic/claude-opus-4-5`). |

## Caveats

- **Requires opencode v1.16.3+.** Earlier versions had a race condition where `--format json` could exit before all text events were flushed ([#31365](https://github.com/anomalyco/opencode/issues/31365), [#31435](https://github.com/anomalyco/opencode/issues/31435)). Upgrade with `opencode upgrade`.
- **Tool use is transparent.** `tool_use` events are used only for session ID capture; their content (bash output, file diffs) is not forwarded to the user. The final text reply summarises what was done.
- **Auth store vs env vars.** By default opencode reads credentials from `~/.local/share/opencode/auth.json` (set up with `opencode auth login`). In environments without keychain access (CI, launchd daemons), set the standard `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env vars and list them in `env_allowlist` instead.

## License

MIT. The `opencode` CLI retains its own license.
