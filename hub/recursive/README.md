# recursive

Wraps the [`recursive`](https://github.com/jeffkit/recursive) CLI — a self-improving Rust coding agent — as an AgentProc agent with streaming and multi-turn session continuity.

## Quick test

```bash
# 1. Build and install recursive (from the recursive repo):
cargo install --locked --path .
recursive init          # configure a provider, model, and API key

# 2. From your project directory:
cd ~/projects/my-app
agentproc hub run recursive -p "what is this codebase?"
```

If `recursive init` is already done, no YAML editing and no `--env` is needed — the bridge lets recursive use its own `~/.recursive/config.toml`. To override the provider per-run instead, pass the relevant `--env` vars (see below).

## Setup (if you can't use `hub run`)

1. Build the `recursive` CLI from source:

   ```bash
   cargo install --locked --path .     # from the recursive repo
   # or: cargo build --release && copy target/release/recursive onto PATH
   recursive init                       # configure provider/model/key
   ```

2. Copy the profile into your project:

   ```bash
   agentproc hub install recursive      # creates ./recursive/
   # or, from a repo checkout:
   cp -r hub/recursive ./recursive
   ```

3. Run against the local copy:

   ```bash
   agentproc --profile ./recursive/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py   # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory, which
#       becomes recursive's workspace root (the directory it can read/write).
timeout_secs: 1200
streaming: true
env:
  # All optional — if unset, recursive uses ~/.recursive/config.toml.
  RECURSIVE_API_KEY:  "${RECURSIVE_API_KEY}"
  RECURSIVE_PROVIDER: "${RECURSIVE_PROVIDER}"
  RECURSIVE_API_BASE: "${RECURSIVE_API_BASE}"
  RECURSIVE_MODEL:    "${RECURSIVE_MODEL}"
```

## How it works

recursive emits its lifecycle as NDJSON `AgentEvent` objects when run with `--json`. The bridge invokes:

- **First turn:** `recursive --json --stream -H run <message>` — and captures the session directory recursive logs on stderr (`session: recording to <dir>`).
- **Subsequent turns:** `recursive --json --stream -H resume --from-file <session-dir> -p <message>` — native session-id resume.

and maps the event stream to AgentProc:

| Event | What the bridge does |
|-------|----------------------|
| `partial_token` | Emits `AGENT_PARTIAL:` with the delta (streaming mode) |
| `assistant_text` | Emits `AGENT_PARTIAL:` (non-streaming), or as a fallback when a step produced no streamed deltas |
| `turn_finished` | Terminal — the bridge stops after EOF |
| non-zero exit + no reply | Emits `AGENT_ERROR:` with the stderr tail |

### Multi-turn continuity

recursive records each run as a session directory (under `~/.recursive/workspaces/<hash>/sessions/<timestamp-slug>/`) and logs `session: recording to <dir>` on stderr. `recursive resume --from-file <dir> -p <msg>` continues that session by appending `<msg>` as the next user turn — the orthodox session-id resume.

The bridge:

1. **Mints an opaque id** (`rc-<uuid>`) and emits it as `AGENT_SESSION:` (recursive's `--json` stream does not surface a session id, so the bridge owns the AgentProc-level handle).
2. **Turn 1:** runs `recursive run`, captures the recursive session directory from stderr, and persists it keyed by the opaque id (a `<state_dir>/<id>.session` file containing the dir path).
3. **Turn N:** loads the stored session directory and runs `recursive resume --from-file <dir> -p <new message>`.

No transcript-file replay, no `--resume-from` indexing, and no system-message stripping — recursive's own session writer keeps the transcript clean and uniquely numbered across turns (it does not re-prepend the system prompt as a message).

`<state_dir>` defaults to `${TMPDIR:-/tmp}/agentproc-recursive`; set `RECURSIVE_STATE_DIR` to a persistent location if your OS clears `/tmp` on reboot and you need long-lived sessions.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `RECURSIVE_API_KEY` | no* | Auth key → `recursive --api-key`. |
| `RECURSIVE_PROVIDER` | no* | `openai` or `anthropic` → `recursive --provider`. |
| `RECURSIVE_API_BASE` | no* | LLM API base URL → `recursive --api-base`. |
| `RECURSIVE_MODEL` | no* | Model identifier → `recursive --model`. |
| `RECURSIVE_WORKSPACE` | no | Workspace root → `recursive --workspace` (default: `cwd`). |
| `RECURSIVE_MAX_STEPS` | no | Agent loop cap → `recursive --max-steps`. |
| `RECURSIVE_PERMISSION_MODE` | no | `default` · `plan` · `auto` (default: `auto`). |
| `RECURSIVE_STATE_DIR` | no | Where session-dir links persist (default: tmpdir). |

\* Required only if you haven't configured recursive via `recursive init` / `~/.recursive/config.toml`. When unset, the bridge passes no override and recursive falls back to its config file.

## Caveats

- **Headless / auto-approve.** The bridge runs non-interactively, so `--permission-mode` defaults to `auto` (all tool calls approved without prompting). The agent CAN read/write files and run shell commands within the workspace `cwd`. Pick the `cwd` accordingly, or set `RECURSIVE_PERMISSION_MODE=default` to route approvals through external hooks.
- **`-H` (headless).** Passed so interactive tools go through external hooks instead of waiting on a terminal that isn't there.
- **Session recording.** The bridge uses recursive's native session recording (no `--no-session`). Sessions land under `~/.recursive/workspaces/<hash>/sessions/` (or `$RECURSIVE_HOME` if set). The bridge captures each session's directory from stderr and resumes it by path, so it does not depend on `~/.recursive` being stable across turns — only on `RECURSIVE_STATE_DIR` (where the `<id>.session` link files live).
- **Provider compatibility.** recursive's Anthropic parser treats `thinking` content blocks as reasoning (emitted as a `reasoning` event, not as reply text). This lands in recursive 0.7.0+ — older binaries (e.g. 0.6.0) reject `thinking` blocks and will error on models that emit them (DeepSeek `deepseek-v4-flash` on its `/anthropic` endpoint is the canonical case). If you hit `unknown variant 'thinking'`, rebuild recursive from source. The bridge itself is provider-agnostic — it only forwards recursive's `--json` events.
- **`resume --from-file -p` requires recursive 0.7.0+.** Native session-id resume with a next-turn message (`resume --from-file <dir> -p <msg>`) and the synthetic "Continue from where you left off." fallback were added in recursive 0.7.0+. Older binaries' `resume` re-runs the original goal instead of accepting a new message — the bridge will not work multi-turn against them.

## Local test

```bash
cd ~/projects/my-app

# Using recursive's own config (recursive init already done) — no --env needed:
agentproc hub run recursive -p "reply with exactly: recursive ok"

# Or override per-run. DeepSeek Anthropic endpoint (recursive's default style,
# requires recursive 0.7.0+ for thinking-block support):
agentproc hub run recursive -p "reply with exactly: recursive ok" \
  --env RECURSIVE_API_KEY="$DEEPSEEK_API_KEY" \
  --env RECURSIVE_PROVIDER=anthropic \
  --env RECURSIVE_API_BASE=https://api.deepseek.com/anthropic \
  --env RECURSIVE_MODEL=deepseek-v4-flash

# Or the OpenAI-compatible endpoint:
agentproc hub run recursive -p "reply with exactly: recursive ok" \
  --env RECURSIVE_API_KEY="$DEEPSEEK_API_KEY" \
  --env RECURSIVE_PROVIDER=openai \
  --env RECURSIVE_API_BASE=https://api.deepseek.com/v1 \
  --env RECURSIVE_MODEL=deepseek-chat
```

Expected (stderr / stdout):

```
AGENT_SESSION:rc-<uuid>
recursive ok
```

Multi-turn:

```bash
agentproc hub run recursive -p "remember the word: banana" 2>/tmp/err.log
sid=$(grep '^agentproc:session:' /tmp/err.log | cut -d: -f3)
agentproc hub run recursive -p "what word did I ask you to remember?" --session "$sid"
# → banana
```

## License

MIT. The `recursive` CLI is licensed separately under its own terms.
