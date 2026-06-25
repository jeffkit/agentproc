# AgentProc Protocol Specification

**Version:** 0.2.0
**Status:** Draft

---

## Overview

AgentProc is a minimal protocol for connecting any agent CLI to a messaging platform via a process-based interface. It defines how a **bridge** (the platform adapter) communicates with an **agent process** (a script or executable that wraps an AI agent).

```
Messaging Platform
      │
      ▼
   Bridge                ← reads profile YAML, manages process lifecycle
      │   env vars (and optional stdin)
      ▼
 Agent Process           ← your script or binary (implements the contract below)
      │   stdout
      ▼
   Bridge                ← forwards reply to the platform
```

The protocol has exactly two sides:

- **Input** — environment variables injected by the bridge before the process starts (optionally accompanied by a single stdin write)
- **Output** — stdout lines written by the agent process, distinguished by line prefix

No HTTP, no sockets, no shared memory. Just a process.

---

## Profile YAML

A profile is a YAML file that tells the bridge how to launch an agent process.

```yaml
# Required: the executable to run
command: ./my_agent.py        # path to script or binary
args: []                      # optional arguments (placeholders supported)
stdin: none                   # none | message

# Execution environment
cwd: /path/to/workspace       # working directory (~ and placeholders supported)
env:                          # extra environment variables
  MY_API_KEY: "${MY_API_KEY}" # reference existing env vars with ${VAR}

# Output control
timeout_secs: 600             # stdout read timeout, default 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL grace period, default 5
max_reply_chars: 8000         # truncate reply at this length, default 8000
truncation_suffix: "\n\n…(truncated)"
include_stderr_in_reply: false
send_error_reply: true        # tell the user when the agent errors

# Streaming
streaming: true               # forward AGENT_PARTIAL: lines in real time

# Session continuity
session_line_prefix: "AGENT_SESSION:"  # prefix that marks a session line
```

### Placeholders

Placeholders in `args`, `cwd`, and `env` values are replaced before the process starts. They are **not** passed through a shell.

| Placeholder | Value |
|-------------|-------|
| `{{MESSAGE}}` | User message text |
| `{{SESSION_ID}}` | Session ID from the previous turn (empty = new session) |
| `{{SESSION_NAME}}` | Human-readable session name |

### Command execution model

The `command` field MUST be split into an argv array on whitespace and passed to the platform's `execve` (or equivalent) **without** invoking a shell. This avoids shell-injection attacks via the `{{MESSAGE}}` placeholder.

If a bridge implementation chooses to use a shell (e.g., for environment-variable expansion), it MUST apply POSIX shell quoting to every placeholder substitution. Bridges SHOULD prefer the no-shell form.

### `stdin` field

| Value | Behavior |
|-------|----------|
| `none` (default) | Message is only available via the `AGENT_MESSAGE` env var |
| `message` | Message text is also written to stdin, **then stdin is closed (EOF)** |

When `stdin: message` is set, the bridge writes the message followed by EOF. The agent can read it with any line-oriented or stream-oriented API (`input()`, `readline`, `fs.readFileSync(0)`, etc.) and trust that it will terminate.

---

## Input — Environment Variables

The bridge injects the following variables before spawning the process. The agent process reads them directly.

### Core variables

| Variable | Description |
|----------|-------------|
| `AGENT_MESSAGE` | User message text |
| `AGENT_SESSION_ID` | Session ID from the previous turn (empty string = new session) |
| `AGENT_SESSION_NAME` | Human-readable session name (default: `"default"`) |
| `AGENT_FROM_USER` | Sender identifier (platform-specific: user ID, handle, etc.) |
| `AGENT_STREAMING` | `"1"` = streaming mode, `"0"` = one-shot mode |
| `AGENT_PROTOCOL_VERSION` | Protocol version string, e.g. `"0.1"`. Agents MAY inspect this to gate behavior. |

### Attachment variables (P0 — single attachment)

| Variable | Description |
|----------|-------------|
| `AGENT_IMAGE_URL` | Image attachment URL (only set when the message contains exactly one image) |
| `AGENT_FILE_URL` | File attachment URL (only set when the message contains exactly one file) |

### Attachment variables (draft — multi-attachment)

