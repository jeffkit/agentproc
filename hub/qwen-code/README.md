# qwen-code

Wraps the [`qwen`](https://github.com/QwenLM/qwen-code) CLI (Alibaba Qwen Code) as an AgentProc agent with full streaming and multi-turn session continuity.

> **Profile status:** `community` — Qwen Code is a fork of gemini-cli and its `--output-format stream-json` schema is expected to match gemini's. This bridge reuses the gemini-cli parser. End-to-end verification against an installed `qwen` is pending; please report drift at [issues](https://github.com/jeffkit/agentproc/issues).

## Quick test (zero config)

```bash
# 1. Install qwen once (Node 22+ required):
npm install -g @qwen-code/qwen-code

# 2. Auth either via qwen's own /auth command OR by exporting a key:
export QWEN_API_KEY="sk-..."
#   Alternative: DASHSCOPE_API_KEY (Alibaba Cloud DashScope)

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run qwen-code -p "what is this codebase?"
```

## Setup (if you can't use `hub run`)

1. Install the `qwen` CLI:

   ```bash
   npm install -g @qwen-code/qwen-code
   ```

2. Authenticate (run `qwen` once and use `/auth`, or set `QWEN_API_KEY`/`DASHSCOPE_API_KEY`).

3. Copy the profile into your project:

   ```bash
   agentproc hub install qwen-code
   # or, from a repo checkout:
   cp -r hub/qwen-code ./qwen-code
   ```

4. Run against the local copy:

   ```bash
   agentproc --profile ./qwen-code/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project \
             --env QWEN_API_KEY="$QWEN_API_KEY"
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
timeout_secs: 600
streaming: true
env:
  QWEN_API_KEY: "${QWEN_API_KEY}"
  # Optional overrides:
  # QWEN_MODEL: "qwen3-coder-plus"
  # QWEN_SANDBOX: "false"
```

## How it works

```
turn.message, turn.session_id
  ↓
bridge.py / bridge.js
  ↓ qwen -p <message> --output-format stream-json --yolo [--resume <id>]
qwen CLI
  ↓ NDJSON stream: init / message / error / result events (gemini-cli shape)
bridge.py / bridge.js
  ↓ {"type":"session","id":"<id>"}   (session_id from init event)
  ↓ {"type":"partial","text":"..."}   (assistant message deltas)
  ↓ {"type":"error","message":"..."}      (on error or result.status=error)
```

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `QWEN_API_KEY` | yes* | Auth. (*Or `DASHSCOPE_API_KEY`, or run `qwen` once for `/auth`.) |
| `QWEN_MODEL` | no | Model name (e.g. `qwen3-coder-plus`) |
| `QWEN_SANDBOX` | no | Set to `false` to disable `--sandbox` |

## Caveats

- `--yolo` (auto-approve tool calls) is set unconditionally — same rationale as the gemini-cli profile.
- Schema is inherited from gemini-cli (Qwen Code is a fork). If qwen diverges in a future release, this bridge needs an update — please open an issue.
- `error` events with `severity: warning` are ignored. Only `severity: error`, or a `result` event with `status: error`, surfaces as `{"type":"error"}`.

## License

MIT. The `qwen` CLI is © Alibaba QwenLM and licensed separately.
