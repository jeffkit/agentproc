# AgentProc Protocol Specification

**Wire protocol:** `0.1` (the string injected as `AGENT_PROTOCOL_VERSION`)
**Document revision:** `0.5`
**Status:** Draft

The wire protocol and this document are versioned **independently**. The wire version only changes when the bytes on stdin/stdout change; the document revision tracks editorial updates, clarifications, and new guidance that does not alter what a conformant agent or bridge must send or accept. See [Versioning](#versioning) below for the rule an implementer should apply when reading `AGENT_PROTOCOL_VERSION`.

---

## Versioning

`AGENT_PROTOCOL_VERSION` is an **opaque string**, not a comparable number. Agents and bridges MUST NOT attempt to order, compare, or range-check it. Two strings are either equal or not equal.

- If a bridge injects a version string the agent does not recognise, the agent SHOULD behave as if the variable were unset (best-effort, fail-soft).
- If an agent expects a version string the bridge does not inject, the agent MUST fall back to its built-in default.
- The string is not a feature-detection mechanism: there is no negotiation, no capability advertisement, and no ordering. Agents that need to know whether a specific feature (e.g. multi-attachment) is present MUST inspect the relevant env var directly (e.g. `AGENT_ATTACHMENTS` non-empty), not the version string.

The rationale is that any comparable version invites implementers to gate behaviour on `>= 0.2`, which breaks the moment a bridge ships without bumping the number. Treating the string as opaque keeps the contract honest: presence of a feature is signalled by presence of the env var that carries it.

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
env_allowlist: [MY_API_KEY]   # optional: restrict which ${VAR} the env block may read

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

Placeholders in `command`, `args`, `cwd`, and `env` values are replaced before the process starts. They are **not** passed through a shell.

| Placeholder | Value |
|-------------|-------|
| `{{MESSAGE}}` | User message text |
| `{{SESSION_ID}}` | Session ID from the previous turn (empty = new session) |
| `{{SESSION_NAME}}` | Human-readable session name |
| `{{PROFILE_DIR}}` | Absolute path to the directory containing the profile YAML. Lets a profile reference a bundled script (e.g. `command: python3 {{PROFILE_DIR}}/bridge.py`) independently of the agent's `cwd`. Bridges set this when invoking a profile by path; if unset (e.g. programmatic use without a file), it expands to empty. |

### `${VAR}` expansion in `env` values

Values in the profile `env` block may reference the bridge's own environment variables with `${VAR}` syntax (e.g. `MY_API_KEY: "${MY_API_KEY}"`). This is **substitution against the bridge process's full environment**, not against the profile or the agent.

**Security implication.** A profile is **trusted input** — anyone who can write the profile can read every environment variable the bridge has access to (cloud credentials, tokens, secrets). This is by design (profiles are configuration, not user input), but it has one practical consequence worth calling out:

> **Do not run profiles from untrusted sources.** `agentproc hub run <name>` fetches a profile from a GitHub repo and runs it. If you would not trust that repo's maintainer with the entire contents of your shell environment, do not run their profile. A profile that sets `env_allowlist` (see below) shrinks this boundary to the variables it declares — but the default, with no allowlist, is still full-environment access, so the trust decision still rests with the user running the profile.

Bridges expand `${VAR}` using POSIX-shell semantics: unknown variables expand to the empty string, not to the literal `${VAR}`.

### `env_allowlist` — shrinking the trust boundary

By default, every `${VAR}` in the `env` block expands against the bridge's full environment. `env_allowlist` lets a profile shrink that boundary to exactly the variables it needs:

```yaml
env:
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
env_allowlist: [ANTHROPIC_API_KEY]
```

- **Optional and opt-in.** When `env_allowlist` is absent (the default), all `${VAR}` references expand normally — the existing trust-the-profile behaviour. Existing profiles keep working unchanged.
- **When present**, a `${VAR}` whose name is **not** in the list expands to the empty string, and the bridge logs a warning to stderr (e.g. `env_allowlist blocked ${AWS_SECRET_ACCESS_KEY}; expanded to empty`). The process still starts — a typo in either the value or the list surfaces as an empty variable and a warning, not a hard failure.
- **Scope.** `env_allowlist` governs only `${VAR}` expansion inside the profile `env` block. It does not affect `{{MESSAGE}}` / `{{SESSION_ID}}` / `{{PROFILE_DIR}}` placeholders, the bridge-injected `AGENT_*` vars, or `env` values that contain no `${VAR}` at all.
- **No globbing.** Names must match exactly. `["ANTHROPIC_*"]` does not match `ANTHROPIC_API_KEY` — list each name in full. This keeps the allowlist an explicit declaration rather than a pattern that can quietly broaden.
- **Recommendation.** Hub profiles SHOULD set `env_allowlist` so that `agentproc hub run <name>` exposes only the credentials the profile actually needs, not the user's entire shell environment. A profile fetched from a third-party repo that omits `env_allowlist` is not malicious by that fact alone, but the user has no way to tell what it reads — setting the list is how a profile author proves they only touch what they declare.

### `cwd` semantics

| Source | What happens |
|--------|--------------|
| `cwd` in profile (absolute path) | Used as-is |
| `cwd` in profile (relative path) | Resolved against `{{PROFILE_DIR}}` (the profile's own directory), not the bridge's process cwd. This makes `cwd: .` mean "the profile's directory" |
| `cwd` omitted | Defaults to the bridge's process cwd (typically the user's current directory) |
| `--cwd` flag (if the bridge's CLI exposes one) | Overrides all of the above |

The split between `{{PROFILE_DIR}}` (locates bundled scripts) and `cwd` (where the agent actually runs) is intentional: a hub profile can bundle a bridge script and still let `claude`/`codex`/etc. operate on whatever project the user is in.

### Command execution model

The bridge assembles the agent's argv from two fields:

- **`command`** — the executable (argv[0]). Treat it as a single token. If it contains whitespace, the bridge MUST treat the entire string as argv[0] verbatim and MUST NOT split it.
- **`args`** — a YAML list of additional argv tokens (argv[1..]). Each list element is one argv token, verbatim.

The resulting argv (`[command, *args]`) is passed to the platform's `execve` (or equivalent) **without invoking a shell**. This avoids shell-injection attacks via the `{{MESSAGE}}` placeholder and lets `command` carry a path that contains spaces.

**Legacy shorthand.** Many existing profiles write a multi-token command string (e.g. `command: python3 {{PROFILE_DIR}}/bridge.py`) and leave `args` unset. Bridges MUST support this shorthand: when `args` is **absent** and `command` contains whitespace, the bridge splits `command` on whitespace into argv.

**The `args` field is the signal.** The bridge decides whether to split by the **presence** of the `args` field, not by its contents:

- `args` **absent** (key not in the profile) → split `command` on whitespace (the shorthand).
- `args` **present** (even an empty array `[]`) → `command` is a single argv token, never split.

This means `args: []` is **meaningful**: it tells the bridge "do not split my command". This is the escape hatch for a `command` that contains whitespace but should be treated as one token.

**Quoting / paths with whitespace.** A profile that needs to invoke an executable whose path contains spaces MUST use the explicit form:

```yaml
command: "/path with spaces/my agent"
args: []                       # tells the bridge: do not split command
```

or, if additional argv tokens are needed:

```yaml
command: "/path with spaces/my agent"
args: ["--flag", "{{MESSAGE}}"]
```

YAML's double-quoted scalar carries the spaces; the bridge passes the string to `execve` as a single argv token. The same rule applies to any `args` element that contains whitespace.

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
| `AGENT_MESSAGE` | User message text. May be empty — see "Empty messages" below. |
| `AGENT_SESSION_ID` | Session ID from the previous turn (empty string = new session) |
| `AGENT_SESSION_NAME` | Human-readable session name (default: `"default"`) |
| `AGENT_FROM_USER` | Sender identifier (platform-specific: user ID, handle, etc.) |
| `AGENT_STREAMING` | `"1"` = streaming mode, `"0"` = one-shot mode |
| `AGENT_PROTOCOL_VERSION` | Protocol version string, e.g. `"0.1"`. **Opaque and non-comparable** — see [Versioning](#versioning). Agents MUST NOT order or range-check this value. |

#### Empty messages

`AGENT_MESSAGE` MAY be an empty string. A turn is considered to "carry content" when **any** of the following is true:

- `AGENT_MESSAGE` is non-empty
- `AGENT_IMAGE_URL` is non-empty
- `AGENT_FILE_URL` is non-empty
- `AGENT_ATTACHMENTS` is set and is not `[]`

If none of the above holds, the turn is empty and the bridge / agent SHOULD surface an error rather than proceed. This rule accommodates the common "image-only message" case where a user posts a screenshot with no accompanying text.

### Attachment variables (P0)

Attachments are conveyed by two layers of variables. Bridges MUST set the layer that matches the message; agents SHOULD read the richer layer when present and fall back to the simpler one when not.

**Single-attachment convenience variables.**

| Variable | Description |
|----------|-------------|
| `AGENT_IMAGE_URL` | Image attachment URL. Set when the message contains exactly one image. |
| `AGENT_FILE_URL` | File attachment URL. Set when the message contains exactly one file. |

**Multi-attachment variable.**

| Variable | Description |
|------|-------------|
| `AGENT_ATTACHMENTS` | JSON array of `{"type":"image\|file\|audio\|video", "url":"...", "name":"..."}`. Set when the message carries zero or more attachments. An empty array is equivalent to "no attachments". |

**Agent reading order.** When `AGENT_ATTACHMENTS` is non-empty, agents SHOULD consume it and ignore the single-attachment vars. When `AGENT_ATTACHMENTS` is unset (empty string), agents SHOULD fall back to `AGENT_IMAGE_URL` / `AGENT_FILE_URL`.

**Bridge consistency requirement.** When a bridge sets `AGENT_ATTACHMENTS` **and** one of the single-attachment vars in the same turn, the two MUST agree: the URL in the single-attachment var MUST equal the `url` of the corresponding entry in `AGENT_ATTACHMENTS`. A bridge that cannot keep them consistent MUST set only one layer.

**Type taxonomy.** The `type` field is one of `image`, `file`, `audio`, `video`. Bridges MAY emit additional types; agents that do not recognise a type SHOULD ignore that entry rather than fail.

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
- **Interaction with `AGENT_ERROR:`** — a CLI's terminal event frequently carries both the session id and an error indication (e.g. `result{session_id, is_error: true}`). When an `AGENT_SESSION:` line and an `AGENT_ERROR:` line appear in the same turn, the bridge MUST still persist the session id for the next turn, even though the current turn is reported to the user as a failure. The error terminates the turn; it does not invalidate the session. Agents that emit `AGENT_ERROR:` and have already learned their session id SHOULD emit the `AGENT_SESSION:` line first (or at any point — the bridge honours last-wins either way).

The session-ID string is **opaque** — the bridge stores and forwards it verbatim and MUST NOT interpret its format. It MAY be a UUID, a CLI-internal handle, or any short opaque token. It MUST NOT contain whitespace, control characters, or colons (`:`): the colon collides with the `AGENT_SESSION:` delimiter and whitespace/control characters break round-tripping through env vars and argv. If an agent emits a `AGENT_SESSION:` line whose value violates this, the bridge SHOULD log a warning to stderr and **ignore the line** (the previously captured session id is preserved; if none was captured yet, the session stays empty). A valid id is non-empty after stripping and matches `^[A-Za-z0-9._~+/=-]+$` (URL-safe characters plus `-` and `.`); agents that emit anything else will not round-trip.

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

This line is honored **regardless** of `streaming` mode. The bridge forwards the decoded string to the user as an error reply and SHOULD stop forwarding any further `AGENT_PARTIAL:` lines from the same turn (see "Interaction with already-delivered partials" below — already-delivered chunks are not retracted, only future ones are suppressed).

Once an `AGENT_ERROR:` line has been emitted, the bridge MAY stop reading the agent's stdout entirely — it has already captured the error and (per the session-line rule) the final session id if one appeared before the error. The agent process is expected to exit shortly after; if it does not, the bridge's normal timeout applies. (Hub bridges that wrap an NDJSON-emitting CLI take this option: they emit `AGENT_ERROR:` and let the process wind down, rather than continuing to parse a stream whose subsequent events no longer affect the user-visible result.)

If an `AGENT_ERROR:` line is emitted, the bridge MUST treat the turn as failed even if the process exits 0. The agent SHOULD exit with a non-zero code after emitting `AGENT_ERROR:`, but a bridge MUST NOT rely on that — the `AGENT_ERROR:` line alone is sufficient to mark the turn failed.

Any reply body produced alongside an `AGENT_ERROR:` line is discarded.

**SDK convention.** SDKs that wrap this protocol (e.g. the official Python and Node `create_profile`/`createProfile` helpers) treat `send_error()` as **terminal**: the agent SHOULD NOT emit further `AGENT_PARTIAL:` or reply body after calling it. The SDK MAY enforce this by exiting the process immediately after `send_error()`. This is a stricter rule than the raw protocol requires (the protocol allows an agent to emit `AGENT_ERROR:` and continue writing — the bridge just discards the rest), but it is the recommended SDK ergonomic because mixing an error with subsequent content is confusing to users.

#### Interaction with already-delivered partials

When `AGENT_PARTIAL:` lines were forwarded before the `AGENT_ERROR:` arrived (the common "stream half the answer, then hit an upstream rate limit" case), the bridge MUST NOT attempt to retract, edit, or annotate the already-delivered chunks — they have already been shown to the user. The `AGENT_ERROR:` text is delivered as-is, after any partials.

This means the user may see a half-finished reply followed by an error. This is intentional: retracting delivered text is not possible on most messaging platforms, and trying to do so (delete + repost) is racy and surprising. Bridges that want to soften this MAY insert a visible separator (e.g. a newline or "—" rule) between the last partial and the error message, but MUST NOT rewrite or remove the partial text.

Agents that prefer a clean failure MAY choose to buffer their output and emit no `AGENT_PARTIAL:` until they are confident the turn will succeed — but then they lose streaming, which is the trade-off.

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

### Precedence when multiple failure signals arrive

A turn may produce more than one failure signal — for example, the agent emits `AGENT_ERROR:` and then the bridge kills it on timeout before it exits, or the agent exits non-zero after emitting `AGENT_ERROR:`. The bridge resolves the final exit code by this precedence (highest first):

1. **Timeout (124)** — the bridge killed the process. A timeout is always reported as `124` regardless of what the agent emitted before the kill.
2. **`AGENT_ERROR:` (1)** — the agent emitted an error line. Reported as `1` even if the process then exited 0.
3. **Process exit code** — whatever the process returned, used when neither of the above apply.

Rationale: a timeout is a bridge-level failure mode that the agent cannot recover from, so it takes precedence. `AGENT_ERROR:` is the agent's own signal that something went wrong, which takes precedence over the raw exit code (because the agent may exit 0 after emitting `AGENT_ERROR:` for self-diagnostic reasons).

stderr is captured as a debug log and not shown to the user, unless `include_stderr_in_reply: true`.

---

## Timeout Handling

When `timeout_secs` is reached without the process exiting:

1. Bridge sends `SIGTERM` to the process.
2. Bridge waits `kill_grace_secs` (default 5) for the process to exit.
3. If still running, bridge sends `SIGKILL`.

Any `AGENT_PARTIAL:` lines already received are forwarded to the user. The bridge then sends a timeout error reply (subject to `send_error_reply`).

The agent SHOULD handle `SIGTERM` by flushing any buffered partial output and exiting promptly.

**Windows caveat.** `SIGTERM` and `SIGKILL` do not exist as deliverable signals on Windows. A bridge running on Windows MUST still honour the two-step intent — first a "polite" termination request (on Windows, `TerminateProcess` is the only available lever, so the grace period collapses to zero) and then, if the process is still alive after `kill_grace_secs`, a hard termination. POSIX bridges implement the full SIGTERM → grace → SIGKILL sequence. Agents that need to flush on shutdown cannot rely on receiving a signal on Windows and SHOULD use `atexit`-style hooks or explicit flush-before-exit discipline instead.

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

**Relationship to AgentProc:** **Richer cousin, different job.** ACP assumes an interactive IDE session with tool calls, file diffs, and mode switching. AgentProc assumes a single chat turn per process invocation. Use ACP if you're building an IDE; use AgentProc if you're bridging a chat bot to a CLI.

The overlap is only superficial. An ACP client must implement file-system, terminal, and permission callbacks because the IDE owns the files the user is editing; an AgentProc bridge owns no user files and renders no diffs. Conversely, ACP offers no unattended-runtime semantics — no timeout, no `SIGTERM`/`SIGKILL` grace, no "tell the user when the agent errored" contract — because an IDE user stops a runaway agent by hand. A messaging bridge runs unattended, so those are load-bearing for AgentProc and out of scope for ACP. Even when the underlying CLI happens to be ACP-compatible (e.g. Claude Code driven over ACP by Zed), building an IM bridge on top of an ACP client is over-engineering: the bridge would implement capabilities it never uses and still miss the timeout/error-reply guarantees the chat scenario requires. AgentProc's contract — env vars in, sentinel-prefixed stdout out, one process per turn — is the smallest one that fits the bridge-to-CLI job.

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

Document revisions are tracked here. The wire protocol has remained `0.1` since the initial draft; entries below record editorial changes and clarifications to this document, not wire-format changes.

- **doc 0.5** — Defined empty-`AGENT_MESSAGE` semantics (legal when attachments are present). Disambiguated `command`/`args`: `args: []` (explicit empty) now means "do not split", distinct from `args` absent. Added `${VAR}` security warning for profile `env` blocks. Added optional `env_allowlist` profile field: when present, `${VAR}` references not in the list expand to empty + a stderr warning, shrinking the trust boundary from the full environment to the declared variables. Codified `AGENT_ERROR:` interaction with already-delivered partials (not retracted), and that the bridge MAY stop reading stdout after the error. Restated the session-id format constraint (no whitespace/control/colon) and defined bridge behaviour on violation (ignore the line, preserve previous id, warn). Codified exit-code precedence (timeout > `AGENT_ERROR:` > exit code). Documented SDK `send_error` terminality.
- **doc 0.4** — Split wire-protocol version (`0.1`) from document revision in the header; added a Versioning section codifying that `AGENT_PROTOCOL_VERSION` is an opaque, non-comparable string. Promoted `AGENT_ATTACHMENTS` from Draft to P0 with a consistency requirement when bridges set it alongside the single-attachment vars. Clarified session-line ordering: when a CLI emits `AGENT_SESSION:` together with `AGENT_ERROR:` (the common `result{is_error}` shape), bridges MUST preserve the session id for the next turn even though the current turn is reported as a failure. Added `AGENT_ERROR:` → bridge MUST treat the turn as failed regardless of exit code. Defined `command` as argv[0] and `args` as the remaining argv, with a quoting rule so paths containing whitespace remain expressible without a shell. Noted Windows caveat for the timeout SIGTERM/SIGKILL contract.
- **0.1.0** — Initial public draft. Defined env-var input, sentinel-prefixed stdout, `AGENT_SESSION:` / `AGENT_PARTIAL:` / `AGENT_ERROR:`, session-line "last wins" rule, `AGENT_PROTOCOL_VERSION`, `AGENT_ATTACHMENTS` (draft), timeout/SIGTERM contract, exit-code conventions, stdin EOF contract, command-execution no-shell rule.