| Variable | Description |
|------|-------------|
| `AGENT_ATTACHMENTS` | JSON array of `{"type":"image\|file\|audio\|video", "url":"...", "name":"..."}`. **Draft**: bridges MAY set this in addition to the single-attachment vars; agents SHOULD prefer `AGENT_ATTACHMENTS` when present and fall back to the single-attachment vars when not. |

Custom variables declared in the profile's `env` block are also injected.

---

## Output — stdout Protocol

The agent process writes to stdout. The bridge reads it line by line in real time.

### Protocol line recognition

A line is treated as a **protocol line** if and only if it matches one of the prefixes below, evaluated in this order:

1. `AGENT_SESSION:` — declares or updates the session ID
2. `AGENT_PARTIAL:` — emits a streaming chunk
3. `AGENT_ERROR:` — emits an error message to forward to the user

All other lines are **reply body** and forwarded verbatim.

This means an agent's reply body MUST NOT contain lines that start with `AGENT_SESSION:`, `AGENT_PARTIAL:`, or `AGENT_ERROR:`. If an agent needs to output such text (e.g., when the user is discussing the protocol itself), it MUST prefix the line with a single space or otherwise ensure it does not match.

> **Implementation note for bridges:** match prefixes against the *stripped* line if you want to be tolerant of leading whitespace from heredocs; otherwise match against the raw line. Bridges SHOULD be consistent.

### `AGENT_SESSION:` — session line

If the agent process maintains its own session state (e.g., an AI CLI with `--resume`), it declares the session ID by emitting:

```
AGENT_SESSION:<opaque-string>
```

**Session line rule (resolves ordering ambiguity):**

- The session line MAY appear at **any position** in stdout — first line, interleaved with partials, or last line.
- If multiple `AGENT_SESSION:` lines are emitted, the **last one wins**. The bridge stores the final value and passes it back as `AGENT_SESSION_ID` on the next turn.
- This rule accommodates the common case where the session ID is only known after the underlying CLI exits (e.g., `claude --output-format stream-json` emits the session ID in its terminal `result` event).

The session-ID string is **opaque** — the bridge stores and forwards it verbatim and MUST NOT interpret its format. It MAY be a UUID, a CLI-internal handle, or any string without whitespace or colons.

This line is consumed by the bridge and does **not** appear in the reply sent to the user.

### `AGENT_PARTIAL:` — streaming chunk

For streaming output, the agent process emits partial chunks at any time:

```
AGENT_PARTIAL:<json-encoded-string>
```

The value MUST be a JSON-encoded string (e.g., `"hello"`, `"line one\nline two"`, `"emoji: 😀"`).

**JSON parsing policy (resolves ambiguity):**

- The bridge attempts to JSON-decode the text after the prefix.
- On success, the decoded string is forwarded to the user immediately.
- On failure, the bridge SHOULD treat the raw text after the prefix as the chunk (lenient mode) and emit a warning to stderr. Bridges MAY choose strict mode (discard the line and log), but the default SHOULD be lenient to accommodate hand-written agents.

When `streaming: false` is set in the profile, the bridge ignores all `AGENT_PARTIAL:` lines.

### `AGENT_ERROR:` — error message

When the agent encounters an error that should be communicated to the user, it emits:

```
AGENT_ERROR:<json-encoded-string>
```

This line is honored **regardless** of `streaming` mode. The bridge forwards the decoded string to the user as an error reply and SHOULD terminate any in-progress partial stream.

If an `AGENT_ERROR:` line is emitted, the bridge SHOULD treat the process as failed even if the exit code is 0. The agent SHOULD exit with a non-zero code after emitting `AGENT_ERROR:`.

Any reply body produced alongside an `AGENT_ERROR:` line is discarded.

### Reply body

All stdout lines that are **not** protocol lines form the final reply body, sent to the user after the process exits.

If all content was already delivered via `AGENT_PARTIAL:` lines, the reply body may be empty — the bridge will skip the final send in that case.

### Complete examples

**Streaming with session discovered at the end (the common CLI-wrapping case):**

```
AGENT_PARTIAL:"Here is the first part of the answer. "
AGENT_PARTIAL:"And here is the second part."
AGENT_SESSION:cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c
```

