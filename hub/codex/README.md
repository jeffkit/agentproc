# codex

Wraps the [`codex`](https://github.com/openai/codex) CLI (OpenAI Codex) as an AgentProc agent with streaming and multi-turn session continuity.

## Setup

1. Install the `codex` CLI:

   ```bash
   npm install -g @openai/codex
   ```

2. Authenticate — set `OPENAI_API_KEY` or run `codex login`.

3. Copy `profile.yaml` and one bridge script into your project:

   ```bash
   cp hub/codex/profile.yaml     ./profile.yaml
   cp hub/codex/bridge.py        ./bridge.py    # Python
   # or
   cp hub/codex/bridge.js        ./bridge.js    # Node.js
   ```

4. Edit `cwd:` in `profile.yaml` to point at the directory `codex` should work in.

## Profile

```yaml
command: python3 ./bridge.py          # or: node ./bridge.js
cwd: ~/your-project
timeout_secs: 600
streaming: true
env:
  OPENAI_API_KEY: "${OPENAI_API_KEY}"
  # Optional:
  # CODEX_MODEL: "gpt-5"
```

## Local test

```bash
cd hub/codex
AGENT_MESSAGE="reply with exactly: codex ok" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="0" \
OPENAI_API_KEY="$OPENAI_API_KEY" \
python3 bridge.py
```

Expected output:

```
AGENT_SESSION:019efead-1a89-7ff3-887a-cd11f3c0843f
codex ok
```

## How it works

codex emits NDJSON events with its own schema (different from claude's):

| Event | What the bridge does |
|-------|---------------------|
| `thread.started` | Captures `thread_id` as the session id |
| `item.completed` (`type: agent_message`) | Emits `AGENT_PARTIAL:` with the text |
| `turn.completed` | Signals end of turn (no-op for the bridge) |
| `turn.failed` | Emits `AGENT_ERROR:` |

For session continuity, the bridge invokes `codex exec resume <thread_id> <prompt>` when `AGENT_SESSION_ID` is set. The thread id is opaque — the messaging bridge forwards it without interpreting.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `OPENAI_API_KEY` | yes | Auth for `codex`. |
| `CODEX_MODEL` | no | Model name (e.g. `gpt-5`). Sets `-c model="..."` on first turn. |

## Caveats

- `codex exec --json` writes its progress lines to stderr by default (visible if you run the bridge with `AGENT_STREAMING=0`). The bridge does not forward stderr to the user unless `include_stderr_in_reply: true` is set in the profile.
- The bridge waits for `turn.completed` / `turn.failed` rather than relying on stdout EOF, because codex's stream ends with a final usage summary that's not part of the reply.
- `codex`'s sandbox mode is **not** disabled — if you want the agent to be able to write files or run commands, configure that in `~/.codex/config.toml` (e.g. `sandbox_mode = "workspace-write"`).

## License

MIT. The `codex` CLI is © OpenAI and licensed separately.
