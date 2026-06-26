# Profile Hub

The hub is a curated collection of ready-to-use AgentProc profiles for popular AI agent CLIs. **You don't need to clone the repo, copy files, or edit YAML** — the `agentproc` CLI fetches profiles on demand, caches them locally, and runs them.

## One-line usage

```bash
# pick any profile, point at any directory, just go
cd ~/projects/my-app
agentproc hub run claude-code -p "what is this codebase?"
```

That's it. The CLI:

1. Fetches `hub/claude-code/` from GitHub on first use
2. Caches it at `~/.agentproc/cache/hub/claude-code/` (24h TTL)
3. Uses **your current directory** as the agent's `cwd` (override with `--cwd`)
4. Locates the bundled bridge script via a `{{PROFILE_DIR}}` placeholder, so `cwd` and script location are decoupled
5. Forwards `AGENT_MESSAGE` and any `--env` you pass

::: tip Hitting GitHub rate limits?
Anonymous fetches are capped at ~60/hour per IP. Raise to 5,000/hour with:

```bash
export GITHUB_TOKEN=$(gh auth token)   # or any personal access token
```

The CLI sends `Authorization: Bearer <token>` when `GITHUB_TOKEN` (or `GH_TOKEN`) is set. If you'd rather skip the network entirely, run against a local checkout: `agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"`.
:::

## All hub commands

| Command | Purpose |
|---------|---------|
| `agentproc hub list` | List all profiles in the hub |
| `agentproc hub show <name>` | Show a profile's README |
| `agentproc hub run <name> [opts]` | Fetch (if needed) and run a profile |
| `agentproc hub install <name>` | Copy a profile to the current directory (for editing) |

Add `--refresh` to force re-fetch from GitHub.

## Available profiles

| Profile | CLI | Tested | Languages |
|---------|-----|--------|-----------|
| [claude-code](https://github.com/jeffkit/agentproc/tree/main/hub/claude-code) | `claude` (Anthropic) | official | Python · Node |
| [codex](https://github.com/jeffkit/agentproc/tree/main/hub/codex) | `codex` (OpenAI) | official | Python · Node |
| [codebuddy](https://github.com/jeffkit/agentproc/tree/main/hub/codebuddy) | `codebuddy` (Tencent) | official | Python · Node |
| [agy](https://github.com/jeffkit/agentproc/tree/main/hub/agy) | `agy` | community | Python · Node |
| [echo-agent](https://github.com/jeffkit/agentproc/tree/main/hub/echo-agent) | (no CLI) | official | Python · Node · Bash |

`tested`:
- **official** — verified by maintainers against the CLI's documented behavior
- **community** — submitted and reportedly working; not verified end-to-end
- **unverified** — submitted without verification

## Examples

### Browse and try

```bash
# See what's available
agentproc hub list
#   claude-code     official    Connect the claude CLI as an AgentProc agent...
#   codex           official    Connect the codex CLI as an AgentProc agent...
#   codebuddy       official    Connect the codebuddy CLI as an AgentProc agent...
#   agy             community   Connect the agy CLI as an AgentProc agent...
#   echo-agent      official    Minimal AgentProc hello-world agent...

# Read its docs
agentproc hub show claude-code

# Run a smoke test (no API key needed)
agentproc hub run echo-agent -p "hello"
# → You said: hello
```

### Use a real CLI

```bash
cd ~/projects/my-app

# claude-code
agentproc hub run claude-code \
  -p "explain this codebase" \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"

# codex
agentproc hub run codex \
  -p "find the bug in src/auth.py" \
  --env OPENAI_API_KEY="$OPENAI_API_KEY"

# codebuddy (uses its own login)
agentproc hub run codebuddy -p "refactor this function"
```

### Multi-turn

```bash
agentproc hub run claude-code -p "what files are in this dir?" 2>/tmp/err.log
session=$(grep '^agentproc:session:' /tmp/err.log | cut -d: -f3)
agentproc hub run claude-code -p "now read src/main.py" --session "$session"
```

### Install for local editing

If you want to own a profile and customize it:

```bash
agentproc hub install claude-code
# → installed to: ./claude-code/

# Edit ./claude-code/profile.yaml however you like
agentproc --profile ./claude-code/profile.yaml -p "hi" --cwd ./claude-code
```

## How the cache works

- Cache location: `~/.agentproc/cache/hub/<name>/`
- TTL: 24 hours (after fetch, cached copy used without network)
- Force refresh: pass `--refresh` to any hub command
- Each profile is a flat directory containing `profile.yaml`, `bridge.py`, `bridge.js`, and `README.md`

The CLI uses GitHub's git-tree API (1 request lists everything) and raw.githubusercontent.com (no rate limit) so the experience stays fast even for unauthenticated users.

## Profile schema

```yaml
name: <kebab-case-id>           # required, matches directory name
description: <one-line>
cli: <command-name>             # the executable this wraps
cli_install: |                  # how to install the CLI itself
  npm install -g ...
agentproc:                      # the actual AgentProc P0 profile
  command: python3 {{PROFILE_DIR}}/bridge.py   # {{PROFILE_DIR}} resolves to the profile's own directory
  # cwd intentionally omitted: `hub run` defaults it to the user's current
  # directory (so the wrapped CLI operates on their project). The bridge
  # script is located via {{PROFILE_DIR}} regardless of cwd.
  timeout_secs: 600
  streaming: true
  env:
    API_KEY: "${API_KEY}"       # env-var references resolved at run time
tested: official | community | unverified
maintainer: <github-handle>
tags: [<category>, ...]
notes: |                        # optional caveats, gotchas
  ...
```

Hub profiles are **pure AgentProc P0** — they don't use bridge-specific `type:` shortcuts. Any conformant bridge can drive them.

## Contributing a new profile

1. Create `hub/<cli-name>/` with `profile.yaml`, `bridge.py`, `bridge.js`, `README.md` in the [agentproc repo](https://github.com/jeffkit/agentproc).
2. Set `tested: unverified` unless you've verified end-to-end.
3. Add an entry to the table in [`hub/README.md`](https://github.com/jeffkit/agentproc/blob/main/hub/README.md).
4. Open a PR. A maintainer will review, possibly test, and bump `tested` accordingly.

See [`CONTRIBUTING.md`](https://github.com/jeffkit/agentproc/blob/main/CONTRIBUTING.md) for repo-wide conventions.

## Relationship to ilink-hub-bridge

AgentProc was extracted from [`ilink-hub-bridge`](https://github.com/jeffkit), a messaging-platform bridge with built-in `type:` handlers for `claude-code`, `cursor`, `codebuddy-code`, and others. We realized the bridge↔agent contract was reusable independently — and AgentProc was born.

Hub profiles are **pure-P0 re-implementations** of what those `type:` handlers do internally. They work with any conformant bridge, not just one specific implementation.
