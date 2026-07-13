# Protocol Specification

**Wire protocol:** `0.3` · **Document revision:** `1.0` · **Status:** Draft

The full specification is maintained in the repository at [`spec/protocol.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.md).

---

## Profile YAML

```yaml
command: python3                      # argv[0] — always a single token, never split
args: ["{{PROFILE_DIR}}/my_agent.py"] # argv[1..]; defaults to [] when omitted
                                       # {{PROFILE_DIR}} = profile's own directory

# cwd is optional. If omitted, defaults to the bridge's process cwd.
# If relative, resolves against {{PROFILE_DIR}} (the profile's directory).
cwd: /path/to/workspace
env:
  MY_API_KEY: "${MY_API_KEY}"
env_allowlist: [MY_API_KEY]           # optional: restrict ${VAR} expansion

timeout_secs: 600             # stdout read timeout, default 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL grace period, default 5
max_reply_chars: 8000
truncation_suffix: "\n\n…(truncated)"
include_stderr_in_reply: false
send_error_reply: true        # tell the user when the agent errors

streaming: true               # forward {"type":"partial"} events in real time

permission: false             # optional tool authorization (keep stdin open; see full spec)
```

Placeholders are substituted **without** invoking a shell. The argv is built from two fields:

- **`command`** — argv[0]. A single token, **never split**, even if it contains whitespace.
- **`args`** — a YAML list of argv[1..] tokens. **Defaults to `[]` when omitted.**

The resulting argv (`[command, *args]`) is passed to `execve` directly, which prevents shell-injection via `{{MESSAGE}}` and lets `command` carry a path with spaces:

```yaml
command: "/path with spaces/my agent"
args: []
```

*(0.2 had a shorthand where `args` absent + `command` containing whitespace meant "split `command`". 0.3 removes it — `command` is always one token.)*

---

## Input — stdin turn object

Before the agent reads its first byte of stdin, the bridge writes **exactly one** NDJSON line: the turn object.

```json
{"type":"turn","message":"hello","session_id":"","session_name":"default",
 "from_user":"u1","attachments":[],"permission":false,"protocol_version":"0.3"}
```

### Required fields

| Field | Description |
|-------|-------------|
| `type` | Literal `"turn"`. |
| `message` | User message text. May be `""` (see "Empty turns" in the full spec). |
| `session_id` | Session ID from the previous turn (`""` = new session). |
| `from_user` | Sender identifier (platform-specific). |
| `protocol_version` | Protocol version string, e.g. `"0.3"`. **Opaque and non-comparable** — agents MUST NOT order or range-check it. |

### Optional fields (present = relevant)

| Field | Description |
|-------|-------------|
| `session_name` | Human-readable session name (default `"default"`). |
| `attachments` | Array of `{kind, url, ...}` (e.g. `{"kind":"image","url":"https://..."}`). The only attachment channel — there are no single-attachment convenience vars in 0.3. Absent/`[]` = none. |
| `permission` | `true` when the profile has `permission: true`; absent/`false` otherwise. |

Secrets and per-CLI config travel in **environment variables** (the profile `env` block), not the turn object. The per-turn request does **not** travel in env vars in 0.3.

### stdin / EOF

- When `permission` is absent/false, the bridge writes the turn line and then closes stdin (EOF). The agent MUST NOT block on stdin after reading the turn.
- When `permission: true`, the bridge keeps stdin open for mid-turn `{"type":"permission_response"}` lines (see full spec).

---

## Output — stdout NDJSON events

Every stdout line is a JSON object terminated by `\n`, carrying a `type` field. The vocabulary is **closed**: `partial`, `text`, `session`, `error`, and (when permission is on) `permission_request`.

```
{"type":"partial","text":"..."}            ← streaming chunk; forwarded when streaming: true
{"type":"text","text":"..."}               ← final reply body; multiple events concatenate
{"type":"session","id":"<opaque-id>"}      ← session id; can appear anywhere; LAST WINS
{"type":"error","message":"..."}           ← user-readable error; any mode; fails the turn
{"type":"permission_request",...}          ← optional tool authorization (permission: true)
```

A line that is not valid JSON, is not an object, or lacks a recognised `type` is **ignored** (logged to stderr) — it is not treated as reply body. Reply body is carried only by `text` events.

### Session event — last wins

The session event MAY appear at **any position** in stdout. If multiple are emitted, the **last one wins**. This accommodates CLIs that only learn their session id at exit time (e.g. `claude --output-format stream-json`).

If a `session` event and an `error` event appear in the same turn, the bridge **MUST** still persist the session id for the next turn even though the current turn is reported as failed.

```
{"type":"partial","text":"thinking..."}
{"type":"partial","text":"answer"}
{"type":"session","id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

### Partial events

Streaming chunks. Forwarded immediately when `streaming: true`; ignored by the runner otherwise. An optional `role` (`"output"` | `"thinking"`) distinguishes assistant output from reasoning.

```
{"type":"partial","text":"Here is the first part. "}
{"type":"partial","role":"thinking","text":"Let me consider..."}
```

### Text events — reply body

Final reply body. Multiple `text` events concatenate in order. If all content was delivered via `partial` and no `text` event is emitted, the final reply is empty and the bridge skips the final send.

```
{"type":"text","text":"Here is the complete answer."}
```

### Error events

User-readable error. Honored **regardless** of `streaming` mode. The bridge forwards it to the user, suppresses further partials, and **MUST** treat the turn as failed even if the exit code is 0. Any `text` produced alongside is discarded.

```
{"type":"error","message":"Upstream API rate limited. Try again in 60s."}
```

### Optional tool permission

Opt-in via profile `permission: true`. Not general HIL — tool authorization only. CLIs without a mid-turn approval channel keep using `--dangerously-skip-permissions` / `--yolo`.

```
{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo ok > f.txt"}}
```

Bridge writes on stdin after the user approves:

```
{"type":"permission_response","request_id":"1","behavior":"allow"}
```

See the full spec for stdin keep-open rules, timeouts, and field definitions.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Generic agent error |
| `124` | Timeout (bridge-imposed; matches GNU `timeout`) |
| `130` | Interrupted by SIGINT |
| `143` | Terminated by SIGTERM |

Precedence when multiple failure signals arrive: **timeout (124) > `error` event (1) > process exit code**.

When `send_error_reply: true` is set and the process exits non-zero without emitting an `error` event, the bridge sends a generic error message to the user.

---

## Timeout Handling

When `timeout_secs` is reached:

1. Bridge sends `SIGTERM`.
2. Bridge waits `kill_grace_secs` (default 5) for the process to exit.
3. If still running, bridge sends `SIGKILL`.

Already-received `partial` events are forwarded. The agent SHOULD handle `SIGTERM` by flushing buffered partials and exiting promptly.

---

## Design Principles

1. **Process boundary is the only contract.** Any process that reads a turn from stdin and writes NDJSON events to stdout is a valid agent.
2. **Agent doesn't know about the platform.** Platform concerns (delivery, rate limiting) are the bridge's job.
3. **Session IDs are opaque.** The bridge stores and forwards them; the agent owns their meaning.
4. **One turn per process.** Long-lived sessions and mid-turn cancellation are out of scope by design.
5. **`type:` is not part of this spec.** Built-in shortcuts are platform extensions, not P0.

See the [full spec on GitHub](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.md) for design rationale and a comparison with MCP, ACP, NDJSON, SSE, LSP, and the Unix filter convention.
