# Troubleshooting

A focused list of the failure modes you're most likely to hit, and the exact fix for each. If you're stuck on something not listed here, [open an issue](https://github.com/jeffkit/agentproc/issues).

## Quick decision tree

```
What does the error say?
│
├─ "GitHub rate-limited the hub fetch"
│   → See: Hub fetch failed / rate limit
│
├─ "profile '<name>' not found in hub"
│   → See: Wrong profile name
│
├─ "[agentproc runner] spawn error: spawn X ENOENT"
│   → See: spawn ENOENT
│
├─ "AGENT_ERROR:..." on stderr
│   → The agent itself failed. See: AGENT_ERROR from the wrapped CLI
│
├─ Hangs / no output
│   → See: Agent runs but nothing comes back
│
└─ Exit code is 124
    → See: Timeout
```

---

## Hub fetch failed / rate limit

### Symptom

```
fetching profile 'claude-code' from jeffkit/agentproc:main...
error: GitHub rate-limited the hub fetch (HTTP 403)

GitHub limits anonymous hub fetches to ~60/hour. ...
```

### Cause

The CLI fetches hub profiles from `github.com/jeffkit/agentproc`. Anonymous GitHub API requests are capped at ~60 per IP per hour. A CI runner, a shared office NAT, or just running `hub list` / `hub show` / `hub run` a few times can blow through that.

### Fix (fastest to slowest)

1. **Wait it out.** The limit resets every hour. Cached profiles (`~/.agentproc/cache/hub/<name>/`, 24h TTL) still work without re-fetching.
2. **Set a token.** Authenticated requests get 5,000/hour:
   ```bash
   export GITHUB_TOKEN=$(gh auth token)   # if you have the GitHub CLI
   # or set GITHUB_TOKEN to any personal access token (no scopes needed for public repos)
   ```
3. **Skip the network entirely.** Run against a local checkout:
   ```bash
   git clone https://github.com/jeffkit/agentproc
   cd agentproc
   agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"
   ```

---

## Wrong profile name

### Symptom

```
error: profile 'claude-codex' not found in hub

Did you mean `claude-code`?

Available profiles:
  - claude-code
  - codex
  - echo-agent
  ...
```

### Cause

Typo in the profile name. The CLI fetches the hub tree, finds no `hub/<name>/` directory matching what you typed, and suggests the closest match.

### Fix

Use the suggested name, or browse what's available:

```bash
agentproc hub list
```

---

## spawn ENOENT

### Symptom

```
[agentproc runner] spawn error: spawn python3 ENOENT
[agentproc runner] hint: <one of the messages below>
AGENT_ERROR:"failed to start agent: ..."
```

The CLI gives you a tailored hint depending on the cause — but here's the background.

### Cause 1: `profile.cwd does not exist: <path>`

You passed `--cwd /some/path` (or your profile sets `cwd:`) and that directory doesn't exist.

**Fix:** Point `--cwd` at a real directory:
```bash
agentproc hub run claude-code -p "hi" --cwd /actual/path/to/your/project
```

### Cause 2: `'python3' not found on PATH`

The bridge spawns the agent via Node's `child_process.spawn`, which inherits the parent process's `PATH`. If the CLI is launched from a context where PATH doesn't include your interpreter (common with systemd, cron, GUI launchers, some IDEs), the spawn fails even though `python3` works in your shell.

**Fix:** Either:
- Make sure PATH includes the interpreter (e.g. symlink or full path).
- Or use a Node bridge script instead of Python (`command: node {{PROFILE_DIR}}/bridge.js`) — Node is obviously available since the CLI ships on it.

### Cause 3: `'claude' not found on PATH` (the wrapped CLI is missing)

You ran e.g. `hub run claude-code` but never installed the `claude` CLI.

**Fix:** Install the wrapped CLI per the profile's README:
```bash
npm install -g @anthropic-ai/claude-code   # for claude-code
npm install -g @openai/codex                # for codex
```

Verify with `agentproc hub show <name>` — each profile's README lists its install command.

### Cause 4: `argument file not found: ./bridge.py`

You're running an old hub profile (or a hand-edited one) that uses `command: python3 ./bridge.py` with a relative path, and the agent's `cwd` doesn't contain that file.

**Fix:** Re-install or refresh the profile (current profiles use `{{PROFILE_DIR}}/bridge.py` which always resolves correctly):
```bash
agentproc hub install claude-code --refresh
```

---

## AGENT_ERROR from the wrapped CLI

### Symptom

```
AGENT_ERROR:"API Error: 400 [1211][模型不存在...]"
agentproc:error:API Error: 400 ...
```

### Cause

The wrapped CLI ran but returned an error. The bridge forwarded it to you via `AGENT_ERROR:` (per the protocol spec). The CLI also surfaces it on stderr as `agentproc:error:<message>`.

### Common sub-cases

#### Model not found / invalid model

The wrapped CLI is calling a model that doesn't exist on your account/endpoint.

**Fix:** Pass the right model env var. Each profile has its own (see its README):
```bash
# claude-code:
agentproc hub run claude-code -p "hi" \
  --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  --env CLAUDE_MODEL="claude-sonnet-4-6"

# codex:
agentproc hub run codex -p "hi" \
  --env OPENAI_API_KEY=$OPENAI_API_KEY \
  --env CODEX_MODEL="gpt-5"
```

#### Missing API key

**Fix:** Pass the key via `--env`:
```bash
agentproc hub run claude-code -p "hi" --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
```

#### Auth expired / wrong endpoint

If you're routing through a proxy or third-party endpoint (e.g. a Chinese mirror for Anthropic), the key may be valid but pointed at the wrong base URL. Check the wrapped CLI's own auth docs.

---

## Agent runs but nothing comes back

### Symptom

`hub run` exits 0, no error, but stdout is empty.

### Cause & fix

1. **Streaming mode, but the agent's reply was emitted via `AGENT_PARTIAL:` lines only.** With `--quiet`, partials are suppressed and you see nothing. Re-run without `--quiet`, or capture the session id from stderr to verify the agent did respond.
2. **The wrapped CLI wrote everything to its own stderr, not stdout.** Some CLIs do this for warnings. Run with `--verbose` (the default) and check stderr. If you want stderr in the reply, set `include_stderr_in_reply: true` in the profile.
3. **The agent exited 0 without writing anything.** This is a bug in the agent script, not in AgentProc. Run the agent directly with the env vars set to see what it does:
   ```bash
   AGENT_MESSAGE="hi" AGENT_STREAMING="1" python3 ./bridge.py
   ```

---

## Timeout (exit code 124)

### Symptom

The CLI runs for a while, then exits with code 124. No reply.

### Cause

The agent didn't finish within `timeout_secs` (default 1800s; some hub profiles set 600s).

### Fix

- **If the agent is genuinely slow** (large codebase, big model): raise the timeout.
  ```bash
  agentproc hub run claude-code -p "..." --timeout 1800
  ```
- **If the agent is hung** (waiting on interactive prompt, network stall): the wrapped CLI likely tried to prompt interactively, which AgentProc can't satisfy. Verify the profile passes `--dangerously-skip-permissions` or equivalent non-interactive flags. The hub profiles already do this; if you're using your own, check that.

---

## "Could not reach GitHub"

### Symptom

```
error: could not reach GitHub while fetching hub profile

This is usually a transient network issue. Try: ...
```

### Cause

`fetch()` itself threw — DNS failure, connection refused, reset, etc. Not a rate limit.

### Fix

- Retry (transient failures usually clear in seconds).
- If you're behind a proxy, set `HTTPS_PROXY`:
  ```bash
  export HTTPS_PROXY=http://your-proxy:port
  ```
- Otherwise, run against a local checkout (see Hub fetch failed → Fix 3 above).

---

## Still stuck

- [Open a GitHub issue](https://github.com/jeffkit/agentproc/issues) — include the exact command, the full stderr output, and `agentproc --version`.
- The [`runner.js` source](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js) is the spec in code form — read it as the canonical reference for what the bridge does.
