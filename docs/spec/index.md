# Protocol Specification

**Wire protocol:** `0.1` · **Document revision:** `0.5` · **Status:** Draft

The full specification is maintained in the repository at [`spec/protocol.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.md).

---

## Profile YAML

```yaml
command: python3 {{PROFILE_DIR}}/my_agent.py   # executable; args absent → split on whitespace (no shell)
                                              # {{PROFILE_DIR}} = profile's own directory
# args: ["--flag", "{{MESSAGE}}"]             # optional; present (even []) → command is one token, never split
stdin: none                   # none | message (message = write + EOF)

# cwd is optional. If omitted, defaults to the bridge's process cwd.
# If relative, resolves against {{PROFILE_DIR}} (the profile's directory).
cwd: /path/to/workspace
env:
  MY_API_KEY: "${MY_API_KEY}"

timeout_secs: 600             # stdout read timeout, default 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL grace period, default 5
max_reply_chars: 8000
truncation_suffix: "\n\n…(truncated)"
include_stderr_in_reply: false
send_error_reply: true        # tell the user when the agent errors

streaming: true               # forward AGENT_PARTIAL: lines in real time
```

Placeholders are substituted **without** invoking a shell. The argv is built from two fields:

- **`command`** — argv[0].
- **`args`** — an optional YAML list of argv[1..] tokens.

The **presence** of `args` is the signal for whether `command` is split:

- `args` **absent** + `command` contains whitespace → split `command` on whitespace into argv (the legacy shorthand: `command: python3 ./bridge.py`).
- `args` **present** (even an empty array `[]`) → `command` is a single argv token, **never split**. This is the escape hatch for a path that contains spaces:

```yaml
command: "/path with spaces/my agent"
args: []
```

The resulting argv is passed to `execve` directly, which prevents shell-injection via `{{MESSAGE}}`.

---

## Input — Environment Variables

### Core

| Variable | Description |
|----------|-------------|
| `AGENT_MESSAGE` | User message text |
| `AGENT_SESSION_ID` | Session ID from previous turn (empty = new session) |
| `AGENT_SESSION_NAME` | Human-readable session name (default `"default"`) |
| `AGENT_FROM_USER` | Sender identifier |
| `AGENT_STREAMING` | `"1"` = streaming, `"0"` = one-shot |
| `AGENT_PROTOCOL_VERSION` | Protocol version string, e.g. `"0.1"`. **Opaque and non-comparable** — see the spec's Versioning section. Agents MUST NOT order or range-check it. |

### Attachments — P0

Two layers. Bridges set the layer matching the message; agents prefer the richer layer when present and fall back otherwise.

| Variable | Description |
|----------|-------------|
| `AGENT_IMAGE_URL` | Image URL (single-attachment convenience var) |
| `AGENT_FILE_URL` | File URL (single-attachment convenience var) |
| `AGENT_ATTACHMENTS` | JSON array of `{type, url, name}` (`type` = `image\|file\|audio\|video`). Set for zero or more attachments. Empty array = no attachments. |

When a bridge sets `AGENT_ATTACHMENTS` **and** a single-attachment var, the two MUST agree (same URL). Agents consume `AGENT_ATTACHMENTS` when non-empty, else fall back to the single-attachment vars.

### stdin

| `stdin` value | Behavior |
|---------------|----------|
| `none` (default) | Message only available via `AGENT_MESSAGE` |
| `message` | Message is also written to stdin, **then stdin is closed (EOF)** |

The agent MUST NOT block on stdin when `stdin: none` is in effect.

---

## Output — stdout Protocol

A line is a **protocol line** if it starts with one of these prefixes (evaluated in this order):

```
AGENT_SESSION:<opaque-id>        ← session id; can appear anywhere; LAST WINS
AGENT_PARTIAL:<json-string>      ← streaming chunk, any line
AGENT_ERROR:<json-string>        ← user-readable error, any line, any mode
<everything else>                ← reply body, forwarded verbatim
```

Reply-body lines MUST NOT start with `AGENT_SESSION:`, `AGENT_PARTIAL:`, or `AGENT_ERROR:`. Prefix with a single space if you need to output such text literally.

### Session line — last wins

The session line MAY appear at **any position** in stdout — first line, interleaved with partials, or last line. If multiple are emitted, the **last one wins**. This accommodates CLIs that only learn their session id at exit time (e.g. `claude --output-format stream-json`).

If an `AGENT_SESSION:` line and an `AGENT_ERROR:` line appear in the same turn, the bridge **MUST** still persist the session id for the next turn even though the current turn is reported as failed. The error terminates the turn; it does not invalidate the session.

```
AGENT_PARTIAL:"thinking..."
AGENT_PARTIAL:"answer"
AGENT_SESSION:cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c
```

### Partial lines

JSON-encoded string. Forwarded immediately when `streaming: true`; ignored otherwise.

```
AGENT_PARTIAL:"Here is the first part. "
AGENT_PARTIAL:"And here is more."
```

### Error lines

JSON-encoded user-readable error. Honored **regardless** of `streaming` mode. The bridge forwards it to the user, terminates any in-progress partial stream, and **MUST** treat the turn as failed even if the exit code is 0. Any reply body produced alongside is discarded.

```
AGENT_ERROR:"Upstream API rate limited. Try again in 60s."
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Generic agent error |
| `124` | Timeout (bridge-imposed; matches GNU `timeout`) |
| `130` | Interrupted by SIGINT |
| `143` | Terminated by SIGTERM |

When `send_error_reply: true` is set and the process exits non-zero without emitting `AGENT_ERROR:`, the bridge sends a generic error message to the user.

---

## Timeout Handling

When `timeout_secs` is reached:

1. Bridge sends `SIGTERM`.
2. Bridge waits `kill_grace_secs` (default 5) for the process to exit.
3. If still running, bridge sends `SIGKILL`.

Already-received `AGENT_PARTIAL:` lines are forwarded. The agent SHOULD handle `SIGTERM` by flushing buffered partials and exiting promptly.

---

## Design Principles

1. **Process boundary is the only contract.** Any process that reads env vars and writes stdout is a valid agent.
2. **Agent doesn't know about the platform.** Platform concerns (delivery, rate limiting) are the bridge's job.
3. **Session IDs are opaque.** The bridge stores and forwards them; the agent owns their meaning.
4. **`type:` is not part of this spec.** Built-in shortcuts are platform extensions, not P0.

See the [full spec on GitHub](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.md) for design rationale and a comparison with MCP, ACP, NDJSON, SSE, LSP, and the Unix filter convention.
