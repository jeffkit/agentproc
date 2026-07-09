# Hub CLI tool-authorization survey

Status of mid-turn tool approval for AgentProc Hub profiles (as of 2026-07).
AgentProc optional permission (`permission: true` / `AGENT_PERMISSION_*`) is
only useful when the underlying CLI can emit an approval request on stdout and
accept a decision on stdin **without** a TTY — or expose an equivalent
programmatic channel the hub bridge can translate.

| Profile | CLI | Unattended flag today | Mid-turn stdio approval? | Notes |
|---------|-----|----------------------|---------------------------|-------|
| **claude-code** | `claude` | `--dangerously-skip-permissions` | **Yes** | `--permission-prompt-tool stdio` + bidirectional `stream-json`. Emits `control_request` / `can_use_tool`; accepts `control_response`. Hub bridge translates when `permission: true`. |
| **codebuddy** | `codebuddy` | `--dangerously-skip-permissions` | **No** | Official headless docs mark `--permission-prompt-tool` as **unsupported**. Bridge **rejects** `permission: true` / `AGENT_PERMISSION=1` with `AGENT_ERROR` (no silent skip-permissions fallback). Use `claude-code` when mid-turn approval is required. |
| **codex** | `codex` | (no skip in hub; relies on policy) | **Yes (via hooks)** | `codex exec --json` has no stdin approval loop. With `permission: true`, the bridge injects a one-shot `CODEX_HOME` `PermissionRequest` hook that relays ↔ `AGENT_PERMISSION_*` over a Unix socket, and sets `approval_policy=on-request` + `--dangerously-bypass-hook-trust`. |
| **gemini-cli** | `gemini` | `--yolo` | **No (known)** | `--approval-mode` is `default` / `auto_edit` / `yolo`. No documented stdio approval handshake for headless `stream-json`. Keep `--yolo` for unattended IM. |
| **cursor** | `agent` | (profile-specific) | **Unknown** | Not surveyed in depth; treat as auto-approve until a stdio protocol is documented. |
| **qwen-code / opencode / aider / kimi / deepseek / …** | various | yolo / yes-always / exec | **No or N/A** | One-shot / TUI / no mid-turn stdio approval suitable for AgentProc. Stay on auto-approve. |
| **recursive** | `recursive` | `--permission-mode auto` | External hooks only | Can set `RECURSIVE_PERMISSION_MODE=default` for external hooks — not AgentProc frames. |
| **agy / echo-agent** | — | skip / n/a | No | |

## Recommendation

1. Ship **claude-code** `permission: true` first (done).
2. **codex** uses Codex `PermissionRequest` hooks as the translation layer (done on this branch).
3. **codebuddy**: keep auto-approve; fail closed if someone enables `permission: true` until Tencent ships `permission-prompt-tool`.
4. Everyone else: keep auto-approve / yolo; document the trade-off.
