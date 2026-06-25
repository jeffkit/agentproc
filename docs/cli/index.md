# agentproc CLI

The `agentproc` command-line tool is the canonical bridge-side runner. It reads a profile YAML, spawns the configured agent process, parses stdout per the protocol spec, and prints the reply. Any conformant agent — from hub profiles to your own — can be driven through this single entry point.

The CLI ships in the same npm package as the Node SDK:

```bash
npm install -g agentproc       # global install
# or:
npx agentproc ...              # run without installing
```

## Quick start

```bash
agentproc --profile hub/echo-agent/profile.yaml --prompt "hello"
# → You said: hello
```

For real CLI profiles from the hub:

```bash
git clone https://github.com/jeffkit/agentproc
cd agentproc

# echo-agent (no API key needed — smoke test)
agentproc --profile hub/echo-agent/profile.yaml \
          --prompt "hello" --cwd hub/echo-agent

# claude-code (requires ANTHROPIC_API_KEY)
agentproc --profile hub/claude-code/profile.yaml \
          --prompt "explain this codebase" \
          --cwd /path/to/your/project
```

## Usage

```
agentproc --profile <path.yaml> --prompt "hello" [options]
```

### Required

| Flag | Description |
|------|-------------|
| `--profile`, `-p <path>` | Profile YAML path |
| `--prompt <text>` | User message (or use `--stdin`) |

### Session

| Flag | Description |
|------|-------------|
| `--session <id>` | Previous session id for multi-turn continuity |
| `--session-name <name>` | Human-readable session name (default: `default`) |
| `--from <user>` | Sender identifier |

### Execution

| Flag | Description |
|------|-------------|
| `--cwd <path>` | Override `profile.cwd` |
| `--env KEY=VALUE` | Extra env var (repeatable) |
| `--timeout <secs>` | Override `profile.timeout_secs` |
| `--no-stream` | Set `AGENT_STREAMING=0` |

### Output

| Flag | Description |
|------|-------------|
| `--verbose` | Forward protocol lines to stderr (default) |
| `--quiet` | Suppress protocol lines on stderr |
| `--raw` | Don't parse stdout; forward agent output verbatim |
| `--stdin` | Read prompt from stdin instead of `--prompt` |

### Other

| Flag | Description |
|------|-------------|
| `--version` | Print version and exit |
| `--help`, `-h` | Show help |

## Output semantics

### Default mode

| Stream | Content |
|--------|---------|
| stderr | Protocol lines (`AGENT_PARTIAL:`, `AGENT_SESSION:`, `AGENT_ERROR:`) in real time |
| stdout | Final reply body (non-protocol lines), printed after the agent exits |
| exit | `0` success · `1` error · `124` timeout (per spec) |

The final session id is also printed on stderr as `agentproc:session:<id>`, so shell scripts can capture it:

```bash
output=$(agentproc -p prof.yaml --prompt "hi" 2>/tmp/err.log)
session=$(grep '^agentproc:session:' /tmp/err.log | cut -d: -f3)
agentproc -p prof.yaml --prompt "follow up" --session "$session"
```

### `--raw` mode

Don't parse stdout at all — forward the agent's stdout verbatim. Useful for piping the agent's raw output to another tool, or for debugging bridge scripts in isolation.

```bash
agentproc -p prof.yaml --prompt "hi" --raw | some-other-tool
```

## Examples

### Multi-turn with claude-code

```bash
# First turn — capture the session id
agentproc -p hub/claude-code/profile.yaml \
          --prompt "what is this codebase?" \
          --cwd ~/projects/myapp \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
          2>/tmp/err.log
session=$(grep '^agentproc:session:' /tmp/err.log | cut -d: -f3)

# Second turn — continue the session
agentproc -p hub/claude-code/profile.yaml \
          --prompt "tell me more about the auth module" \
          --cwd ~/projects/myapp \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
          --session "$session"
```

### Prompt from stdin

```bash
echo "what files are in this directory?" | \
  agentproc -p hub/claude-code/profile.yaml --cwd . --stdin
```

### Quiet mode (clean stdout for piping)

```bash
agentproc -p hub/claude-code/profile.yaml --prompt "summarize" --quiet | jq .
```

## How it implements the spec

The CLI is a thin wrapper over the SDK's `run()` function in [`sdk/node/src/runner.js`](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js). That module is the canonical reference implementation of the AgentProc bridge-side contract:

- **Profile parsing**: accepts both top-level form (`command:` at root) and hub form (`command:` nested under `agentproc:`).
- **Placeholder substitution**: `{{MESSAGE}}`, `{{SESSION_ID}}`, `{{SESSION_NAME}}` in `args`, `cwd`, and `env` values — no shell involved.
- **Env injection**: `AGENT_MESSAGE`, `AGENT_SESSION_ID`, `AGENT_SESSION_NAME`, `AGENT_FROM_USER`, `AGENT_STREAMING`, `AGENT_PROTOCOL_VERSION`, plus `${VAR}` expansion in `profile.env`.
- **stdout classification**: `AGENT_SESSION:` (last wins), `AGENT_PARTIAL:` (JSON lenient mode), `AGENT_ERROR:`, everything else = reply body.
- **stdin contract**: writes the message then EOF when `profile.stdin: message`.
- **Timeout handling**: SIGTERM → `kill_grace_secs` (default 5s) → SIGKILL. Exit code 124.
- **Exit codes**: 0 success · 1 error (including when `AGENT_ERROR:` was emitted) · 124 timeout.

If you're writing your own bridge in another language, [`runner.js`](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js) is the spec in code form.
