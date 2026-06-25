# Changelog

All notable changes to AgentProc are documented here. The protocol version and the SDK package versions are kept in lockstep.

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

Python (`agentproc`) and Node.js (`@agentproc/sdk`) SDKs released at parity.

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
