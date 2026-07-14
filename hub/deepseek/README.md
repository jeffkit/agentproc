# deepseek

Wraps the [DeepSeek TUI CLI](https://deepseek.com) as an AgentProc agent. Uses `deepseek exec` for **non-interactive, one-shot** mode — the agent runs once, returns plain text, and exits. No streaming, no native session continuity.

## Quick test (zero config)

```bash
# 1. Install the DeepSeek TUI CLI:
brew install deepseek
# Or download from https://deepseek.com/downloads

# 2. Authenticate:
deepseek login
# Or set DEEPSEEK_API_KEY in your environment.

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run deepseek -p "what is this codebase?"
```

## Setup (if you can't use `hub run`)

1. Install the CLI and authenticate (see above).
2. Copy the profile:

   ```bash
   agentproc hub install deepseek    # creates ./deepseek/
   # or, from a repo checkout:
   cp -r hub/deepseek ./deepseek
   ```

3. Run:

   ```bash
   agentproc --profile ./deepseek/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
timeout_secs: 300
streaming: false                     # deepseek exec returns full text only
env:
  DEEPSEEK_MODEL: "${DEEPSEEK_MODEL}"
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}"
env_allowlist: [DEEPSEEK_MODEL, DEEPSEEK_API_KEY]
```

## Local test

```bash
cd hub/deepseek
echo '{"type":"turn","message":"reply with exactly: deepseek ok","session_id":"","from_user":"u1","protocol_version":"0.4"}' | python3 bridge.py
```

Expected output:

```
deepseek ok
```

(No `session_id` on events — see "Session continuity" below.)

<details>
<summary>Run with Node bridge</summary>

```bash
cd hub/deepseek
echo '{"type":"turn","message":"reply with exactly: deepseek ok","session_id":"","from_user":"u1","protocol_version":"0.4"}' | node bridge.js
```

</details>

## How it works

```
turn.message
  ↓
bridge.py / bridge.js
  ↓ deepseek exec -p <message> [--model <m>]
DeepSeek TUI CLI
  ↓ plain text reply on stdout (after the turn completes)
bridge.py / bridge.js
  ↓ reply body (no session_id on events, no {"type":"partial"} lines)
```

## Session continuity

**`deepseek exec` is stateless.** Each invocation is independent; context is NOT persisted between separate `deepseek exec` calls, even when passing the same `--session` ID. No `session_id` is stamped on events.

For multi-turn context, use the [AgentProc SDK](https://agentproc.dev/sdk/python)'s history helpers (`load_history` / `append_history`) to maintain a JSONL conversation file keyed by your messaging bridge's own session ID.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `DEEPSEEK_API_KEY` | depends | DeepSeek API key. If omitted, `deepseek login` credentials are used. |
| `DEEPSEEK_MODEL` | no | Model override (e.g. `deepseek-v4-flash`). Default: `deepseek-v4-pro`. |
| `DEEPSEEK_TIMEOUT` | no | Process timeout in seconds (default: 300). |

Available models:

| Model | Notes |
|-------|-------|
| `deepseek-v4-pro` | Best quality (default) |
| `deepseek-v4-flash` | Faster, lighter |

## Caveats

- **No streaming.** `deepseek exec` returns the full reply after the turn completes. The user waits for the whole response.
- **No multi-turn by default.** See "Session continuity" above.
- **TUI binary required.** The bridge calls the `deepseek` binary (the TUI CLI), not the DeepSeek Python SDK. Install it via `brew install deepseek` or from [deepseek.com/downloads](https://deepseek.com/downloads).

## License

MIT. The DeepSeek TUI CLI retains its own license.
