# agy

Wraps the `agy` CLI as an AgentProc agent. **One-shot mode only** — agy doesn't expose a resumable session id or stream chunks in `--print` mode, so this bridge forwards the final reply as a single AgentProc message body.

## Setup

1. Install `agy` (see the agy project for installation instructions).
2. Authenticate per agy's docs.
3. Copy `profile.yaml` and one bridge script into your project:

   ```bash
   cp hub/agy/profile.yaml     ./profile.yaml
   cp hub/agy/bridge.py        ./bridge.py    # Python
   # or
   cp hub/agy/bridge.js        ./bridge.js    # Node.js
   ```

4. Edit `cwd:` in `profile.yaml` to point at the directory `agy` should work in.

## Profile

```yaml
command: python3 ./bridge.py          # or: node ./bridge.js
cwd: ~/your-project
timeout_secs: 300
streaming: false                      # agy doesn't stream
env:
  AGY_MODEL: "${AGY_MODEL:-}"          # optional model override
  AGY_DANGEROUSLY_SKIP_PERMISSIONS: "1"
```

## Local test

```bash
cd hub/agy
AGENT_MESSAGE="reply with exactly: agy ok" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="0" \
python3 bridge.py
```

Expected output:

```
agy ok
```

(No `AGENT_SESSION:` line — see "Session continuity" below.)

## How it works

```
AGENT_MESSAGE
  ↓
bridge.py / bridge.js
  ↓ agy --print <message> [--dangerously-skip-permissions] [--model <m>]
agy CLI
  ↓ plain text reply on stdout (after the turn completes)
bridge.py / bridge.js
  ↓ reply body (no AGENT_SESSION: line, no AGENT_PARTIAL: lines)
```

## Session continuity

**agy's `--print` mode does not expose a session id on stdout.** The bridge therefore emits no `AGENT_SESSION:` line. Each AgentProc turn spawns a fresh agy process.

If your messaging bridge needs multi-turn context, use the [AgentProc SDK](https://agentproc.dev/sdk/python)'s history helpers (`load_history` / `append_history`) to maintain context in a JSONL file keyed by the messaging bridge's own session id. The [Python SDK history example](https://agentproc.dev/sdk/python#conversation-history) shows the pattern.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGY_MODEL` | no | Model override passed as `--model`. |
| `AGY_DANGEROUSLY_SKIP_PERMISSIONS` | no | `"1"` (default) adds the skip-permissions flag. Set to `"0"` to disable. |
| `AGY_TIMEOUT` | no | Process timeout in seconds (default: 300). |

## Caveats

- **No streaming.** agy's `--print` returns the full reply after the turn. The user waits for the whole response.
- **No multi-turn by default.** See "Session continuity" above.
- **No `--dangerously-skip-permissions` opt-out by default.** The bridge sets it because non-interactive agents can't satisfy interactive permission prompts. Set `AGY_DANGEROUSLY_SKIP_PERMISSIONS=0` to disable.

## License

MIT. The `agy` CLI retains its own license.
