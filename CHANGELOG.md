# Changelog

All notable changes to AgentProc are documented here. The protocol version and the SDK package versions are kept in lockstep.

## Unreleased

### Hub: two new profiles + shared bridge module

Added two new NDJSON-based hub profiles and extracted the duplicated bridge logic into a shared module.

**New profiles:**

| Profile | CLI | Tested |
|---------|-----|--------|
| `gemini-cli` | `gemini` (Google Gemini CLI) | official |
| `cursor` | `agent` (Cursor Agent) | official |
| `qwen-code` | `qwen` (Alibaba Qwen Code) | community |

`gemini-cli` was verified end-to-end against the CLI's published `stream-json` event schema (`init` / `message` / `error` / `result`) by reading the source at `google-gemini/gemini-cli/packages/core/src/output/types.ts`. Session id is emitted up-front in the `init` event (unlike claude/codex where it appears in the terminal `result` event).

`cursor` wraps the Cursor Agent CLI (binary name `agent`, not `cursor` — the binary is a standalone download separate from the Cursor IDE). Verified end-to-end against the installed CLI (2026.06.24): schema is claude-code-compatible (`system/init` → session_id, `assistant` content blocks → text deltas, `result` → session_id + result text). Cursor's `--stream-partial-output` flag emits N delta chunks AND THEN a final `assistant` event with the full assembled text; the bridge tracks accumulated emitted text and drops the duplicate via `make_parse_event()` closure state. Multi-turn resume via `--resume <chatId>` confirmed working.

`qwen-code` is a fork of gemini-cli; its `--output-format stream-json` flag set matches gemini's (`-p / -o / -r / -y`), and the bridge reuses the gemini-cli parser. Marked `community` because the published stream-json schema was not verified against a real `qwen` invocation — please report drift.

**Shared bridge module (`hub/_shared/`):**

Three of the existing bridges (`claude-code`, `codex`, `codebuddy`) shared ~80% identical logic: subprocess spawn, NDJSON line reading, JSON decoding, AGENT_* emission, exit-code mapping. Extracted to:

- `hub/_shared/stream_utils.py` — `EventResult` dataclass + `run_bridge()` / `main_entry()`
- `hub/_shared/stream_utils.js` — `runBridge()` + emit helpers

Each bridge is now ~30 lines supplying two functions: `build_args()` (CLI-specific) and `parse_event()` (event-shape-specific). The shared module handles subprocess lifecycle, line reading, dedup, and the streaming-vs-non-streaming emission policy.

**New `partial_text` vs `final_text` split in `EventResult`:**

The previous bridges had a latent duplicate-emission bug: when a CLI streams partial chunks AND THEN emits a `result` event containing the full assembled text (the common Claude / CodeBuddy pattern), the user received the text twice. The shared module fixes this by distinguishing incremental `partial_text` (streamed live) from terminal `final_text` (emitted only in non-streaming mode, or as fallback when nothing was streamed). See `hub/_shared/README.md`.

**Other:**

- Added a coverage matrix to `hub/README.md` tracking AgentProc Hub's coverage of the [ACP Registry](https://agentclientprotocol.com/get-started/registry) agent list. Five ACP-listed agents are covered (4 official, 1 community); seven are explicitly out of scope with documented reasons (Cursor ships inside its app, Goose is cargo-install, Copilot's JSONL schema is undocumented, Junie/Cline/GLM-Agent have no standalone CLI to wrap).
- Bridges that don't fit the NDJSON abstraction (`agy`, `echo-agent`) keep their bespoke implementations — `stream_utils` is opt-in.

### No protocol changes

Still protocol `0.1`. The new profiles and the shared module are bridge-side concerns; agents see the same env vars and emit the same stdout protocol.

## 0.4.0 — 2026-06-26

A round of UX and resilience fixes after running the CLI as a non-coder would. The 5-minute path now actually works on the first try. No protocol changes — `AGENT_*` env vars, stdout sentinels, and profile schema are all backward-compatible.

### Highlights

