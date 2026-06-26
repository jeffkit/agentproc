# codex

Wraps the [`codex`](https://github.com/openai/codex) CLI (OpenAI Codex) as an AgentProc agent with streaming and multi-turn session continuity.

## Quick test (zero config)

```bash
# 1. Install codex once:
npm install -g @openai/codex

# 2. Auth either via codex login OR by exporting a key:
export OPENAI_API_KEY="sk-..."

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run codex -p "what is this codebase?"
```

No YAML editing. `agentproc hub run` uses your current directory as the agent's `cwd`, and locates the bundled `bridge.py` via `{{PROFILE_DIR}}` — so `codex` runs against your project, no matter where you invoke from.

## Setup (if you can't use `hub run`)

1. Install the `codex` CLI:

   ```bash
   npm install -g @openai/codex
   ```

2. Authenticate — set `OPENAI_API_KEY` or run `codex login`.

3. Copy the profile into your project:

   ```bash
   agentproc hub install codex    # creates ./codex/
   # or, from a repo checkout:
   cp -r hub/codex ./codex
   ```

4. Run against the local copy. Use `--cwd` to tell `codex` which project to work on:

   ```bash
   agentproc --profile ./codex/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project \
             --env OPENAI_API_KEY="$OPENAI_API_KEY"
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory.
#       With --profile, pass --cwd explicitly.
timeout_secs: 600
streaming: true
env:
  OPENAI_API_KEY: "${OPENAI_API_KEY}"
  # Optional:
  # CODEX_MODEL: "gpt-5"
```

## Local test

```bash
cd ~/projects/my-app
agentproc hub run codex -p "reply with exactly: codex ok" --env OPENAI_API_KEY=$OPENAI_API_KEY
```

Expected output (on stderr / stdout):

```
AGENT_SESSION:019efead-1a89-7ff3-887a-cd11f3c0843f
codex ok
```

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/codex
AGENT_MESSAGE="reply with exactly: codex ok" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="0" \
OPENAI_API_KEY="$OPENAI_API_KEY" \
python3 bridge.py
```

</details>

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
