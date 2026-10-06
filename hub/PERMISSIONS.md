# Hub CLI tool-authorization survey

Status of mid-turn tool approval for AgentProc Hub profiles (as of 2026-07).
AgentProc optional permission (`permission: true` / `permission*`) is
only useful when the underlying CLI can emit an approval request on stdout and
accept a decision on stdin **without** a TTY — or expose an equivalent
programmatic channel the hub bridge can translate.

| Profile | CLI | Unattended flag today | Mid-turn stdio approval? | Notes |
|---------|-----|----------------------|---------------------------|-------|
| **claude-code** | `claude` | `--dangerously-skip-permissions` | **Yes** | `--permission-prompt-tool stdio` + bidirectional `stream-json`. Emits `control_request` / `can_use_tool`; accepts `control_response`. Hub bridge translates when `permission: true`. The SDK executors build the same approval argv, but their frame loop is **not wired yet** — see "SDK executor posture". |
| **codebuddy** | `codebuddy` | `--dangerously-skip-permissions` | **No** | Official headless docs mark `--permission-prompt-tool` as **unsupported**. Bridge **rejects** `permission: true` with a `{"type":"error"}` event (no silent skip-permissions fallback). Use `claude-code` when mid-turn approval is required. |
| **codex** | `codex` | (no skip in hub; relies on policy) | **Yes (via hooks)** | `codex exec --json` has no stdin approval loop. With `permission: true`, the bridge injects a one-shot `CODEX_HOME` `PermissionRequest` hook that relays ↔ `permission*` over a Unix socket, and sets `approval_policy=on-request` + `--dangerously-bypass-hook-trust`. |
| **gemini-cli** | `gemini` | `--yolo` | **No (known)** | `--approval-mode` is `default` / `auto_edit` / `yolo`. No documented stdio approval handshake for headless `stream-json`. Keep `--yolo` for unattended IM. |
| **grok-build** | `grok` | `--always-approve` | **No (known)** | Headless has `--permission-mode` / `--allow` / `--deny` / hooks, but no documented AgentProc-compatible stdin approval loop yet. Keep `--always-approve` for unattended IM. |
| **cursor** | `agent` | (profile-specific) | **Unknown** | Not surveyed in depth; treat as auto-approve until a stdio protocol is documented. |
| **qwen-code / opencode / aider / kimi / deepseek / …** | various | yolo / yes-always / exec | **No or N/A** | One-shot / TUI / no mid-turn stdio approval suitable for AgentProc. Stay on auto-approve. |
| **recursive** | `recursive` | `--permission-mode auto` | External hooks only | Can set `RECURSIVE_PERMISSION_MODE=default` for external hooks — not AgentProc frames. |
| **dsh** | `dsh` | bridge sets `DSH_PERMISSION_MODE=danger-full-access` | **No** | DeepSeek Harness headless has no stdio approval channel. dsh's default "ask" policy has no UI in headless; the hub bridge auto-sets `danger-full-access` for unattended runs — **unless** `AGENTPROC_AUTO_APPROVE=0`, which suppresses that injection and leaves the "ask" policy in place. Override via env to `read-only`/`workspace-write` to lean on dsh's sandbox. |
| **agy / echo-agent** | — | skip / n/a | No | |

## SDK executor posture

The in-process executor path (`executor: <name>` in a profile, no bridge
subprocess) is stricter than the hub bridges above. Each executor declares a
`supportsPermission` bit; **only `claude-code` sets it**. The runner applies
this matrix before spawning anything:

| Executor | `permission: true` | argv |
|---|---|---|
| `claude-code` | allowed | `--permission-prompt-tool stdio` + `--input-format stream-json` (no auto-approve flag) |
| every other executor (codebuddy, codex, cursor, gemini-cli, grok-build, kimi-code, opencode, qwen-code, agy, aider, deepseek, dsh, pi) | **refused** | none — the CLI is never spawned |

A refusal is an `error` event plus a non-zero exit code: no silent fallback to
`--dangerously-skip-permissions` / `--yolo`. A third-party executor that does
have an approval channel must declare `supportsPermission: true` (Python
registry key `supports_permission`), otherwise it is treated as channelless.

**Known limitation.** On the Python and Node executor paths the claude-code
approval argv is built, but the bidirectional frame loop (initial `stream-json`
user message on stdin, `control_request` → `control_response`) is not wired up
yet — do not read `permission: true` on those paths as a working approval loop.
The hub bridge and the Rust SDK are wired.

## Posture switch

Bridges and the SDK runners read `AGENTPROC_AUTO_APPROVE` from their **own**
process environment. Setting it to `0` or `false` (case-insensitive, surrounding
whitespace ignored) makes the runner refuse to spawn any CLI whose argv contains
one of these tokens:

`--dangerously-skip-permissions`, `--yolo`, `--always-approve`,
`--yes-always`, `--approve`, `--auto`

(`auto_approve_flags` in `spec/conformance/cases.json` is the single source of
truth; every SDK embeds the same list and the conformance drivers assert the two
are equal item for item.) The refusal is an `error` event plus a non-zero exit
code — never a silent downgrade.

- An approval argv produced for `permission: true` contains none of those
  tokens, so it is still allowed.
- Any other value, including an unset variable, keeps the current behaviour
  (auto-approve flags pass through).
- It reaches a hub bridge only via the profile `env:` block (add the
  pass-through *and* a matching `env_allowlist` entry) or the CLI `--env` flag —
  the runner's child env carries just the infra set + profile `env` + `--env`.
  `hub/dsh` additionally stops injecting `DSH_PERMISSION_MODE` under this
  switch.

## Recommendation

1. Ship **claude-code** `permission: true` first (done).
2. **codex** uses Codex `PermissionRequest` hooks as the translation layer (done on this branch).
3. **codebuddy**: keep auto-approve; fail closed if someone enables `permission: true` until Tencent ships `permission-prompt-tool`.
4. Everyone else: an executor with no approval channel must not declare
   `permission: true` — it hard-fails instead of silently auto-approving.
   Unattended auto-approve is now an explicit choice, not an unlabelled default:
   `--dangerously-skip-permissions` / `--yolo` remain the argv default, but
   `AGENTPROC_AUTO_APPROVE=0` turns any of them into a refusal.
