# Protocol Specification

**Version:** 0.1.0 · **Status:** Draft

The full specification is maintained in the repository at [`spec/protocol.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.md).

---

## Profile YAML

```yaml
command: ./my_agent.py        # executable to run, split on whitespace (no shell)
args: []                      # supports {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}}
stdin: none                   # none | message (message = write + EOF)

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
session_line_prefix: "AGENT_SESSION:"
```

Placeholders are substituted **without** invoking a shell. The `command` string is split into an argv array on whitespace and passed to `execve` directly — this prevents shell-injection via `{{MESSAGE}}`.

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
| `AGENT_PROTOCOL_VERSION` | Protocol version string, e.g. `"0.1"` |

### Attachments — P0 (single)

| Variable | Description |
|----------|-------------|
| `AGENT_IMAGE_URL` | Image URL (set when message has exactly one image) |
| `AGENT_FILE_URL` | File URL (set when message has exactly one file) |

### Attachments — draft (multi)

| Variable | Description |
|----------|-------------|
| `AGENT_ATTACHMENTS` | JSON array of `{type, url, name}` (`type` = `image\|file\|audio\|video`). Draft — prefer this when present, fall back to the single-attachment vars when not. |

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

JSON-encoded user-readable error. Honored **regardless** of `streaming` mode. The bridge forwards it to the user, terminates any in-progress partial stream, and treats the process as failed even if the exit code is 0. Any reply body produced alongside is discarded.

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
