# dsh

Wraps the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) CLI (`dsh`) as an AgentProc agent, via its **headless profile** — a one-shot, full agent runtime with a coding persona, bash/fs/search tools, and a sandbox. Unlike the `deepseek` TUI profile (which shells out to a stateless chat exec), this profile runs a complete harness turn: the agent can read your project, run commands, and answer with the results.

> Verified end-to-end against `dsh` 0.1.0-rc.6 (plain reply, error path, and a real bash tool-use turn through the agentproc runner). dsh is a developer preview — re-verify on major bumps.

## Quick test (zero config)

```bash
# 1. Install the dsh CLI (Node.js >= 20):
npm install -g @deepseek-ai/dsh

# 2. Authenticate (either way works):
export DEEPSEEK_API_KEY=...
# or store it once via the web UI — headless reads stored credentials too:
dsh --profile web        # Models page writes the key, then exit

# 3. From your project directory:
cd ~/projects/my-app
agentproc hub run dsh -p "what is this codebase?"
```

## Setup (if you can't use `hub run`)

1. Install the CLI and authenticate (see above).
2. Copy the profile:

   ```bash
   agentproc hub install dsh    # creates ./dsh/
   # or, from a repo checkout:
   cp -r hub/dsh ./dsh
   ```

3. Run it with any conformant bridge, or directly:

   ```bash
   echo '{"type":"turn","message":"hello","session_id":"","session_name":"default","attachments":[],"permission":false,"protocol_version":"0.4"}' \
     | node ./dsh/bridge.js
   ```

## What you get

| Capability | Status |
|---|---|
| Reply body | Final assistant message (plain stdout) |
| Streaming (`partial`) | ✗ — headless prints the result once, at the end |
| Session continuity (`session_id`) | ✗ on the wire — headless mints a fresh Agent per run and exposes no `--resume` |
| Tools | ✓ bash / fs / fs-search / skills (agent-grade turn against your `cwd`) |
| Attachments | Partial — appended to the task text as reference URLs; dsh's web tool may fetch public URLs |
| Mid-turn approval (`permission: true`) | ✗ — no stdio approval channel in headless (see below) |

The underlying session **is** persisted (JSONL under `~/.dsh/sessions`): inspect
or replay a run with `dsh --profile tui --resume <session>` — the id just
can't be learned from the process output, so AgentProc treats this profile as
stateless.

## Configuration

Environment (in the profile `env` block, overridable by your bridge env):

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | API key; empty falls through to dsh's stored credentials (web Models page) |
| `DSH_PERMISSION_MODE` | `danger-full-access` (bridge default) | Tool authorization posture — see below |
| `DSH_TOOLS_MODE` | — | Optional dsh Code Mode opt-in |
| `DSH_TIMEOUT` | `1800` | Per-turn process timeout (seconds) |

### Tool authorization posture

dsh's own default is an "ask" approval policy, which has no UI to answer it in
headless mode — so the bridge defaults `DSH_PERMISSION_MODE=danger-full-access`
(auto-approve), the same unattended convention as the claude-code profile's
`--dangerously-skip-permissions`. To lean on the sandbox instead:

```yaml
env:
  DSH_PERMISSION_MODE: "read-only"      # or workspace-write
```

Note: with an "ask" policy (workspace-write), a headless ask may stall or deny —
prefer `read-only` for strict unattended runs.

## Limitations and upgrade paths

- **Stateless turns.** Multi-turn continuity must be maintained externally via
  the AgentProc SDK's `load_history` / `append_history` helpers. If dsh later
  adds native session resume to headless (or prints the session id), this
  bridge can stamp `session_id` on events — one small change in `bridge.js`.
- **No mid-turn approval.** If dsh grows a stdio approval channel (its web
  profile already has an approval service behind a capability seam), the
  bridge can translate it to `permission_request` / `permission_response`
  frames like the claude-code profile does.
- **Message in child argv.** The task text is passed as the headless
  positional argument (visible to `ps(1)`), matching how the claude-code and
  deepseek profiles pass `-p <message>`. Keep sensitive content out of task
  text on shared machines.

## Files

- `profile.yaml` — AgentProc P0 profile
- `bridge.js` — Node bridge (used by the profile)
- `bridge.py` — Python bridge (equivalent, for runners preferring it)
