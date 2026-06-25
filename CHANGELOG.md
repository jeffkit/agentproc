# Changelog

All notable changes to AgentProc are documented here. The protocol version and the SDK package versions are kept in lockstep.

## 0.2.1 — 2026-06-25

### Python CLI

The Python package now ships the `agentproc` CLI alongside the SDK. After `pip install agentproc` (or `pipx install agentproc`), the `agentproc` command is available — same flags, same output semantics as the Node CLI.

- New `sdk/python/src/agentproc/runner.py` — the canonical bridge-side engine in Python (mirrors `sdk/node/src/runner.js`)
- New `sdk/python/src/agentproc/cli.py` — argparse-based CLI entry point
- `[project.scripts] agentproc = "agentproc.cli:main"` added to `pyproject.toml`
- 60 new tests in `tests/test_runner.py` (parity with Node's `runner.test.js`)

Both language implementations stay at parity: same protocol behavior, same exit codes, same CLI flags. Pick whichever language fits your stack.

### Docs: homepage onboarding flow

The homepage (`/` and `/zh/`) now has a 4-step "Get started in 5 minutes" guide:

1. **Install** — npm / pipx / pip in a code-group
2. **Pick a profile** — links to the 5 official hub profiles
3. **Run it** — echo-agent smoke test, then a real claude-code call with session capture
4. **Connect to messaging** — a ~30-line Node.js bridge example showing how AgentProc fits between your platform and the agent

Feature cards updated to lead with concrete value (5-minute setup, supported CLIs, any platform, open spec) instead of protocol mechanics.

## 0.2.0 — 2026-06-25

### agentproc CLI

The Node SDK package now ships a canonical bridge-side runner. Install it globally and drive any profile:

```bash
npm install -g agentproc
agentproc --profile hub/claude-code/profile.yaml --prompt "hello"
```

Features:

- `--profile <path.yaml>` + `--prompt <text>` (or `--stdin`)
- `--session <id>` for multi-turn continuity
- `--cwd`, `--env KEY=VALUE` (repeatable), `--timeout` overrides
- `--no-stream` to set `AGENT_STREAMING=0`
- `--verbose` (default) / `--quiet` for protocol line visibility
- `--raw` for verbatim stdout passthrough
- Final session id printed on stderr as `agentproc:session:<id>` for shell capture
- Exit codes per spec (0 / 1 / 124)

The CLI is a thin wrapper over the new `run()` function in `sdk/node/src/runner.js`, which is the canonical reference implementation of the bridge-side contract. Implementers writing bridges in other languages can read `runner.js` as the spec in code form.

Runner tests: 60 unit + end-to-end tests covering profile parsing, placeholder substitution, env injection, stdout classification (session/partial/error/body), timeout/SIGTERM, stdin EOF, exit codes.

### Profile Hub

Five drop-in profiles added under `hub/`, each with `profile.yaml` + `bridge.py` + `bridge.js` + `README.md`:

| Profile | CLI | Tested |
|---------|-----|--------|
| `claude-code` | Anthropic Claude Code | official |
| `codex` | OpenAI Codex | official |
| `codebuddy` | Tencent CodeBuddy | official |
| `agy` | agy | community |
| `echo-agent` | (no CLI, hello world) | official |

CI added: `test-hub-bridges` job does syntax checks, echo-agent end-to-end, and profile YAML schema validation.

### Docs

- New `/cli/` VitePress section (EN + ZH)
- Hub pages documented at `/hub/`
- `llms.txt` and `llms-full.txt` updated with hub + CLI sections
- `AGENTS.md` and `CONTRIBUTING.md` reflect the new structure

### No protocol changes

0.2.0 adds tooling and profiles, but does not change the wire protocol. Existing 0.1 agents and bridges remain compatible.

## 0.1.1 — 2026-06-25

Republish to align the PyPI release with npm. The initial 0.1.0 tag fired the publish workflow before tests were wired into CI; the Python package was uploaded to PyPI from a pre-revision commit and never received the protocol/SDK improvements. npm 0.1.0 was published from the correct tree.

**No protocol or API changes vs 0.1.0.** Use 0.1.1+ for both SDKs.

## 0.1.0 — 2026-06-25

First public draft of the protocol and SDKs.

### Protocol

Initial definition of the AgentProc process-based interface between a messaging bridge and an agent process.

**Input contract:**

- Environment variables: `AGENT_MESSAGE`, `AGENT_SESSION_ID`, `AGENT_SESSION_NAME`, `AGENT_FROM_USER`, `AGENT_STREAMING`
- `AGENT_PROTOCOL_VERSION` injected by the bridge (currently `"0.1"`)
- Attachment env vars: `AGENT_IMAGE_URL`, `AGENT_FILE_URL` (P0, single attachment)
- `AGENT_ATTACHMENTS` JSON array (draft, multi-attachment) — agents SHOULD prefer it when present
- Optional stdin write when profile `stdin: message`; bridge closes stdin (EOF) after writing

**Output contract (sentinel-prefixed stdout lines):**

- `AGENT_SESSION:<opaque-id>` — declare session ID; **may appear anywhere; last occurrence wins**
- `AGENT_PARTIAL:<json-string>` — streaming chunk (ignored when `streaming: false`)
- `AGENT_ERROR:<json-string>` — error message forwarded to the user regardless of streaming mode
- All other lines = final reply body
- Reply body MUST NOT start with `AGENT_SESSION:`, `AGENT_PARTIAL:`, or `AGENT_ERROR:`
- JSON parse failure on `AGENT_PARTIAL:` defaults to lenient mode (raw text forwarded, warning logged)

**Profile fields:**

- `command` — split on whitespace into argv, NOT passed through a shell
- `args`, `cwd`, `env` — support `{{MESSAGE}}`, `{{SESSION_ID}}`, `{{SESSION_NAME}}` placeholders
- `stdin` — `none` (default) | `message`
- `timeout_secs` (default 1800), `kill_grace_secs` (default 5) — SIGTERM then SIGKILL
- `max_reply_chars` (default 8000), `truncation_suffix`
- `include_stderr_in_reply`, `send_error_reply`, `streaming`

**Exit codes:** `0` success · `1` error · `124` timeout · `130` SIGINT · `143` SIGTERM.

### SDKs

Python (`agentproc`) and Node.js (`agentproc`) SDKs released at parity.

- `create_profile(handler)` / `createProfile(handler)` — async handler with `AgentContext`
- `ctx.send_partial(text)` / `ctx.sendPartial(text)` — streaming
- `ctx.send_error(text)` / `ctx.sendError(text)` — surface user-readable errors
- `raise ProtocolError(text)` (Python) / `throw await sdk.protocolError(text)` (Node) — exception form
- `ctx.attachments` — parsed `AGENT_ATTACHMENTS` (draft)
- `ctx.protocol_version` / `ctx.protocolVersion`
- History helpers: `load_history` / `loadHistory`, `append_history` / `appendHistory`, `session_file_path` / `sessionFilePath`

### Docs

- VitePress site (`docs/`) in English and Chinese
- Protocol spec at `spec/protocol.md` and `spec/protocol.zh.md`
- Comparison with MCP, ACP, NDJSON, SSE, LSP, Unix filter
- Design rationale (why env vars, why sentinel lines, why last-wins, why AGENT_ERROR)

### Tests

- Node SDK: 25 tests via `node --test`
- Python SDK: 29 tests via `pytest`
