# dsh

Wraps the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) CLI (`dsh`) as an AgentProc agent, via its **headless profile** — a one-shot, full agent runtime with a coding persona, bash/fs/search tools, and a sandbox. Unlike the `deepseek` TUI profile (which shells out to a stateless chat exec), this profile runs a complete harness turn: the agent can read your project, run commands, and answer with the results.

> Verified end-to-end against `dsh` 0.1.7-rc.2 (streamed reply with partials, a real bash tool-use turn, two-turn session resume with cross-turn memory, usage reporting, and the unknown-session error path, through the agentproc bridge). dsh is a developer preview — re-verify on major bumps.

## Quick test (zero config)

```bash
# 1. Install the dsh CLI (Node.js >= 22.18 / >= 24.2, or Bun — the launcher
#    entry reads import.meta.main):
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
| Reply body | Final assistant message (`result`) |
| Streaming (`partial`) | ✓ feature-detected — dsh ≥ 0.1.6-alpha.1 (`--json` in `--help`) streams committed text as partials; commit-point granularity, not per-token |
| Session continuity (`session_id`) | ✓ feature-detected — with `--session-id` advertised, the bridge stamps the id from the opening frame and resumes the persisted Session on later turns; older builds stay stateless |
| Usage | ✓ on `result`/`error` — dsh ≥ 0.1.6-alpha.1, per-turn totals (see mapping below) |
| Tools | ✓ bash / fs / fs-search / skills (agent-grade turn against your `cwd`) |
| Attachments | Partial — appended to the task text as reference URLs; dsh's web tool may fetch public URLs |
| Mid-turn approval (`permission: true`) | ✗ — no stdio approval channel in headless (see below) |

The underlying session is always persisted (`~/.dsh/sessions`, v4 JSONL,
zstd-compressed by default): inspect or replay a run with
`dsh --profile tui --resume <session>`.

## How the bridge decides success

dsh's launcher maps SIGTERM to exit 0, and a turn that ends in an error
reason still writes its `final` event — so the bridge trusts frames, not the
exit code: a turn counts as successful only when a `final` event arrived and
the exit code was 0. Driver failures (unknown session, missing credential,
adoption refusal) arrive as an `error` event with no final.

## Session continuity, precisely

- The session id comes from the run stream's opening `session` event; the
  bridge stamps it on every event it emits.
- A later turn passes `--session-id <id>`; dsh **adopts** the persisted
  Session — an unknown id is an error, not a new session.
- Adoption is strict upstream: same cwd, no subagent/fork, no agent preset.
  A mismatch (e.g. the bridge runs from a different directory) surfaces as an
  error event naming the conflict. Keep the bridge cwd stable for continuity.

## Usage mapping

dsh reports token counts in **disjoint** buckets (`inputTokens` is uncached
input only; billed input = input + cacheRead + cacheWrite). agentproc's
`input_tokens` is the inclusive billed input, so the bridge folds the cache
buckets in and passes the rest through:

| agentproc key | dsh field |
|---|---|
| `input_tokens` | `inputTokens + cacheReadTokens + cacheWriteTokens` |
| `output_tokens` | `outputTokens` |
| `total_tokens` | `totalTokens` |
| `cache_read_input_tokens` | `cacheReadTokens` |
| `cache_creation_input_tokens` | `cacheWriteTokens` |
| `reasoning_tokens` | `reasoningTokens` |

Optional buckets a turn omits are omitted from the mapping too. `thinking`
frames (reasoning projection) stay off the wire, matching the claude-code
profile's text-deltas-only posture.

## Configuration

Environment (in the profile `env` block, overridable by your bridge env):

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | API key; empty falls through to dsh's stored credentials (web Models page) |
| `DSH_PERMISSION_MODE` | `danger-full-access` (bridge default) | Tool authorization posture — see below |
| `DSH_TOOLS_MODE` | — | Optional dsh Code Mode opt-in |
| `DSH_TIMEOUT` | `1800` | Per-turn process timeout (seconds) |
| `AGENTPROC_AUTO_APPROVE` | — | Read from the **bridge's own** environment; `0` / `false` stops the bridge injecting `DSH_PERMISSION_MODE=danger-full-access` |

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

That default is a controllable switch, not a hard-coded deployment posture:
`AGENTPROC_AUTO_APPROVE=0` (or `false`, case-insensitive) makes the bridge skip
the injection entirely, leaving dsh's own "ask" policy in place (fail-closed).
It does **not** override an explicit `DSH_PERMISSION_MODE` from the profile —
only the bridge's own default.

The variable is a process-side knob read from the bridge's environment, and the
runner's child env only carries the infra set + the profile `env` block +
`--env` extras, so you have to pass it in explicitly:

```
# CLI flag — simplest
agentproc ... --env AGENTPROC_AUTO_APPROVE=0
```

```yaml
# profile env block — note the env_allowlist entry, or ${VAR} expands to ""
env:
  AGENTPROC_AUTO_APPROVE: "${AGENTPROC_AUTO_APPROVE}"
env_allowlist: [DEEPSEEK_API_KEY, DSH_PERMISSION_MODE, DSH_TOOLS_MODE, AGENTPROC_AUTO_APPROVE]
```

## Limitations and upgrade paths

- **Legacy fallback.** Without `--json` in the headless `--help` (pre-0.1.6
  builds), the bridge uses plain one-shot stdout and stays stateless; use the
  AgentProc SDK's `load_history` / `append_history` helpers for continuity on
  such builds.
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