- **`agentproc hub run <name> -p "hi"` finally works.** The `-p` short flag was treated as a positional in the hub subcommand parser (it's `--profile` short form in the main parser), so the homepage's own smoke-test command failed. Fixed in both Node and Python CLIs.
- **Hub fetch failures are now human-readable.** GitHub 403/404/network errors used to dump raw stack traces. They now raise a typed `HubError` with a remediation hint (set `GITHUB_TOKEN`, run against a local checkout, etc.).
- **Hub profiles no longer ship a `cwd: ~/your-project` placeholder.** All 5 profiles switched to `command: python3 {{PROFILE_DIR}}/bridge.py` with cwd unset — `hub run` defaults cwd to your current directory, so the wrapped CLI operates on whatever project you're in. The `{{PROFILE_DIR}}` placeholder decouples bridge-script location from agent cwd.
- **New troubleshooting page** at `/guide/troubleshooting` (EN + ZH) — decision tree for the most common errors.

### CLI fixes (Node + Python parity)

- `agentproc hub run echo-agent -p "hello"` works (was: "requires --prompt").
- `agentproc hub run echo-agent --refresh` works (was: `unknown option: --refresh`).
- `agentproc hub run --help` shows the hub help (was: "requires a profile name").
- `agentproc hub runn ...` exits 2 (was: exit 0 — shell scripts couldn't detect the failure).
- `agentproc hub install <name>` prints a "Next:" hint with the exact command to run.
- Top-level `agentproc --help` now leads with the hub three-liner; `--profile` mode moved under "Advanced".

### Runner resilience (Node + Python parity)

- New `{{PROFILE_DIR}}` placeholder in `command`, `args`, `cwd`, and `env` values. Resolves to the profile's own directory.
- Relative `cwd` now resolves against `{{PROFILE_DIR}}`, not the bridge process cwd.
- `spawn ENOENT` errors get a tailored hint: "cwd does not exist" / "command not on PATH" / "argument file not found" / "permission denied".
- When the wrapped interpreter (python/node/bash) writes "No such file" to its own stderr before exiting non-zero, the runner surfaces it as `AGENT_ERROR:"agent script not found: ..."` instead of letting raw text leak through.
- stderr accumulated as a sliding 8KB window (last 8KB wins, not first) for post-mortem diagnosis.

### Hub fetch resilience (Node + Python parity)

- `HubError` class with `.hint` and `.status` fields.
- `GITHUB_TOKEN` / `GH_TOKEN` env vars add `Authorization: Bearer <token>`, raising the rate limit from ~60/hr to ~5,000/hr.
- 403/429 hint lists the rate-limit fix; 404 hint suggests `agentproc hub list`.
- Typo'd profile names get a "Did you mean X?" suggestion via prefix match + edit distance (length-scaled threshold).

### Hub profile schema

The five hub profiles (`claude-code`, `codex`, `codebuddy`, `agy`, `echo-agent`) changed shape:

```yaml
# Before (0.3.0)
agentproc:
  command: python3 ./bridge.py
  cwd: ~/your-project

# After (0.4.0)
agentproc:
  command: python3 {{PROFILE_DIR}}/bridge.py
  # cwd intentionally omitted: `hub run` defaults it to your current directory.
```

**Compatibility**: this is technically a breaking change for users who hand-edited hub profile templates. The hub design has always been "don't edit, use `hub run`," so impact is limited. Custom profiles (outside the hub) are unaffected — relative `./xxx` paths still work, they just resolve against `{{PROFILE_DIR}}` instead of process cwd.

### Docs

- New `/guide/troubleshooting` (EN + ZH): rate limit, wrong name, spawn ENOENT, agent AGENT_ERROR, timeout, network down.
- Homepage: `hub run` smoke test leads, GITHUB_TOKEN tip, "short replies may not show AGENT_PARTIAL" tip, macOS `pip`-via-`ensurepip` tip.
- `cli/`, `hub/`, `guide/`, `spec/` rewritten to lead with `hub run` and document `{{PROFILE_DIR}}`.
- SDK pages: "Local testing" now uses `agentproc --profile` first; raw env-var forms moved under `<details>`.
- All 5 hub profile READMEs reorganized: "Quick test" leads, raw-env-var tests under `<details>`.
- `CLAUDE_MODEL` / `CODEX_MODEL` env-var comments added to profile.yaml templates.

### Protocol

No changes. Still protocol `0.1`. The `{{PROFILE_DIR}}` placeholder is a bridge-level convention; agents don't see it (the bridge resolves it before spawning).

## 0.3.0 — 2026-06-25

### `agentproc hub` subcommands

Hub profiles can now be fetched and run directly from GitHub — no clone, no copy, no YAML editing.

```bash
agentproc hub list                          # list available profiles
agentproc hub show <name>                   # print a profile's README
agentproc hub run <name> -p "hello"         # fetch (if needed) and run
agentproc hub install <name>                # copy to current dir for editing
```

Profiles are cached at `~/.agentproc/cache/hub/<name>/` with a 24-hour TTL. Pass `--refresh` to force re-fetch.

Implementation uses the GitHub git-tree API (1 call for the entire file listing) plus raw.githubusercontent.com for downloads, so unauthenticated users stay below rate limits.

### New `agentproc.hub` module

Both SDKs now expose a `hub` module:

```js
// Node
const { fetchProfile, listProfiles, showReadme, installProfile } = require('agentproc/src/hub');
```

```python
# Python
from agentproc import hub
hub.fetch_profile("claude-code")
```

Zero new dependencies. Python uses `urllib.request` (stdlib), Node uses global `fetch` (Node 18+).

### Python CLI version display fix

`agentproc --version` now correctly shows the installed package version (was showing `0.0.0+unknown` after pip install due to pyproject.toml not being shipped in the wheel). Uses `importlib.metadata.version("agentproc")` with a pyproject.toml fallback for source checkouts.

Also exposes `agentproc.__version__` for SDK users who need it.

### Homepage onboarding flow updated

Step ③ of the "Get started in 5 minutes" guide now uses `agentproc hub run` instead of `git clone + cp + edit YAML`, reducing the time-to-first-success from minutes to seconds.

### Tests

- Python: 17 new tests in `tests/test_hub.py` (mock-based, no real network)
- Node: 16 new tests in `src/hub.test.js` (mock-based, no real network)
- All existing tests still pass

### No protocol changes

0.3.0 adds tooling on the bridge runner side. The wire protocol is unchanged from 0.1. Existing agents and bridges remain compatible.

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
