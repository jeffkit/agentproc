# AgentProc Protocol Specification

**Wire protocol:** `0.4` (the string carried in the `protocol_version` field of the turn object)
**Document revision:** `1.2`
**Status:** Stable

The wire protocol and this document are versioned **independently**. The wire version only changes when the bytes on stdin/stdout change; the document revision tracks editorial updates, clarifications, and new guidance that does not alter what a conformant agent or bridge must send or accept. See [Versioning](#versioning) below for the rule an implementer should apply when reading `protocol_version`.

---

## Versioning

`protocol_version` is an **opaque string**, not a comparable number. Agents and bridges MUST NOT attempt to order, compare, or range-check it. Two strings are either equal or not equal.

- If a bridge sends a version string the agent does not recognise, the agent SHOULD behave as if the field were unset (best-effort, fail-soft).
- If an agent expects a version string the bridge does not send, the agent MUST fall back to its built-in default.
- The string is not a feature-detection mechanism: there is no negotiation, no capability advertisement, and no ordering. Agents that need to know whether a specific feature (e.g. an image attachment) is present MUST inspect the relevant field in the [turn object](#input--stdin-turn-object) directly (e.g. a non-empty `attachments` array), not the version string.

The rationale is that any comparable version invites implementers to gate behaviour on `>= 0.4`, which breaks the moment a bridge ships without bumping the number. Treating the string as opaque keeps the contract honest: presence of a feature is signalled by presence of the field that carries it.

---

## Overview

AgentProc is a minimal protocol for connecting any agent CLI to a messaging platform via a process-based interface. It defines how a **bridge** (the platform adapter) communicates with an **agent process** (a script or executable that wraps an AI agent).

```
Messaging Platform
      │
      ▼
   Bridge                ← reads profile YAML, manages process lifecycle
      │   stdin: one NDJSON turn object (then optional permission responses)
      │   env:   secrets / config (profile env block)
      │   argv:  launch params via {{SESSION_ID}} etc.
      ▼
 Agent Process           ← your script or binary (implements the contract below)
      │   stdout: NDJSON events (one JSON object per line)
      ▼
   Bridge                ← forwards reply to the platform
```

The protocol has three input paths and one output path:

- **Input — stdin:** a single NDJSON [turn object](#input--stdin-turn-object) describing this turn, written before the process starts (or just after). When [optional tool permission](#optional-tool-permission) is enabled, stdin stays open and the bridge writes further NDJSON `permission_response` objects mid-turn.
- **Input — environment variables:** secrets and configuration (the profile `env` block, plus a minimal infra set). The per-turn request does **not** travel in env vars in 0.4 — it travels in the stdin turn object.
- **Input — argv placeholders:** `{{SESSION_ID}}`, `{{SESSION_NAME}}`, `{{PROFILE_DIR}}` substituted into `command`/`args` before launch. The message is delivered via stdin only — not in argv.
- **Output — stdout:** NDJSON events, one JSON object per line, distinguished by a `type` field.

No HTTP, no sockets, no shared memory. Just a process.

---

## Profile YAML

A profile is a YAML file that tells the bridge how to launch an agent process.

```yaml
# Required: the executable to run (always argv[0], never split).
# Optional when `executor:` is set and the SDK recognises the name.
command: python3
args: ["{{PROFILE_DIR}}/bridge.py"]   # argv[1..]; defaults to [] if omitted

# Optional in-process executor (SDK-specific).
# When present and the SDK recognises the name, the runner invokes the named
# executor in-process instead of spawning `command`/`args`. This eliminates the
# bridge-process fork overhead. `command`/`args` are used as a fallback when the
# SDK does NOT recognise the name; if the SDK does not recognise it and
# `command` is absent, the runner MUST hard-fail with a clear error listing
# known executors.
#
# Resolution rules:
#   executor present + SDK knows it     → run in-process; ignore command/args
#   executor present + SDK unknown      → warn, spawn command (fallback)
#   executor present + SDK unknown + no command  → hard fail
#   executor absent                     → spawn command (existing behaviour)
#
# Built-in executor names (Node SDK): see `sdk.executorNames`.
executor: claude-code         # optional; omit to use the command/args spawn path

# Execution environment
cwd: /path/to/workspace       # working directory (~ and placeholders supported)
env:                          # extra environment variables (secrets / config)
  MY_API_KEY: "${MY_API_KEY}" # reference existing env vars with ${VAR}
env_allowlist: [MY_API_KEY]   # optional: restrict which ${VAR} the env block may read

# Output control
timeout_secs: 600             # per-turn wall-clock timeout (secs), default 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL grace period, default 5

# Streaming (bridge-side hint)
streaming: true               # forward {"type":"partial"} events in real time

# Optional tool permission (default false — see "Optional tool permission")
permission: false             # true → keep stdin open; honor permission_request/response frames
```

### Placeholders

Placeholders in `command`, `args`, `cwd`, and `env` values are replaced before the process starts. They are **not** passed through a shell.

| Placeholder | Value |
|-------------|-------|
| `{{SESSION_ID}}` | Session ID from the previous turn (empty = new session) |
| `{{SESSION_NAME}}` | Human-readable session name |
| `{{PROFILE_DIR}}` | Absolute path to the directory containing the profile YAML. Lets a profile reference a bundled script (e.g. `command: python3`, `args: ["{{PROFILE_DIR}}/bridge.py"]`) independently of the agent's `cwd`. Bridges set this when invoking a profile by path; if unset (e.g. programmatic use without a file), it expands to empty. |

> **Note:** `{{MESSAGE}}` is not a supported placeholder. The user message is always delivered to the agent via the stdin turn object, never in argv. Putting user input in argv leaks it to `ps(1)` and may hit system arg-length limits.

### `${VAR}` expansion in `env` values

Values in the profile `env` block may reference the bridge's own environment variables with `${VAR}` syntax (e.g. `MY_API_KEY: "${MY_API_KEY}"`). This is **substitution against the bridge process's full environment**, not against the profile or the agent.

**Security implication.** A profile is **trusted input** — anyone who can write the profile can read every environment variable the bridge has access to via `${VAR}` expansion (cloud credentials, tokens, secrets). This is by design (profiles are configuration, not user input), but it has one practical consequence worth calling out:

> **Do not run profiles from untrusted sources.** `agentproc hub run <name>` fetches a profile from a GitHub repo and runs it. If you would not trust that repo's maintainer to *read* your shell environment through `${VAR}` references, do not run their profile. `env_allowlist` (below) shrinks what `${VAR}` may expand. The trust decision still rests with the user running the profile.

Bridges expand `${VAR}` using POSIX-shell semantics: unknown variables expand to the empty string, not to the literal `${VAR}`.

### `env_allowlist` — shrinking `${VAR}` expansion

By default, every `${VAR}` in the `env` block expands against the bridge's full environment (so a profile author can pull credentials they declare). `env_allowlist` lets a profile shrink that expansion surface to exactly the variables it needs:

```yaml
env:
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
env_allowlist: [ANTHROPIC_API_KEY]
```

- **Optional.** When `env_allowlist` is absent, all `${VAR}` references expand against the bridge's full environment.
- **When present:** a `${VAR}` whose name is **not** in the list expands to the empty string, and the bridge logs a warning to stderr (e.g. `env_allowlist blocked ${AWS_SECRET_ACCESS_KEY}; expanded to empty`). The process still starts — a typo in either the value or the list surfaces as an empty variable and a warning, not a hard failure.
- **Scope.** `env_allowlist` governs `${VAR}` expansion inside the profile `env` block only. It does not affect the infra set (below), `{{SESSION_ID}}` / `{{PROFILE_DIR}}` placeholders, or `env` values that contain no `${VAR}` at all.
- **No globbing.** Names must match exactly. `["ANTHROPIC_*"]` does not match `ANTHROPIC_API_KEY` — list each name in full.
- **Recommendation.** Hub profiles SHOULD set `env_allowlist` so that `${VAR}` expansion is an explicit declaration of which credentials the profile reads. Combined with the always-minimal infra set, "what they declare" is what the agent sees.

### Child environment composition

The agent process's environment is built from exactly three layers, in this order (later layers override earlier):

1. **Infra set** — the bridge copies these names from its own environment into the child (when set): `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `LC_CTYPE`, `LC_MESSAGES`, `TERM`, `TMPDIR`, `TZ`, `PWD`, and on Windows `SystemRoot`, `TEMP`, `TMP`, `USERPROFILE`, `USERNAME`, `PATHEXT`, `COMSPEC`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE`, `OS`. These are operational variables an agent needs to find its interpreter, temp directory, and locale — none of them are credential-bearing. The infra set is **always** applied; there is no "inherit everything" mode in 0.3.
2. **Profile `env` block** — after `${VAR}` expansion and `env_allowlist` filtering.
3. **CLI `--env` extras** — if the bridge's CLI exposes a per-run override.

A profile that needs a non-secret variable the bridge has (e.g. a custom `WORKSPACE_DIR`) must declare it in the `env` block. Undeclared ambient variables do **not** reach the agent. (0.3 removes the `env_inherit: all` escape hatch that existed in 0.2; profiles that relied on ambient vars must declare them.)

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

- **`command`** — the executable (argv[0]). A single token, **never split**, even if it contains whitespace. If it contains whitespace, the bridge passes the entire string to `execve` as one argv token.
- **`args`** — a YAML list of additional argv tokens (argv[1..]). Each list element is one argv token, verbatim. **Defaults to `[]` when omitted.**

The resulting argv (`[command, *args]`) is passed to the platform's `execve` (or equivalent) **without invoking a shell**. This lets `command` carry a path that contains spaces.

```yaml
# Multi-token command — the normal form:
command: python3
args: ["{{PROFILE_DIR}}/bridge.py", "--flag"]

# Single token, no args — args defaults to []:
command: ./my_agent

# Path with spaces — no special handling needed, command is one token:
command: "/path with spaces/my agent"
args: []
```

**Migration from 0.2.** 0.2 allowed a shorthand where `args` *absent* + `command` containing whitespace meant "split `command` on whitespace into argv". 0.3 removes this shorthand: `command` is always one token. Profiles that wrote `command: python3 {{PROFILE_DIR}}/bridge.py` must split it into `command: python3` + `args: ["{{PROFILE_DIR}}/bridge.py"]`. The migration is mechanical.

If a bridge implementation chooses to use a shell (e.g., for environment-variable expansion), it MUST apply POSIX shell quoting to every placeholder substitution. Bridges SHOULD prefer the no-shell form.

### `permission` field

| Value | Behavior |
|-------|----------|
| `false` / absent (default) | Optional permission frames are **not** part of this turn. Bridges that see a `{"type":"permission_request"}` event SHOULD log a warning and ignore it (do not block). Agents that need tool approval without this field MUST use a CLI-side auto-approve mode (e.g. `--dangerously-skip-permissions`, `--yolo`) or pre-allow tools. |
| `true` | Enables the optional permission channel for this profile. The bridge MUST keep stdin open, honor `{"type":"permission_request"}` events and write matching `{"type":"permission_response"}` objects, and set `permission: true` in the [turn object](#input--stdin-turn-object). |

`permission` is **opt-in**. Profiles and CLIs that have no mid-turn approval channel keep working unchanged.

### In-process executors

The `executor:` field (see [Profile YAML](#profile-yaml)) selects an **in-process executor** — a named, SDK-registered implementation of the bridge side of the protocol. When the runner recognises the name, it invokes the executor directly **in the runner's own process**, spawning the target CLI without forking a bridge subprocess first. This eliminates the bridge-process fork overhead while reusing the same CLI-adapter logic that standalone bridge scripts carry.

Executors are **SDK-specific**. The set of recognised names, and the API for registering new ones, is defined by each SDK (Python / Node / Rust / …) — there is no cross-SDK registry. A profile that sets `executor:` to a name the host SDK does not recognise falls back to the `command`/`args` spawn path per the four-case rule above. This means a single profile can run three ways depending on what the host has installed: Rust executor (in-process), Node executor (in-process), or Python bridge script (spawn) — all producing the same observable NDJSON.

#### Executor interface

Every executor exposes the following surface. SDKs MAY use different concrete syntax (a JS object, a Python class, a Rust trait) but MUST preserve these semantics:

| Field | Type | Description |
|-------|------|-------------|
| `cliName` | string | The CLI binary name, used in error messages (e.g. `"claude"`, `"codex"`). |
| `installHint` | string | Human-readable install instruction appended to "CLI not found" errors. |
| `plain` | boolean | `true` = the CLI emits plain text on stdout (not NDJSON); the runner treats the entire stdout as the reply body and does **not** call `parseEvent`. `false` (default) = the CLI emits NDJSON, one JSON object per line, decoded via `parseEvent`. |
| `buildArgs` | `(message, sessionId, env) -> string[]` | Builds the target CLI's argv. `message` is the turn's user message; `sessionId` is the previous turn's session id (empty string = new session); `env` is the composed child environment (infra set + profile `env` after `${VAR}` expansion + `env_allowlist` filtering). The returned argv is passed to `execve` **without** a shell. Returning an empty array is a hard error. |
| `parseEvent` | `(event) -> ParseResult \| null` | Translates one decoded JSON object from the CLI's stdout into a `ParseResult`. Return `null` for events the executor does not recognise (the runner logs and ignores the line). Omitted / unused when `plain: true`. |
| `makeHandlers` | `() -> { buildArgs, parseEvent }` | Optional factory for **stateful** executors that need per-turn state shared between `buildArgs` and `parseEvent` (e.g. a session id minted in `buildArgs` and returned in `parseEvent`). When present, the runner calls `makeHandlers()` **once per turn** and uses the returned pair for that turn only. When absent, the runner uses `buildArgs` / `parseEvent` directly, and they MUST be stateless and re-entrant. |

`buildArgs` and `parseEvent` (or the pair returned by `makeHandlers`) form a closed turn-local contract: the runner calls `buildArgs` once before spawn, then calls `parseEvent` for each stdout line until EOF. The executor MUST NOT assume any ordering between events beyond what the CLI itself guarantees.

#### `ParseResult`

The object `parseEvent` returns. All fields optional; a `null` return means "this event contributes nothing".

| Field | Type | Meaning |
|-------|------|---------|
| `partialText` | string | A streaming chunk. The runner forwards it immediately as `{"type":"partial"}` (when `streaming: true`), and accumulates it for the final body. |
| `finalText` | string \| null | The terminal reply body. `null` (the value, not omission) explicitly means "no text body this event". When the turn ends with no `finalText` and no accumulated `partialText`, the runner treats the turn as having an empty body. |
| `sessionId` | string | A session id to persist for the next turn. First non-empty value wins; a different non-empty value later is a protocol violation (the runner keeps the first). |
| `error` | string | A terminal error message. The runner forwards it as `{"type":"error"}` and stops forwarding further `partial` events from the same turn. |
| `usage` | object | Token / cost stats attached to the run result. Forward-compatible; the runner SHOULD ignore keys it does not understand. |

`parseEvent` MAY return multiple fields at once (e.g. `{ partialText, sessionId }` — stream a chunk and stamp the session id in the same event). It MUST NOT return `partialText` and `error` together; an error is terminal.

#### `plain` executors

When `plain: true`, the runner does not decode stdout as NDJSON and does not call `parseEvent`. Instead:

- The entire stdout (UTF-8 decoded, trailing whitespace trimmed) becomes the reply body.
- Session continuity is not supported (no `sessionId` can be extracted from plain text by the contract itself — a `plain` executor that needs session continuity MUST use a bespoke run loop outside this interface, as `recursive` and `echo-agent` do).
- Timeouts (`timeout_secs` / `kill_grace_secs`) still apply.
- `streaming: true` has no effect for `plain` executors (there are no `partial` events to forward).

#### Runner contract

When the runner takes the in-process path (`executor:` present + recognised), it MUST:

1. Compose the child environment exactly as the spawn path does (infra set + profile `env` after expansion + CLI `--env` extras) and pass it to `buildArgs` as the `env` argument.
2. Resolve handlers via `makeHandlers()` if present, else use `buildArgs` / `parseEvent` directly.
3. Call `buildArgs(message, sessionId, env)` once. An empty return is a hard error.
4. Spawn the target CLI's argv directly (no bridge subprocess, no shell).
5. Apply `timeout_secs` / `kill_grace_secs` / `streaming` / `permission` with the same semantics as the spawn path.
6. For `plain: false`: decode stdout line by line, call `parseEvent` per line, forward `partialText` as `{"type":"partial"}`, accumulate `finalText`, persist the first non-empty `sessionId`, and on `error` emit `{"type":"error"}` and suppress further `partial`s.
7. For `plain: true`: treat stdout as the body, apply truncation, emit a single `{"type":"result"}` at turn end.
8. Emit a terminal `{"type":"result"}` (or `{"type":"error"}`) at turn end, carrying the first non-empty `sessionId` and any `usage` seen.

The in-process path and the spawn path MUST produce observably equivalent NDJSON for the same CLI + turn. This is verified by the shared conformance suite.

---

## Input — stdin turn object

Before the agent process reads its first byte of stdin, the bridge writes **exactly one** NDJSON line: the turn object. It is a single JSON object terminated by `\n`.

```json
{"type":"turn","message":"hello","session_id":"","session_name":"default",
 "attachments":[],"permission":false,"protocol_version":"0.4"}
```

### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Literal `"turn"`. |
| `message` | string | User message text. May be `""` — see "Empty turns" below. |
| `session_id` | string | Session ID from the previous turn (`""` = new session). |
| `protocol_version` | string | Protocol version string, e.g. `"0.4"`. **Opaque and non-comparable** — see [Versioning](#versioning). |

### Optional fields (present = relevant)

Optional fields use **presence-as-feature**: the bridge includes the field when the feature is in play, and omits it (or sends a neutral value) otherwise. An agent that needs to know whether a feature is supported checks whether the field is present, not the version string.

| Field | Type | Description |
|-------|------|-------------|
| `session_name` | string | Human-readable session name. Defaults to `"default"` when absent. |
| `attachments` | array | Attachment list for this turn. Each element is an object with at least `kind` (string, e.g. `"image"`, `"file"`) and `url` (string); bridges MAY include additional fields (e.g. `filename`, `mime_type`, `size`). Absent or `[]` = no attachments this turn. There is no separate single-attachment convenience field — `attachments` is the only attachment channel. |
| `permission` | boolean | `true` when the profile has `permission: true` and the bridge supports the optional permission channel; absent or `false` otherwise. Agents that can emit permission requests MUST check this before relying on mid-turn approval — absence means auto-approve / skip-permissions is the only option. |

Custom variables declared in the profile's `env` block are injected via the environment, not the turn object.

### Empty turns

A turn is considered to "carry content" when **any** of the following is true:

- `message` is non-empty
- `attachments` is present and non-empty

If none of the above holds, the turn is empty and the bridge / agent SHOULD surface an error rather than proceed. This rule accommodates the common "image-only message" case where a user posts a screenshot with no accompanying text.

### Reading the turn

The agent reads exactly one line from stdin and JSON-decodes it. After that:

- When `permission` is absent/false, the bridge closes stdin (EOF) immediately after the turn line. The agent MUST NOT block on stdin after reading the turn.
- When `permission` is true, the bridge keeps stdin open; the agent MAY read further lines (see [Optional tool permission](#optional-tool-permission)).

---

## Output — stdout NDJSON events

The agent process writes to stdout. The bridge reads it line by line in real time. **Every line is a JSON object** terminated by `\n`, carrying a `type` field that says what kind of event it is.

### Event types

| `type` | Direction | Description |
|--------|-----------|-------------|
| `partial` | agent → bridge | A streaming chunk, forwarded to the user immediately. |
| `result` | agent → bridge | Terminal success body for this turn (at most one). |
| `error` | agent → bridge | A terminal error message to forward to the user. |
| `permission_request` | agent → bridge | Optional tool-permission request (only when `permission: true`). |

### Closed vocabulary

The event set above is **closed**. The six `type` values — `turn` (stdin), `partial`, `result`, `error`, `permission_request`, and `permission_response` (stdin) — are the entire protocol vocabulary. AgentProc deliberately does **not** grow typed events for tool calls, file diffs, plan updates, reasoning blocks, or other richer semantics. An agent that needs those should wrap an IDE-oriented protocol (e.g. ACP) internally; an AgentProc bridge renders no diffs and owns no user files, so it has nothing to do with such events. Bridges MUST NOT expect additional event types, and agents MUST NOT invent them as a way to smuggle richer semantics through this protocol — that path leads to reimplementing ACP poorly. Unknown `type` values are handled per [Malformed lines](#malformed-lines).

This protocol is scoped to **one turn**: one user message, one process, one reply. Long-lived sessions, mid-turn cancellation, concurrent requests, and client-provided callbacks (file system, terminal) are out of scope by design — they are what make ACP an IDE protocol rather than a chat-bridge protocol.

### `session_id` on events

Session continuity is carried as an optional field on stdout events, **not** as a dedicated event type.

| Rule | Requirement |
|------|-------------|
| Persistence | The bridge persists the **first** non-empty `session_id` observed in the turn and passes it back as `turn.session_id` on the next turn. If no event carries a non-empty `session_id`, the bridge does not learn a new agent session id from this turn. |
| Once known | After a non-empty `session_id` is known, agents SHOULD attach that same value to every subsequent stdout event. Early events MAY omit the field (e.g. while waiting for a CLI `init`). Omitting-then-presenting is **not** a protocol violation. |
| Non-empty when present | On output, `session_id` MUST NOT be `""`. Empty string on input (`turn.session_id`) still means “new session”; that meaning does not apply on output. |
| Stateless agents | Agents with no native resume/session MUST **omit** the field on every event. They MUST NOT mint an id when the underlying tool cannot resume with it. Generating an id the tool itself requires as an input (e.g. a CLI `--session <id>` flag) and returning that same id on events is allowed — that is not “minting solely to populate the field.” |
| Inbound stamp | When `turn.session_id` is non-empty, resume-capable agents SHOULD include that same value on every stdout event from the first event onward (no discovery buffering). Discovery buffering applies only when starting a new session (`turn.session_id` is `""`) and the CLI assigns an id asynchronously. |
| No permission buffering | Agents MUST NOT buffer a `permission_request` (or any event that requires a stdin response) while waiting for a session id. Prefer stamping the inbound `turn.session_id` when present, or emitting the request without the field until the id is known. |
| Disagreement | A **different** non-empty `session_id` after one has already been observed is a protocol violation. The bridge SHOULD log a warning to stderr and MUST keep the first non-empty value (fail-soft). It MUST NOT invent an id. |

**Plain-CLI executors.** Some SDK executor implementations run a CLI that emits plain text (not NDJSON) and therefore cannot surface a `session_id` via stdout events. For these executors, session continuity is handled entirely in the executor's argument-building step:

- When the CLI accepts a session or conversation flag (e.g. `agy`'s `--conversation <id>`), the executor passes the inbound `session_id` to that flag. If `session_id` is empty (new session), the executor generates a stable id (e.g. a UUID) and passes it. After the process exits successfully, the SDK returns that id in `RunResult.sessionId` so the host can pass it back on the next turn.
- When the CLI does not expose a resumption flag (`aider`, `deepseek`, `pi`), `RunResult.sessionId` is `""`. The host is responsible for persisting the id it sent and passing it back on subsequent turns; the SDK cannot learn it from the process output.

`session_id` on the wire MUST be a non-empty JSON string that contains no path separators (`/`, `\`) and no control characters (including NUL and newline). The reference SDK runners enforce this constraint at the wire classification step: a non-conforming `session_id` is silently dropped and a warning is logged to stderr (the previously captured id, if any, is preserved). The reason the constraint lives at the runner level — not only at storage — is that the SDK history helpers persist sessions as `<id>.jsonl` files; a `session_id` containing `/` would silently create a subdirectory instead of a flat file. Colons, spaces, hyphens, and most other printable characters are accepted (e.g. `"org:proj:thread-42"` is valid; `"org/proj/thread"` is not).

### Optional `usage` on terminal events

`result` and `error` MAY include a `usage` object for token/cost stats. Bridges MAY ignore it. There is **no** separate `usage` event type. Additional keys are forward-compatible and SHOULD be ignored if unrecognised.

Recommended keys (all optional):

| Key | Type | Description |
|-----|------|-------------|
| `input_tokens` | number | Total input tokens billed (may include cached tokens, per provider convention). |
| `output_tokens` | number | Total output tokens billed. |
| `total_tokens` | number | Sum of input and output tokens. |
| `cache_read_input_tokens` | number | Anthropic prompt-cache hits. These are a subset of `input_tokens` under standard Anthropic billing. |
| `cache_creation_input_tokens` | number | Anthropic prompt-cache writes (first-write cost). |
| `reasoning_tokens` | number | Reasoning/thinking tokens (OpenAI o-series, Claude extended thinking). These are a **subset** of `output_tokens`, not additive. |
| `duration_ms` | number | Agent-measured wall-clock time for the turn (excludes spawn/IPC overhead). |
| `cost_usd` | number | Estimated cost in USD. Bridges that ship a pricing table MAY populate this; bridges without a pricing source SHOULD omit it rather than guess. |

Convention for `input_tokens` and cache fields: the standard interpretation is `input_tokens = non-cached input + cache_read_input_tokens + cache_creation_input_tokens` (i.e. cache tokens count toward the total). Bridges SHOULD follow this so that `input_tokens` always represents the billable input total.

### `{"type":"partial"}` — streaming chunk

```json
{"type":"partial","text":"hello ","role":"output","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Literal `"partial"`. |
| `text` | string | The chunk text. May contain newlines, emoji, etc. — JSON-escaped as needed. |
| `role` | string | Optional. `"output"` (default) or `"thinking"`. Lets the agent distinguish assistant output from reasoning/thinking text. Bridges MAY render thinking differently (e.g. collapsed, greyed) but MUST forward it. Unknown values are forwarded as-is. |
| `session_id` | string | Per [`session_id` on events](#session_id-on-events). |

Additional fields MAY be included by the agent (forward-compatibility); bridges SHOULD ignore fields they do not understand.

When `streaming: false` is set in the profile, the bridge ignores all `partial` events and assembles the reply from the `result` event only.

### `{"type":"result"}` — terminal success body

```json
{"type":"result","text":"Here is the complete answer.","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c","usage":{"input_tokens":12,"output_tokens":34}}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Literal `"result"`. |
| `text` | string | The final reply body for this turn. MAY be `""` when the full body was already delivered via `partial` (the bridge skips a duplicate final send when appropriate). |
| `session_id` | string | Per [`session_id` on events](#session_id-on-events). |
| `usage` | object | Optional. Per [Optional `usage`](#optional-usage-on-terminal-events). |

**At most one `result` per turn.** If a second `result` appears, the bridge SHOULD log a warning and MUST ignore it.

**Assembling the user-visible body:**

- When `streaming: true`: the user-visible body is the concatenation of forwarded `partial` texts. If `result.text` is non-empty and no `partial` was forwarded, the bridge uses `result.text` as the body. If one or more `partial`s were already forwarded, the bridge MUST NOT append `result.text` again (treat it as terminal metadata for `usage` / completeness only — many CLIs repeat the full assembled text in their own terminal event).
- When `streaming: false`: the body is `result.text` only (all `partial` events are ignored).

A turn that produces no `result` (and no `partial`) and exits `0` is a **successful** turn. A turn that streams only via `partial` and exits `0` without a `result` is also successful (the user-visible body is whatever partials were forwarded). Agents that have `usage` to publish SHOULD still emit a trailing `{"type":"result","text":"",...}` so stats have a home. An empty output is only a failure when paired with a non-zero exit code or an `error` event.

After the first `error` in a turn, the bridge MUST treat the turn as failed; subsequent `error` or `result` events SHOULD be ignored. `usage` on a `partial` event MUST be ignored (bridges MAY warn).

### `{"type":"error"}` — error event

```json
{"type":"error","message":"Upstream API rate limited. Try again in 60s.","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Literal `"error"`. |
| `message` | string | A user-readable error message. |
| `session_id` | string | Per [`session_id` on events](#session_id-on-events). Present when the agent has a session (including the common “CLI result with `is_error`” case) so the bridge can still persist continuity after a failed turn. |
| `usage` | object | Optional. Per [Optional `usage`](#optional-usage-on-terminal-events). |

This event is honored **regardless** of `streaming` mode. The bridge forwards the message to the user as an error reply and MUST stop forwarding any further `partial` events from the same turn (already-delivered chunks are not retracted, only future ones are suppressed — see "Interaction with already-delivered partials" below). A `result` event that arrives after an `error` MUST be discarded (it cannot contribute to the reply body of a failed turn).

Once an `error` event has been emitted, the bridge MAY stop reading the agent's stdout entirely — it has already captured the error and (when present) the session id. The agent process is expected to exit shortly after; if it does not, the bridge's normal timeout applies.

If an `error` event is emitted, the bridge MUST treat the turn as failed even if the process exits 0. The agent SHOULD exit with a non-zero code after emitting `error`, but a bridge MUST NOT rely on that — the `error` event alone is sufficient to mark the turn failed.

**SDK convention.** In the reference SDKs (`create_profile`/`createProfile`), `send_error()` / `sendError()` is **non-terminal**: after emitting the `{"type":"error"}` event, the handler MAY continue and return a body. Both the `error` event and any subsequent `result` event will appear on stdout; the bridge discards the `result` per the rule above, so only the error reaches the user. For a truly fatal path use `ProtocolError` / `protocolError()` instead, which terminates the process with a non-zero exit.

#### Interaction with already-delivered partials

When `partial` events were forwarded before the `error` arrived (the common "stream half the answer, then hit an upstream rate limit" case), the bridge MUST NOT attempt to retract, edit, or annotate the already-delivered chunks — they have already been shown to the user. The `error` message is delivered as-is, after any partials.

This means the user may see a half-finished reply followed by an error. This is intentional: retracting delivered text is not possible on most messaging platforms, and trying to do so (delete + repost) is racy and surprising. Bridges that want to soften this MAY insert a visible separator (e.g. a newline or "—" rule) between the last partial and the error message, but MUST NOT rewrite or remove the partial text.

Agents that prefer a clean failure MAY choose to buffer their output and emit no `partial` until they are confident the turn will succeed — but then they lose streaming, which is the trade-off.

### Malformed lines

A stdout line that is not valid JSON, is valid JSON but not an object, or lacks a recognised `type` is a protocol violation. The bridge SHOULD log a warning to stderr and **ignore the line**. It is **not** forwarded to the user as reply body — in 0.4, reply body is carried only by `result` (and live `partial` when streaming). (This is stricter than 0.2, which treated any non-prefixed line as body. The cost is that a hand-written `echo "hello"` shell agent is no longer a valid agent; see [Design Rationale](#design-rationale).)

### Complete examples

**Streaming with session id on every event (resume-capable agent):**

```
{"type":"partial","text":"Here is the first part of the answer. ","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
{"type":"partial","text":"And here is the second part.","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
{"type":"result","text":"","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c","usage":{"input_tokens":12,"output_tokens":34}}
```

**One-shot with session (body only in `result`):**

```
{"type":"result","text":"Here is the complete answer.","session_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479"}
```

**Stateless agent (no `session_id` field):**

```
{"type":"result","text":"Here is the complete answer."}
```

**Thinking + output, streamed:**

```
{"type":"partial","role":"thinking","text":"Let me consider the options... ","session_id":"cli-sess-…"}
{"type":"partial","role":"output","text":"The answer is 42.","session_id":"cli-sess-…"}
{"type":"result","text":"","session_id":"cli-sess-…"}
```

**Error mid-stream (session still carried):**

```
{"type":"partial","text":"Let me look that up... ","session_id":"cli-sess-…"}
{"type":"error","message":"Upstream API rate limited. Try again in 60s.","session_id":"cli-sess-…"}
```

**Multi-attachment turn (bridge writes to stdin):**

```
{"type":"turn","message":"compare these two","session_id":"",
 "attachments":[{"kind":"image","url":"https://.../a.png"},{"kind":"image","url":"https://.../b.png"}],
 "permission":false,"protocol_version":"0.4"}
```

**Optional permission mid-turn** (profile `permission: true`; bridge keeps stdin open):

```
{"type":"partial","text":"I'll create that file.","session_id":"cli-sess-…"}
{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo ok > f.txt"},"session_id":"cli-sess-…"}
```

Bridge writes on stdin after the user approves in the messaging UI:

```
{"type":"permission_response","request_id":"1","behavior":"allow"}
```

Agent continues, then finishes:

```
{"type":"partial","text":"Done.","session_id":"cli-sess-…"}
{"type":"result","text":"","session_id":"cli-sess-…"}
```

---

## Optional tool permission

This section is **optional**. A conformant bridge or agent MAY ignore it entirely. Profiles default to `permission: false`. CLIs with no mid-turn approval channel (or bridges that only support unattended runs) keep using auto-approve flags such as `--dangerously-skip-permissions` or `--yolo` — that remains a valid and expected deployment mode.

### What this is (and is not)

- **Is:** a channel for **tool execution authorization** while a turn is in progress — the agent (or a wrapped CLI) needs allow/deny before running Bash, Write, etc.
- **Is not:** a general human-in-the-loop Q&A protocol. Clarifying questions belong in the normal `result` / `partial` content; the user answers on the **next** IM turn. Disabling interactive questionnaire tools (e.g. Claude Code's `AskUserQuestion`) in headless IM bridges is therefore reasonable and recommended.

### Enabling

When the profile sets `permission: true`:

1. The bridge MUST set `permission: true` in the [turn object](#input--stdin-turn-object).
2. The bridge MUST keep the agent's stdin open until the process exits or the bridge times out (see [stdin / EOF Contract](#stdin--eof-contract)).
3. The bridge MUST recognise `{"type":"permission_request"}` events and MUST write matching `{"type":"permission_response"}` objects to stdin.
4. Agents that wrap a CLI with a native control protocol (e.g. Claude Code `--permission-prompt-tool stdio` emitting `control_request` / accepting `control_response`) translate between that protocol and these frames inside the agent process. AgentProc does not require every underlying CLI to speak `control_request`; only agents that opt in need a translation layer.

When `permission` is absent or `false`, bridges MUST NOT require agents to speak these frames, and MUST NOT leave stdin open solely for permission traffic.

### `{"type":"permission_request"}` — agent → bridge (stdout)

```json
{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo ok"},"description":"Write a file","session_id":"cli-sess-…"}
```

Required fields:

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Literal `"permission_request"`. |
| `request_id` | string | Opaque id for this request. MUST be unique within the turn. The matching response MUST echo the same id. MUST NOT contain whitespace, control characters, or newlines. |
| `tool_name` | string | Tool or action name (e.g. `Bash`, `Write`). |
| `input` | object | Tool arguments as a JSON object (MAY be empty `{}`). |

Optional fields the agent MAY include for UI / policy:

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Short human-readable summary for the messaging UI. |
| `tool_use_id` | string | Underlying CLI / model tool-use id, if any. |
| `session_id` | string | Per [`session_id` on events](#session_id-on-events). SHOULD be included once known; MUST NOT delay the request to wait for it. |

On a malformed event (invalid JSON or missing required fields), the bridge SHOULD log a warning to stderr and MUST NOT block the turn waiting for a user decision; it SHOULD write a deny response only if it can still parse a `request_id`, otherwise ignore the event.

The request event is consumed by the bridge and does **not** appear in the user-visible reply body. Bridges typically render it as an approval prompt (buttons, reply keyboard, etc.) on the messaging platform.

### `{"type":"permission_response"}` — bridge → agent (stdin)

```json
{"type":"permission_response","request_id":"1","behavior":"allow"}
```

Written by the bridge to the agent's **stdin** as one NDJSON line, then the bridge continues waiting for more requests or process exit. Required fields:

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Literal `"permission_response"`. |
| `request_id` | string | MUST match the pending request. |
| `behavior` | string | `"allow"` or `"deny"`. |

Optional:

| Field | Type | Description |
|-------|------|-------------|
| `updated_input` | object | When `behavior` is `"allow"`, the input the agent SHOULD use, when the bridge wants to override or explicitly confirm the request's `input`. Agents wrapping CLIs that require an updated input blob (e.g. Claude Code `updatedInput`) MUST pass this through. When the response omits `updated_input` on allow, the agent MUST fall back to the request's original `input` — the bridge does NOT auto-fill it. Bridges MUST NOT pre-fill `updated_input` with the request's original `input` when the upstream approver did not provide one: doing so erases the distinction between "user explicitly accepted unchanged" and "user never touched it" for downstream CLIs. |
| `message` | string | When `behavior` is `"deny"`, a reason the agent MAY surface to the model or user. |

### Ordering, blocking, and timeout

- The agent MAY emit multiple permission requests in one turn (sequentially or with other events interleaved). Each outstanding `request_id` needs its own response.
- After emitting a `permission_request`, the agent (or wrapped CLI) typically **blocks** that tool call until a matching response arrives. The AgentProc bridge MUST NOT close stdin while a request is unanswered, except on turn timeout / process death.
- **`timeout_secs` still applies to the whole turn.** If the user never approves in the messaging UI, the bridge's normal timeout fires (SIGTERM → grace → SIGKILL). Bridges SHOULD, when timing out with a pending permission request, prefer a deny response with a timeout `message` if stdin is still writable, then proceed with the normal kill sequence — but MUST NOT hang past `timeout_secs` waiting for the user.
- Bridges MAY impose a shorter permission-specific wait; if they do, they MUST deny (or kill) rather than leave the agent blocked indefinitely.

### Interaction with other events

- `partial` / `result` MAY appear before and after permission traffic in the same turn.
- `error` still fails the turn. A pending permission request becomes moot; the bridge SHOULD stop waiting for user approval.
- When the turn has a known `session_id`, `permission_request` events SHOULD include it (see [`session_id` on events](#session_id-on-events)). Agents MUST NOT delay emitting `permission_request` to wait for session discovery.

### Relationship to auto-approve modes

If the underlying CLI has no stdio permission prompt (or the profile leaves `permission: false`), the supported options remain:

- CLI auto-approve / skip-permissions / yolo flags
- Pre-allow specific tools via CLI flags or config
- Sandbox the agent process and accept full auto-approve inside the sandbox

Optional permission does not replace those modes; it is an alternative when both the agent and the bridge opt in.

---

## stdin / EOF Contract

- When `permission` is absent or `false`, the bridge writes exactly one line — the [turn object](#input--stdin-turn-object) — followed by EOF. The agent reads one line, decodes it, and MUST NOT block on stdin thereafter.
- When `permission: true`, the bridge writes the turn line **without** EOF; then writes zero or more `{"type":"permission_response"}` lines; and closes stdin only when the process exits or the bridge ends the turn (timeout / kill).

The agent MUST NOT block on stdin after reading the turn line when `permission` is not `true`.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — stdout content (assembled from `result` and forwarded `partial` events) is sent as the reply |
| `1` | Generic agent error |
| `124` | Timeout (bridge-imposed; matches GNU `timeout` convention) |
| `130` | Interrupted by SIGINT (Ctrl-C) |
| `143` | Terminated by SIGTERM |

Other non-zero codes are treated as generic errors. Bridges SHOULD send a generic error message to the user when the process exits non-zero without having emitted an `error` event.

### Precedence when multiple failure signals arrive

A turn may produce more than one failure signal — for example, the agent emits `error` and then the bridge kills it on timeout before it exits, or the agent exits non-zero after emitting `error`. The bridge resolves the final exit code by this precedence (highest first):

1. **Timeout (124)** — the bridge killed the process. A timeout is always reported as `124` regardless of what the agent emitted before the kill.
2. **`error` event (1)** — the agent emitted an error event. Reported as `1` even if the process then exited 0.
3. **Process exit code** — whatever the process returned, used when neither of the above apply.

Rationale: a timeout is a bridge-level failure mode that the agent cannot recover from, so it takes precedence. `error` is the agent's own signal that something went wrong, which takes precedence over the raw exit code (because the agent may exit 0 after emitting `error` for self-diagnostic reasons).

### Bridge deployment hints

The following behaviors are bridge-side deployment decisions. They are **not** profile YAML fields read by the reference SDK runners; callers receive raw `RunResult.error` / `RunResult.exitCode` and `onStderr` data and decide how to surface them.

- **Error reply to user** (SHOULD): when a turn fails (non-zero exit or `error` event), the bridge SHOULD forward an error message to the user so they know the turn failed rather than receiving silence.
- **Stderr visibility** (default hidden): stderr is captured as an internal debug log. Bridges MAY surface it to the user for developer profiles or debugging contexts; it SHOULD be hidden in production deployments.

---

## Timeout Handling

When `timeout_secs` is reached without the process exiting:

1. Bridge sends `SIGTERM` to the process.
2. Bridge waits `kill_grace_secs` (default 5) for the process to exit.
3. If still running, bridge sends `SIGKILL`.

Any `partial` events already received are forwarded to the user. The bridge SHOULD send a timeout error reply to the user.

The agent SHOULD handle `SIGTERM` by flushing any buffered partial output and exiting promptly.

**Windows caveat.** `SIGTERM` and `SIGKILL` do not exist as deliverable signals on Windows. A bridge running on Windows MUST still honour the two-step intent — first a "polite" termination request (on Windows, `TerminateProcess` is the only available lever, so the grace period collapses to zero) and then, if the process is still alive after `kill_grace_secs`, a hard termination. POSIX bridges implement the full SIGTERM → grace → SIGKILL sequence. Agents that need to flush on shutdown cannot rely on receiving a signal on Windows and SHOULD use `atexit`-style hooks or explicit flush-before-exit discipline instead.

---

## Design Principles

**1. Process boundary is the only contract.**
The bridge does not care what language the agent is written in, what AI model it calls, or how it manages state. Any process that reads a turn from stdin and writes NDJSON events to stdout is a valid agent.

**2. No bridge logic in the agent.**
The agent process should not need to know anything about the messaging platform. It reads a turn, does something, writes events. Platform-specific concerns (delivery, rate limiting, session storage) are the bridge's responsibility.

**3. Session IDs are opaque.**
The bridge stores and forwards session IDs but never interprets them. The agent process owns the meaning of its session IDs.

**4. The unit of work is one turn.**
Each user message spawns one process. The agent is not expected to be a long-running daemon. (Long-running daemons are out of scope; see "Comparison with related protocols" below.)

**5. `type:` shortcuts are not part of this protocol.**
Built-in shortcuts (e.g. `type: claude-code`) are platform extensions, not P0. Implementations may offer them, but they are out of scope for this specification.

---

## Design Rationale

**Why NDJSON on stdout, rather than sentinel-prefixed lines?**

0.2 used sentinel-prefixed plain text (`AGENT_PARTIAL:...`) so that a hand-written bash agent (`echo "You said: $AGENT_MESSAGE"`) was a valid agent. The cost was threefold: (a) a collision rule — reply body had to avoid lines starting with `AGENT_*:`; (b) an encoding asymmetry — `AGENT_PARTIAL:` carried a JSON-encoded string while the final body was plain text, so the same logical "chunk of text" was encoded two different ways; (c) the `partial` payload was a bare string, leaving no room for metadata (e.g. distinguishing thinking from output).

0.3 makes every stdout line a JSON object with a `type` field. This removes the collision rule (body is now `{"type":"result"}` in 0.4; was `{"type":"text"}` in 0.3), unifies encoding (`partial` and the terminal body event both carry a `text` string field), and lets `partial` grow fields like `role`. The cost is that a bare `echo "hello"` is no longer a valid agent — it must emit a JSON event. Real agents are wrapper scripts (every hub profile is a Python or Node wrapper around an underlying CLI that already emits NDJSON internally), so they were already doing JSON; the only thing lost is the 5-minute bash smoke test, which a 3-line Python/Node script replaces.

**Why a stdin turn object for input, rather than environment variables?**

0.2 put the per-turn request in env vars (`AGENT_MESSAGE`, `AGENT_SESSION_ID`, ...). Every input field was an env var, and secrets were also env vars — so the input channel and the secret channel were the same channel. That conflation had a hard limit: env vars cannot carry structure, so multi-attachment (`AGENT_ATTACHMENTS`) was drafted and then removed because "JSON-in-env broke the bash echo agent promise". The protocol's capability ceiling was set by the bash echo agent.

0.3 separates the three input paths by purpose:

- **stdin** — the dynamic per-turn request (the turn object). Carries arbitrary structure: `attachments` arrays, nested fields, anything JSON can express.
- **argv** — launch params via `{{SESSION_ID}}` / `{{PROFILE_DIR}}`. The message is intentionally excluded from argv: argv is visible in `ps(1)`, has OS-level length limits, and puts user input in a location that is not stdin — making it harder to reason about trust boundaries.
- **env** — secrets and configuration (the profile `env` block), kept in env deliberately so they are not logged as part of the turn payload.

Debuggability is barely changed: `AGENT_MESSAGE="hello" ./agent.sh` becomes `echo '{"type":"turn","message":"hello","session_id":"","protocol_version":"0.4"}' | ./agent` — still a one-liner, just no longer an env-var assignment.

**Why drop `env_inherit`?**

0.2 added `env_inherit: minimal|all` so the secure-by-default (`minimal`) could be escaped with `all` for legacy profiles that relied on ambient shell variables. In practice the escape hatch kept the trust boundary fuzzy. 0.3 fixes the child base env at the infra set always; ambient variables a profile needs must be declared in the `env` block. This makes "what the agent sees" equal to "what the profile declares" plus the fixed infra set — a cleaner boundary. Profiles that set `env_inherit: all` in 0.2 must declare the variables they relied on.

**Why `session_id` as a field on events, instead of a `session` event?**

0.3 inherited a dedicated `{"type":"session"}` event (and a “last wins” rule) from the 0.2 `AGENT_SESSION:` line prefix. That shape treated session continuity as a discrete event, which is the wrong abstraction: the id is turn-level metadata. Coding CLIs already attach `session_id` / `sessionID` / `thread_id` to their own NDJSON events (often from the first `init` / `thread.started`); hub bridges were inventing a second event solely to re-declare it.

0.4 puts `session_id` on the events themselves. The bridge persists the **first** non-empty value; agents SHOULD attach it to subsequent events once known. Early events MAY omit it so streaming and mid-turn `permission_request` are not delayed. A different non-empty value later is a protocol violation (fail-soft: keep the first). Stateless agents omit the field entirely; they MUST NOT mint an id the underlying tool cannot resume with (generating an id a CLI requires as `--session` input is fine). When `turn.session_id` is already non-empty, agents SHOULD stamp it from the first event — no discovery wait.

**Why `result` instead of `text`?**

`text` sounded like “another body fragment” and invited multiple concatenating lines, which made turn-level metadata (`session_id`, `usage`) ambiguous. `result` is the terminal success outcome — at most one per turn — matching the `result`-shaped events many CLIs already emit, and giving a natural home for optional `usage`.

**Why an `error` event in addition to non-zero exit codes?**

Exit codes tell the bridge *that* something went wrong, but not *what* to tell the user. The `error` event lets the agent forward a meaningful, user-readable error message (e.g., "API key expired", "rate limited; retry in 60s") instead of the bridge's generic template.

**Why optional permission frames instead of general HIL?**

Messaging bridges already give the user a next turn. Clarifying questions belong in the reply body. What headless coding CLIs uniquely need mid-turn is **tool authorization** (allow this Bash/Write before it runs). That maps to Claude Code's `control_request` / `control_response` over stdio — not to AskUserQuestion. Making the channel opt-in (`permission: true`) keeps auto-approve / `--dangerously-skip-permissions` / `--yolo` valid for CLIs or deployments with no approval prompt.

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

The overlap is only superficial. An ACP client must implement file-system, terminal, and permission callbacks because the IDE owns the files the user is editing; an AgentProc bridge owns no user files and renders no diffs. Conversely, ACP offers no unattended-runtime semantics — no timeout, no `SIGTERM`/`SIGKILL` grace, no "tell the user when the agent errored" contract — because an IDE user stops a runaway agent by hand. A messaging bridge runs unattended, so those are load-bearing for AgentProc and out of scope for ACP. Even when the underlying CLI happens to be ACP-compatible (e.g. Claude Code driven over ACP by Zed), building an IM bridge on top of an ACP client is over-engineering: the bridge would implement capabilities it never uses and still miss the timeout/error-reply guarantees the chat scenario requires. AgentProc's contract — a turn object on stdin, NDJSON events on stdout, one process per turn — is the smallest one that fits the bridge-to-CLI job.

- Spec: https://agentclientprotocol.com/

### NDJSON / JSON Lines

NDJSON is one JSON object per line, newline-delimited. It's the wire format used internally by Claude Code, Codex, Gemini CLI streaming modes, and by MCP.

**Relationship to AgentProc:** **Same wire format as of 0.3 (refined in 0.4).** AgentProc is NDJSON in both directions: one JSON object per line on stdin (the turn, then optional permission responses) and on stdout (events). The difference from raw NDJSON is the fixed, small event vocabulary (`turn` / `partial` / `result` / `error` / `permission_request` / `permission_response` in 0.4) and the one-turn-per-process lifecycle, rather than a long-lived bidirectional RPC stream.

- Spec: https://jsonlines.org/

### SSE — Server-Sent Events (WHATWG)

SSE streams `event:` / `data:` lines over HTTP.

**Relationship to AgentProc:** **Semantic ancestor of `partial`.** The pattern of "newline-terminated events with a type discriminator" is borrowed from SSE, minus the HTTP transport and with a fixed field set. 0.3's `{"type":"partial","text":"..."}` is the JSON-object form of the same idea.

- Spec: https://html.spec.whatwg.org/multipage/server-sent-events.html

### LSP / DAP — Language Server / Debug Adapter Protocols (Microsoft)

LSP and DAP connect an editor to a language server or debugger. Transport: JSON-RPC 2.0 over stdio with `Content-Length: N` framing.

**Relationship to AgentProc:** **Framing contrast.** LSP uses byte-length-prefixed framing (allows binary payloads, requires a parser). AgentProc uses newline-delimited framing (text only, trivial to parse). The trade-off is deliberate.

- Specs: https://microsoft.github.io/language-server-protocol/ / https://microsoft.github.io/debug-adapter-protocol/

### Unix filter convention

The POSIX-derived convention of "read from stdin, write to stdout, exit 0 on success" — formalized in Eric Raymond's *The Art of Unix Programming*.

**Relationship to AgentProc:** **Philosophical foundation.** AgentProc extends the Unix filter convention with two things filters don't have: session-continuity handoff (`session_id` on events) and streaming events (`{"type":"partial"}`). Everything else is ordinary Unix. 0.3 onwards makes the filter "JSON lines in, JSON lines out" rather than raw text, but the shape is still a filter.

- Reference: http://www.catb.org/~esr/writings/taoup/html/ch01s06.html

### What AgentProc is *not*

- **Not a bot framework.** Hubot, Errbot, BotKit, and Microsoft Bot Framework operate on the *consumer* side of the bridge (in-process adapters, HTTP connectors). AgentProc defines the contract *between* the bridge and the agent, and is orthogonal to those frameworks.
- **Not an agent-to-agent protocol.** A2A / AGNTCY solve a different problem (agents talking to each other).
- **Not an IDE protocol.** Use ACP for that.
- **Not a tool protocol.** Use MCP for that.

---

## Migration from 0.3

Wire 0.4 is a **hard cutover** (same posture as 0.2 → 0.3). There is no dual-read period.

| 0.3 | 0.4 |
|-----|-----|
| `{"type":"session","id":"..."}` | Remove. Put `session_id` on `partial` / `result` / `error` / `permission_request` as appropriate. |
| `{"type":"text","text":"..."}` (may repeat; concatenate) | Single `{"type":"result","text":"..."}`. Extra body chunks → `partial`. |
| Session “last wins” | Persist **first** non-empty `session_id`; later different value = violation (keep first). |
| `protocol_version`: `"0.3"` | `"0.4"` — bump only when stdout vocabulary matches 0.4. |

Bridges that still emit or expect `type:session` / `type:text` are not 0.4-conformant. Under a 0.4 bridge, those lines are unknown `type` values and are handled per [Malformed lines](#malformed-lines) (warn + ignore) — they do **not** contribute session id or reply body. Opaque-version fail-soft (“unrecognised version → behave as unset”) does **not** mean a 0.3 agent remains usable under a 0.4 bridge.

Hub wrappers that previously read `session_id` only from a CLI’s terminal `result` event SHOULD also read it from the earliest CLI event that carries it (e.g. `system/init`) so streaming need not wait until process exit. They MUST NOT buffer an entire turn’s `partial`s solely to satisfy session tagging, and MUST NOT buffer `permission_request` while waiting for an id.

---

## Changelog

Document revisions are tracked here. Wire-protocol bumps are called out explicitly; other entries are editorial unless noted.

- **wire 0.4 / doc 1.1** — Breaking stdout shape. Removes `{"type":"session"}` and `{"type":"text"}`. Session continuity is an optional `session_id` field on stdout events: bridge persists the first non-empty value; agents SHOULD attach it once known; early omit is allowed; a conflicting later value is a violation (keep first). Never mint an id the tool cannot resume with; never use `""` on output. Final success body is a single `{"type":"result","text":...}` (optional `usage`). Streaming body assembly: forwarded `partial`s win over a duplicate `result.text`. Hard cutover from 0.3 (see [Migration from 0.3](#migration-from-03)). Rationale for 0.3’s “last session event wins” is retired.
- **wire 0.3 / doc 1.0** — NDJSON on both directions. Input: a single [turn object](#input--stdin-turn-object) on stdin replaces all `AGENT_*` environment variables; secrets/config stay in env; argv placeholders unchanged. Output: stdout is now NDJSON events (`partial` / `text` / `session` / `error` / `permission_request`) distinguished by a `type` field, replacing the `AGENT_*:` sentinel prefixes. `partial` gains an optional `role` (`output` | `thinking`). Attachments collapse to a single `attachments` array in the turn object (each element `{kind, url, ...}`), replacing the 0.2 `AGENT_IMAGE_URL` / `AGENT_FILE_URL` single-attachment convenience vars — there is no longer a dual single/multi representation. Session id is now an arbitrary JSON string on the wire (charset restriction moved to a storage-level concern). Profile changes: `command` is always argv[0] and never split (the `args`-absent whitespace-split shorthand is removed; `args` defaults to `[]`); the `stdin` field is removed (stdin always carries the turn); `streaming` becomes a bridge-side hint rather than a wire field; `env_inherit` is removed (child base env is always the infra set). Malformed stdout lines are logged and ignored rather than treated as reply body. The event vocabulary is declared closed to resist drift toward ACP-style richer events. This is a hard cutover from 0.2; the runner does not support both.
- **wire 0.2 / doc 0.9** — Secure-by-default child environment inheritance. New profile field `env_inherit: minimal|all` (default `minimal`). Inheritance is decoupled from `env_allowlist`: the allowlist only gates `${VAR}` expansion; full `process.env` / `os.environ` inheritance requires explicit `env_inherit: all`. SDK packages bumped to 0.6.1; wire protocol stays `0.2`.
- **wire 0.2 / doc 0.8** — Optional tool permission channel: profile `permission: true`, env `AGENT_PERMISSION`, stdout `AGENT_PERMISSION_REQUEST:<json>`, stdin `AGENT_PERMISSION_RESPONSE:<json>`, and the keep-stdin-open turn rule. Opt-in only; default profiles and auto-approve / skip-permissions deployments are unchanged. Wire protocol string becomes `0.2` because new protocol line prefixes and mid-turn stdin frames are on the wire.
- **doc 0.7** — `env_allowlist` is now a real trust boundary, not a cosmetic `${VAR}` filter. When `env_allowlist` is present, the agent process no longer inherits the bridge's full environment; its env is built from a minimal infra set (`PATH`/`HOME`/`TERM`/…, enumerated in the spec) + the profile `env` block + `AGENT_*` + CLI `--env` extras. Previously the child inherited the bridge env wholesale, so any secret the bridge held leaked to the agent regardless of the allowlist — contradicting the "shrinking the trust boundary" claim. The `${VAR}`-blocking and warning behaviour is unchanged. Absent `env_allowlist` keeps the back-compat full-inheritance behaviour. SDK version bumped to 0.5.2 (both Python and Node); wire protocol stays `0.1`. Cross-implementation conformance coverage extended to the SDK entry points (`create_profile` / `createProfile`) via a new `spec/conformance/sdk.json` fixture: both SDKs now run the same return-type / `send_partial` / `send_error` / `ProtocolError` scenarios as subprocesses and assert identical stdout + exit codes.
- **doc 0.5** — Defined empty-`AGENT_MESSAGE` semantics (legal when attachments are present). Disambiguated `command`/`args`: `args: []` (explicit empty) now means "do not split", distinct from `args` absent. Added `${VAR}` security warning for profile `env` blocks. Added optional `env_allowlist` profile field: when present, `${VAR}` references not in the list expand to empty + a stderr warning, shrinking the trust boundary from the full environment to the declared variables. Codified `AGENT_ERROR:` interaction with already-delivered partials (not retracted), and that the bridge MAY stop reading stdout after the error. Restated the session-id format constraint (no whitespace/control/colon) and defined bridge behaviour on violation (ignore the line, preserve previous id, warn). Codified exit-code precedence (timeout > `AGENT_ERROR:` > exit code). Documented SDK `send_error` terminality. Removed the unused `session_line_prefix` profile field — bridges hardcode `AGENT_SESSION:` and the field was never read.
- **doc 0.4** — Split wire-protocol version (`0.1`) from document revision in the header; added a Versioning section codifying that `AGENT_PROTOCOL_VERSION` is an opaque, non-comparable string. Promoted `AGENT_ATTACHMENTS` from Draft to P0 with a consistency requirement when bridges set it alongside the single-attachment vars. Clarified session-line ordering: when a CLI emits `AGENT_SESSION:` together with `AGENT_ERROR:` (the common `result{is_error}` shape), bridges MUST preserve the session id for the next turn even though the current turn is reported as a failure. Added `AGENT_ERROR:` → bridge MUST treat the turn as failed regardless of exit code. Defined `command` as argv[0] and `args` as the remaining argv, with a quoting rule so paths containing whitespace remain expressible without a shell. Noted Windows caveat for the timeout SIGTERM/SIGKILL contract.
- **0.1.0** — Initial public draft. Defined env-var input, sentinel-prefixed stdout, `AGENT_SESSION:` / `AGENT_PARTIAL:` / `AGENT_ERROR:`, session-line "last wins" rule, `AGENT_PROTOCOL_VERSION`, `AGENT_ATTACHMENTS` (draft), timeout/SIGTERM contract, exit-code conventions, stdin EOF contract, command-execution no-shell rule.
