# agy

Wraps the `agy` CLI as an AgentProc agent. **One-shot mode only** — agy doesn't expose a resumable session id or stream chunks in `--print` mode, so this bridge forwards the final reply as a single AgentProc message body.

## Quick test (zero config)

```bash
# 1. Install agy (see the agy project for installation instructions).
# 2. Authenticate per agy's docs.

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run agy -p "what is this codebase?"
```

No YAML editing. `agentproc hub run` uses your current directory as the agent's `cwd`, and locates the bundled `bridge.py` via `{{PROFILE_DIR}}`.

## Setup (if you can't use `hub run`)

1. Install `agy` (see the agy project for installation instructions).
2. Authenticate per agy's docs.
3. Copy the profile into your project:

   ```bash
   agentproc hub install agy    # creates ./agy/
   # or, from a repo checkout:
   cp -r hub/agy ./agy
   ```

4. Run against the local copy. Use `--cwd` to tell `agy` which project to work on:

   ```bash
   agentproc --profile ./agy/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory.
#       With --profile, pass --cwd explicitly.
timeout_secs: 300
streaming: false                      # agy doesn't stream
env:
  AGY_MODEL: "${AGY_MODEL}"           # optional model override
env_allowlist: [AGY_MODEL]
```

## Local test

```bash
cd ~/projects/my-app
agentproc hub run agy -p "reply with exactly: agy ok"
```

Expected output (on stdout):

```
agy ok
```

(No `{"type":"session"}` line — see "Session continuity" below.)

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/agy
echo '{"type":"turn","message":"reply with exactly: agy ok","session_id":"","from_user":"u1","protocol_version":"0.3"}' | python3 bridge.py
```

</details>

## How it works

```
turn.message
  ↓
bridge.py / bridge.js
  ↓ agy --print <message> [--dangerously-skip-permissions] [--model <m>]
agy CLI
  ↓ plain text reply on stdout (after the turn completes)
bridge.py / bridge.js
  ↓ reply body (no {"type":"session"} line, no {"type":"partial"} lines)
```

## Session continuity

**agy's `--print` mode does not expose a session id on stdout.** The bridge therefore emits no `{"type":"session"}` line. Each AgentProc turn spawns a fresh agy process.

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