**One-shot with session declared up front:**

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
Here is the complete answer.
```

**Error mid-stream:**

```
AGENT_PARTIAL:"Let me look that up... "
AGENT_ERROR:"Upstream API rate limited. Try again in 60s."
```

---

## stdin / EOF Contract

- When `stdin: none` (default), the bridge does not write to stdin. The agent's stdin reads will return EOF immediately.
- When `stdin: message`, the bridge writes `AGENT_MESSAGE` to stdin followed by EOF. The agent can read it via `input()`, `readline()`, `fs.readFileSync(0, 'utf8')`, etc., and the read will terminate.

The agent MUST NOT block on stdin when `stdin: none` is in effect.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — stdout content (minus protocol lines) is sent as the reply |
| `1` | Generic agent error |
| `124` | Timeout (bridge-imposed; matches GNU `timeout` convention) |
| `130` | Interrupted by SIGINT (Ctrl-C) |
| `143` | Terminated by SIGTERM |

Other non-zero codes are treated as generic errors. When `send_error_reply: true` is set and the process exits non-zero (without having emitted `AGENT_ERROR:`), the bridge sends a generic error message to the user.

stderr is captured as a debug log and not shown to the user, unless `include_stderr_in_reply: true`.

---

## Timeout Handling

When `timeout_secs` is reached without the process exiting:

1. Bridge sends `SIGTERM` to the process.
2. Bridge waits `kill_grace_secs` (default 5) for the process to exit.
3. If still running, bridge sends `SIGKILL`.

Any `AGENT_PARTIAL:` lines already received are forwarded to the user. The bridge then sends a timeout error reply (subject to `send_error_reply`).

The agent SHOULD handle `SIGTERM` by flushing any buffered partial output and exiting promptly.

---

## Design Principles

**1. Process boundary is the only contract.**
The bridge does not care what language the agent is written in, what AI model it calls, or how it manages state. Any process that reads env vars and writes to stdout is a valid agent.

**2. No bridge logic in the agent.**
The agent process should not need to know anything about the messaging platform. It reads a message, does something, writes a reply. Platform-specific concerns (delivery, rate limiting, session storage) are the bridge's responsibility.

**3. Session IDs are opaque.**
The bridge stores and forwards session IDs but never interprets them. The agent process owns the meaning of its session IDs.

**4. The unit of work is one turn.**
Each user message spawns one process. The agent is not expected to be a long-running daemon. (Long-running daemons are out of scope; see "Comparison with related protocols" below.)

**5. `type:` shortcuts are not part of this protocol.**
Built-in shortcuts (e.g. `type: claude-code`) are platform extensions, not P0. Implementations may offer them, but they are out of scope for this specification.

---

## Design Rationale

**Why environment variables for input, rather than stdin or JSON args?**

Three reasons:

1. **Debuggability.** You can drive an agent by hand from a shell: `AGENT_MESSAGE="hello" ./agent.sh`. No fixtures, no test harness.
2. **Language neutrality.** Every programming language reads env vars identically. Argument parsing differs across languages and shells.
3. **No quoting ambiguity.** A long, multiline message in a CLI arg requires shell escaping. An env var carries its full value verbatim.

The cost is that env vars have platform-specific size limits (typically 128 KB – 8 MB). Messages larger than that should use `stdin: message`.

**Why sentinel-prefixed lines, rather than NDJSON?**

NDJSON (one JSON object per line) is the format used internally by Claude Code's `stream-json`, MCP, ACP, etc. It's a good format — but it forces every emitted line to be valid JSON. AgentProc wants this to be a valid agent:

```bash
#!/usr/bin/env bash
echo "You said: $AGENT_MESSAGE"
```

Sentinel-prefixed lines let the common case (final reply body) be plain text, while structured events (`AGENT_SESSION:`, `AGENT_PARTIAL:`, `AGENT_ERROR:`) opt in to a prefix. The cost is one rule: the reply body must not start with `AGENT_` followed by a known prefix.

**Why "last session line wins"?**

Because the underlying CLI often doesn't know its own session ID until it exits. `claude --output-format stream-json` emits the session ID in the terminal `result` event, which is the last event of the run. A "must be first line" rule would force bridge authors into awkward buffering. "Last wins" lets the agent emit the session line whenever it learns it.

**Why `AGENT_ERROR:` in addition to non-zero exit codes?**

Exit codes tell the bridge *that* something went wrong, but not *what* to tell the user. `AGENT_ERROR:` lets the agent forward a meaningful, user-readable error message (e.g., "API key expired", "rate limited; retry in 60s") instead of the bridge's generic template.

---

## Comparison with Related Protocols

AgentProc occupies a specific niche. The neighboring protocols are similar in *shape* (subprocess + stdio) but different in *purpose*.

### MCP — Model Context Protocol (Anthropic)

MCP connects an LLM application (the client) to **tools and data sources** (the server, a subprocess). Transport: JSON-RPC 2.0 over stdio or HTTP+SSE.

**Relationship to AgentProc:** **Reverse direction.** In MCP, the AI is the client and the tool provider is the subprocess. In AgentProc, the bridge is the client and the AI wrapper is the subprocess. They compose naturally: an AgentProc agent may internally use MCP tools.

- Spec: https://modelcontextprotocol.io/

### ACP — Agent Client Protocol (Zed Industries)

ACP connects a code editor to an AI coding agent. Transport: JSON-RPC 2.0 over stdio, bidirectional, long-lived.

**Relationship to AgentProc:** **Richer cousin.** ACP assumes an interactive IDE session with tool calls, file diffs, and mode switching. AgentProc assumes a single chat turn per process invocation. Use ACP if you're building an IDE; use AgentProc if you're bridging a chat bot to a CLI.

- Spec: https://agentclientprotocol.com/

### NDJSON / JSON Lines

NDJSON is one JSON object per line, newline-delimited. It's the wire format used internally by Claude Code, Codex, Gemini CLI streaming modes, and by MCP.

**Relationship to AgentProc:** **Alternative wire format.** NDJSON requires every emitted line to be valid JSON. AgentProc uses sentinel-prefixed plain text to keep hand-written agents (`echo "You said: $AGENT_MESSAGE"`) valid. The cost is one disambiguation rule (reply body must not start with `AGENT_*:`).

- Spec: https://jsonlines.org/

### SSE — Server-Sent Events (WHATWG)

SSE streams `event:` / `data:` lines over HTTP.

**Relationship to AgentProc:** **Semantic ancestor of `AGENT_PARTIAL:`.** The pattern of "newline-terminated events with a prefix" is borrowed from SSE, minus the HTTP transport and with a fixed field set.

- Spec: https://html.spec.whatwg.org/multipage/server-sent-events.html

### LSP / DAP — Language Server / Debug Adapter Protocols (Microsoft)

LSP and DAP connect an editor to a language server or debugger. Transport: JSON-RPC 2.0 over stdio with `Content-Length: N` framing.

**Relationship to AgentProc:** **Framing contrast.** LSP uses byte-length-prefixed framing (allows binary payloads, requires a parser). AgentProc uses newline-delimited framing (text only, trivial to parse by hand). The trade-off is deliberate.

- Specs: https://microsoft.github.io/language-server-protocol/ / https://microsoft.github.io/debug-adapter-protocol/

### Unix filter convention

The POSIX-derived convention of "read from stdin, write to stdout, exit 0 on success" — formalized in Eric Raymond's *The Art of Unix Programming*.

**Relationship to AgentProc:** **Philosophical foundation.** AgentProc extends the Unix filter convention with two things filters don't have: session-continuity handoff (`AGENT_SESSION:`) and streaming events (`AGENT_PARTIAL:`). Everything else is ordinary Unix.

- Reference: http://www.catb.org/~esr/writings/taoup/html/ch01s06.html

### What AgentProc is *not*

- **Not a bot framework.** Hubot, Errbot, BotKit, and Microsoft Bot Framework operate on the *consumer* side of the bridge (in-process adapters, HTTP connectors). AgentProc defines the contract *between* the bridge and the agent, and is orthogonal to those frameworks.
- **Not an agent-to-agent protocol.** A2A / AGNTCY solve a different problem (agents talking to each other).
- **Not an IDE protocol.** Use ACP for that.
- **Not a tool protocol.** Use MCP for that.

---

## Changelog

- **0.1.0** — Initial public draft. Defined env-var input, sentinel-prefixed stdout, `AGENT_SESSION:` / `AGENT_PARTIAL:` / `AGENT_ERROR:`, session-line "last wins" rule, `AGENT_PROTOCOL_VERSION`, `AGENT_ATTACHMENTS` (draft), timeout/SIGTERM contract, exit-code conventions, stdin EOF contract, command-execution no-shell rule.
