# aider

Wraps [aider](https://aider.chat) AI pair programming tool as an AgentProc agent. **One-shot mode** — aider processes a single message, modifies files in your working directory, optionally makes a git commit, and exits. The bridge forwards aider's stdout (a human-readable summary) as the reply body.

## Quick test (zero config)

```bash
# 1. Install aider:
pip install aider-chat

# 2. Set your API key:
export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY for OpenAI models

# 3. From a git project directory:
cd ~/projects/my-app
agentproc hub run aider -p "add a README.md with a brief project description"
```

aider will read your codebase via its repo map, make the change, and return the summary.

## Setup (if you can't use `hub run`)

1. Install aider and set your API key (see above).
2. Copy the profile:

   ```bash
   agentproc hub install aider    # creates ./aider/
   # or, from a repo checkout:
   cp -r hub/aider ./aider
   ```

3. Run against the local copy:

   ```bash
   agentproc --profile ./aider/profile.yaml \
             --prompt "add type hints to all functions" \
             --cwd /path/to/your/project
   ```

## Profile

```yaml
command: python3 {{PROFILE_DIR}}/bridge.py    # or: node {{PROFILE_DIR}}/bridge.js
timeout_secs: 600
streaming: false                      # --no-stream collects full output
env:
  AIDER_MODEL: "${AIDER_MODEL}"       # optional model override
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
  OPENAI_API_KEY: "${OPENAI_API_KEY}"
env_allowlist: [AIDER_MODEL, ANTHROPIC_API_KEY, OPENAI_API_KEY]
```

## Local test

```bash
cd hub/aider
AGENT_MESSAGE="reply with exactly: aider ok (do not edit any files)" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="0" \
python3 bridge.py
```

Expected: stdout contains `aider ok` (plus aider's usual banner/summary output).

## How it works

```
AGENT_MESSAGE
  ↓
bridge.py / bridge.js
  ↓ aider --message <msg> --yes-always --no-show-release-notes --no-stream [--model <m>]
aider CLI
  ↓ reads cwd via repo map → LLM → edits files → optional git commit
  ↓ human-readable summary on stdout (after the turn completes)
bridge.py / bridge.js
  ↓ reply body (no AGENT_SESSION: line, no AGENT_PARTIAL: lines)
```

## Session continuity

aider does not expose a session id. Instead, it uses the **git history and a repo map** to understand project context — each fresh aider process reads the current git state. For multi-turn conversations in a git repo, context is therefore preserved automatically via the repo itself, not via the AgentProc session mechanism.

For projects without git, there is no automatic context continuity. Use the [AgentProc SDK](https://agentproc.dev/sdk/python) history helpers to prepend prior conversation context to each message.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | depends | Anthropic API key (for claude-* models). |
| `OPENAI_API_KEY` | depends | OpenAI API key (for gpt-* models). |
| `AIDER_MODEL` | no | Model override (e.g. `claude-opus-4-5`, `gpt-4o`). Aider's default is `claude-3-7-sonnet`. |
| `AIDER_TIMEOUT` | no | Process timeout in seconds (default: 600). |

## Caveats

- **Modifies files.** aider edits your working directory. Run it in a git repo so you can review and revert changes.
- **No streaming.** `--no-stream` returns the full response at the end.
- **No AGENT_SESSION:.** Session continuity is git-based, not AgentProc-native. The bridge emits no `AGENT_SESSION:` line.
- **No explicit file targeting.** The bridge does not pass specific files; aider uses its repo-map heuristic to find relevant files. Add files to aider's context by including them in your message (e.g. `"edit src/utils.py: add type hints"`).
- **Auto-confirm.** `--yes-always` bypasses all aider confirmation prompts. Aider will edit and commit without asking.

## License

MIT. `aider` retains its own license (Apache 2.0).
