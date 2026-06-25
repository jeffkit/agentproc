# AgentProc Protocol Specification

**Version:** 0.1.0  
**Status:** Draft

---

## Overview

AgentProc is a minimal protocol for connecting any Agent CLI to a messaging platform via a process-based interface. It defines how a **bridge** (the platform adapter) communicates with an **agent process** (a script or executable that wraps an AI agent).

```
Messaging Platform
      │
      ▼
   Bridge                ← reads profile YAML, manages process lifecycle
      │   stdin / env
      ▼
 Agent Process           ← your script or binary (the P0 contract)
      │   stdout
      ▼
   Bridge                ← forwards reply to the platform
```

The protocol has exactly two sides:

- **Input** — environment variables injected by the bridge before the process starts
- **Output** — stdout lines written by the agent process

No HTTP, no sockets, no shared memory. Just a process.

---

## Profile YAML

A profile is a YAML file that tells the bridge how to launch an agent process.

```yaml
# Required: the executable to run
command: ./my_agent.py        # path to script or binary
args: []                      # optional arguments (support placeholders)
stdin: none                   # none | message

# Execution environment
cwd: /path/to/workspace       # working directory (supports ~ and placeholders)
env:                          # extra environment variables
  MY_API_KEY: "${MY_API_KEY}" # reference existing env vars with ${VAR}

# Output control
timeout_secs: 600             # stdout read timeout, default 1800
max_reply_chars: 8000         # truncate reply at this length, default 8000
truncation_suffix: "\n\n…(truncated)"
include_stderr_in_reply: false

# Streaming
streaming: true               # forward AGENT_PARTIAL: lines in real time

# Session continuity
cli_session_first_line_prefix: "AGENT_SESSION:"
```

### Placeholders

Placeholders in `args`, `cwd`, and `env` values are replaced before the process starts. They are **not** passed through a shell.

| Placeholder | Value |
|-------------|-------|
| `{{MESSAGE}}` | User message text |
| `{{SESSION_ID}}` | Session UUID from the previous turn (empty = new session) |
| `{{SESSION_NAME}}` | Human-readable session name |

### `stdin` field

| Value | Behavior |
|-------|----------|
| `none` (default) | Message is only available via `AGENT_MESSAGE` env var |
| `message` | Message text is also written to stdin (useful for long messages or multiline input) |

---

## Input — Environment Variables

The bridge injects the following variables before spawning the process. The agent process reads them directly.

| Variable | Description |
|----------|-------------|
| `AGENT_MESSAGE` | User message text |
| `AGENT_SESSION_ID` | CLI session UUID from the previous turn (empty string = new session) |
| `AGENT_SESSION_NAME` | Human-readable session name (default: `"default"`) |
| `AGENT_FROM_USER` | Sender identifier |
| `AGENT_STREAMING` | `"1"` = streaming mode, `"0"` = one-shot mode |
| `AGENT_IMAGE_URL` | Image attachment URL (only set when message contains an image) |
| `AGENT_FILE_URL` | File attachment URL (only set when message contains a file) |

Custom variables declared in the profile's `env` block are also injected.

---

## Output — stdout Protocol

The agent process writes to stdout. The bridge reads it line by line in real time.

### Session line (optional)

If the agent process maintains its own session state (e.g. an AI CLI with `--resume`), it can declare the session ID on the **first line** of stdout:

```
AGENT_SESSION:<uuid>
```

The bridge stores this UUID and passes it back as `AGENT_SESSION_ID` on the next turn. This enables multi-turn continuity without the bridge needing to understand the underlying AI system.

This line is consumed by the bridge and does **not** appear in the reply sent to the user.

### Partial lines (optional, streaming)

For streaming output, the agent process can emit partial chunks at any time:

```
AGENT_PARTIAL:<json-encoded-string>
```

The value must be a JSON-encoded string (e.g. `"hello"`, `"line one\nline two"`). The bridge forwards each partial to the user immediately without waiting for the process to exit.

When `streaming: false` is set in the profile, the bridge ignores all `AGENT_PARTIAL:` lines.

### Reply body

All stdout lines that are **not** a session line or partial line form the final reply body, sent to the user after the process exits.

If all content was already delivered via `AGENT_PARTIAL:` lines, the reply body may be empty — the bridge will skip the final send in that case.

### Complete example

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
AGENT_PARTIAL:"Here is the first part of the answer. "
AGENT_PARTIAL:"And here is the second part."
```

Or without streaming:

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
Here is the complete answer.
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — stdout content is sent as the reply |
| non-zero | Error — bridge sends an error message if `send_error_reply: true` |

stderr is captured as a debug log and not shown to the user, unless `include_stderr_in_reply: true`.

---

## Design Principles

**1. Process boundary is the only contract.**  
The bridge does not care what language the agent is written in, what AI model it calls, or how it manages state. Any process that reads env vars and writes to stdout is a valid agent.

**2. No bridge logic in the agent.**  
The agent process should not need to know anything about the messaging platform. It reads a message, does something, writes a reply. Platform-specific concerns (delivery, rate limiting, session storage) are the bridge's responsibility.

**3. Session IDs are opaque.**  
The bridge stores and forwards session IDs but never interprets them. The agent process owns the meaning of its session IDs.

**4. `type:` is not part of this protocol.**  
Built-in shortcuts (e.g. `type: claude-code`) are platform extensions, not P0. Implementations may offer them, but they are out of scope for this specification.
