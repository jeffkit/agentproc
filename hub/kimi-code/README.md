# kimi-code

Wraps the [`kimi` CLI](https://moonshotai.github.io/kimi-cli) (Kimi Code CLI by Moonshot AI) as an AgentProc agent with **streaming** and **session continuity**. kimi's `--print` mode emits role-based NDJSON events; this bridge parses them and re-emits as `AGENT_PARTIAL:` and `AGENT_SESSION:` lines.

## Quick test (zero config)

```bash
# 1. Install kimi (see https://moonshotai.github.io/kimi-cli):
curl -fsSL https://moonshotai.github.io/kimi-cli/install.sh | sh

# 2. Authenticate:
kimi login

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run kimi-code -p "what is this codebase?"
```

## Setup (if you can't use `hub run`)

1. Install kimi and authenticate (see above).
2. Copy the profile:

   ```bash
   agentproc hub install kimi-code    # creates ./kimi-code/
   # or, from a repo checkout:
   cp -r hub/kimi-code ./kimi-code
   ```

3. Run against the local copy:

   ```bash
   agentproc --profile ./kimi-code/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
timeout_secs: 600
streaming: true
env:
  KIMI_MODEL: "${KIMI_MODEL}"                 # optional model override
env_allowlist: [KIMI_MODEL, MOONSHOT_API_KEY]
```

## Local test

```bash
cd hub/kimi-code
AGENT_MESSAGE="reply with exactly: kimi ok" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="1" \
python3 bridge.py
```

Expected output (streaming with session):

```
AGENT_PARTIAL:"kimi ok"
AGENT_SESSION:<uuid>
```

## How it works

```
AGENT_MESSAGE / AGENT_SESSION_ID
  ↓
bridge.py / bridge.js
  ↓ kimi --print -p <message> --output-format=stream-json --session <id> [--model <m>]
kimi CLI  (emits role-based NDJSON on stdout)
  ↓ {"role":"assistant","content":"..."}  → AGENT_PARTIAL: (streaming) / reply body
  ↓ {"role":"tool",...}                   → ignored
bridge.py / bridge.js
  ↓ AGENT_SESSION:<uuid>   (forwarded to SDK for next turn's AGENT_SESSION_ID)
```

## Session continuity

kimi's `--session <id>` flag creates a new session with the given ID if it doesn't exist, or resumes the existing one. The bridge generates a UUID on the first turn and forwards it as `AGENT_SESSION:`. On subsequent turns, the SDK passes it back as `AGENT_SESSION_ID`, and the bridge replays it as `--session <id>`.

This gives kimi native multi-turn continuity backed by kimi's own session database, without needing any external state file.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `MOONSHOT_API_KEY` | depends | Moonshot API key (alternative to `kimi login`). |
| `KIMI_MODEL` | no | Model override passed as `--model`. |
| `KIMI_TIMEOUT` | no | Process timeout in seconds (default: 600). |

## Caveats

- **Auth store vs env vars.** By default kimi reads OAuth credentials from `~/.kimi/` (set up with `kimi login`). In environments without keychain access (CI, launchd daemons), set `MOONSHOT_API_KEY` instead.
- **Tool use is transparent.** Intermediate `role=assistant` messages (with tool_calls) ARE streamed so the user can see kimi's reasoning. `role=tool` messages (tool results) are not forwarded.
- **Session ID format.** The bridge uses UUID v4 as the kimi session ID. kimi creates a new session directory under `~/.kimi/sessions/<uuid>/` on first use.

## License

MIT. The `kimi` CLI retains its own license.
