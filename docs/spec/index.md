# P0 Protocol Specification

**Version:** 0.1.0 · **Status:** Draft

The full specification is maintained in the repository at [`spec/protocol.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.md).

---

## Profile YAML

```yaml
command: ./my_agent.py        # executable to run
args: []                      # supports {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}}
stdin: none                   # none | message

cwd: /path/to/workspace
env:
  MY_API_KEY: "${MY_API_KEY}"

timeout_secs: 600
max_reply_chars: 8000
truncation_suffix: "\n\n…(truncated)"
include_stderr_in_reply: false

streaming: true
cli_session_first_line_prefix: "AGENT_SESSION:"
```

---

## Input — Environment Variables

| Variable | Description |
|----------|-------------|
| `AGENT_MESSAGE` | User message text |
| `AGENT_SESSION_ID` | CLI session UUID from previous turn (empty = new session) |
| `AGENT_SESSION_NAME` | Human-readable session name |
| `AGENT_FROM_USER` | Sender identifier |
| `AGENT_STREAMING` | `"1"` = streaming, `"0"` = one-shot |
| `AGENT_IMAGE_URL` | Image attachment URL (when present) |
| `AGENT_FILE_URL` | File attachment URL (when present) |

---

## Output — stdout Protocol

```
AGENT_SESSION:<uuid>              ← optional, first line only
AGENT_PARTIAL:<json-string>       ← optional, any line, streaming chunk
<reply text>                      ← everything else = final reply body
```

### Session line

Declare the CLI session UUID on the first line. The bridge stores it and passes it back as `AGENT_SESSION_ID` on the next turn.

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
```

### Partial lines

Emit streaming chunks at any time. The value is a JSON-encoded string.

```
AGENT_PARTIAL:"Here is the first part. "
AGENT_PARTIAL:"And here is more."
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| non-zero | Error (`send_error_reply: true` sends error to user) |

---

## Design Principles

1. **Process boundary is the only contract.** Any process that reads env vars and writes stdout is a valid agent.
2. **Agent doesn't know about the platform.** Platform concerns (delivery, rate limiting) are the bridge's job.
3. **Session IDs are opaque.** The bridge stores and forwards them; the agent owns their meaning.
4. **`type:` is not part of this spec.** Built-in shortcuts are platform extensions, not P0.
