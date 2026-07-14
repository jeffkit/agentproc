# codebuddy

Wraps the [`codebuddy`](https://copilot.tencent.com) CLI (Tencent CodeBuddy) as an AgentProc agent with streaming and session continuity.

CodeBuddy's `--output-format stream-json` produces a schema compatible with Anthropic's `claude` CLI, so this bridge is structurally identical to the [claude-code bridge](../claude-code/) — only the command name, the resume flag (`-r` instead of `--resume`), and the auth flow differ.

## Quick test (zero config)

```bash
# 1. Install codebuddy (distributed by Tencent — see your internal docs).
# 2. Authenticate by running `codebuddy` once interactively (login flow).

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run codebuddy -p "what is this codebase?"
```

No YAML editing. `agentproc hub run` uses your current directory as the agent's `cwd`, and locates the bundled `bridge.py` via `{{PROFILE_DIR}}`.

## Setup (if you can't use `hub run`)

1. Install `codebuddy` (distributed by Tencent — see your internal docs).
2. Authenticate by running `codebuddy` once interactively and following the login flow.
3. Copy the profile into your project:

   ```bash
   agentproc hub install codebuddy    # creates ./codebuddy/
   # or, from a repo checkout:
   cp -r hub/codebuddy ./codebuddy
   ```

4. Run against the local copy. Use `--cwd` to tell `codebuddy` which project to work on:

   ```bash
   agentproc --profile ./codebuddy/profile.yaml \
             --prompt "explain this codebase" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
# cwd: omitted — `hub run` defaults it to your current directory.
#       With --profile, pass --cwd explicitly.
timeout_secs: 600
streaming: true
env:
  # Auth handled by codebuddy's own login.
  # Optional overrides:
  # CODEBUDDY_MODEL: "claude-sonnet-4.6"
  # CODEBUDDY_DISALLOW_TOOLS: "AskUserQuestion,WebSearch"
```

## Local test

```bash
cd ~/projects/my-app
agentproc hub run codebuddy -p "reply with exactly: codebuddy ok"
```

Expected output (on stderr / stdout):

```
{"type":"partial","text":"codebuddy ok","session_id":"53bb6352-4b47-43fc-bce6-eaf808d419da"}
{"type":"result","text":"","session_id":"53bb6352-4b47-43fc-bce6-eaf808d419da"}
```

<details>
<summary>Drive the bridge script directly (without the CLI)</summary>

```bash
cd hub/codebuddy
echo '{"type":"turn","message":"reply with exactly: codebuddy ok","session_id":"","from_user":"u1","protocol_version":"0.4"}' | python3 bridge.py
```

</details>

## How it works

Same shape as [claude-code](../claude-code/#how-it-works), with these deltas:

| Aspect | claude-code | codebuddy |
|--------|-------------|-----------|
| Command | `claude` | `codebuddy` |
| Resume flag | `--resume <id>` | `-r <id>` |
| Disallowed-tools flag | `--disallowed-tools` | `--disallowedTools` (camelCase) |
| Auth env | `ANTHROPIC_API_KEY` | (via `codebuddy` login) |
| Model env | `CLAUDE_MODEL` | `CODEBUDDY_MODEL` |

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `CODEBUDDY_MODEL` | no | Model override. |
| `CODEBUDDY_DISALLOW_TOOLS` | no | Comma-separated disallowed tools (default: `AskUserQuestion`). |

## Optional tool permission

**Not supported.** CodeBuddy's headless docs mark `--permission-prompt-tool` as unsupported. If you set `permission: true` in the profile, this bridge emits a `{"type":"error"}` event instead of silently falling back to `--dangerously-skip-permissions`.

For mid-turn IM tool authorization, use [claude-code](../claude-code/) (`permission: true`) or [codex](../codex/) (`permission: true` via Codex `PermissionRequest` hooks). See [PERMISSIONS.md](../PERMISSIONS.md).

## Caveats

- Same `--dangerously-skip-permissions` trade-off as claude-code; sandbox if concerned.
- `codebuddy` is currently Tencent-internal distribution; this profile is provided for users who already have access. Open-source users without a CodeBuddy account should use the [claude-code](../claude-code/) profile instead.

## License

MIT. The `codebuddy` CLI is © Tencent and licensed separately.
