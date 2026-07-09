# Hub CLI tool-authorization survey

Status of mid-turn tool approval for AgentProc Hub profiles (as of 2026-07).
AgentProc optional permission (`permission: true` / `AGENT_PERMISSION_*`) is
only useful when the underlying CLI can emit an approval request on stdout and
accept a decision on stdin **without** a TTY.

| Profile | CLI | Unattended flag today | Mid-turn stdio approval? | Notes |
|---------|-----|----------------------|---------------------------|-------|
| **claude-code** | `claude` | `--dangerously-skip-permissions` | **Yes** | `--permission-prompt-tool stdio` + bidirectional `stream-json`. Emits `control_request` / `can_use_tool`; accepts `control_response`. Hub bridge translates when `permission: true`. |
| **codebuddy** | `codebuddy` | `--dangerously-skip-permissions` | **Likely** (claude-compatible) | Same stream-json shape as Claude; not verified end-to-end for `permission-prompt-tool`. Candidate for a follow-up once Claude path is stable. |
| **codex** | `codex` | (no skip in hub; relies on policy) | **Partial / different protocol** | Has `--ask-for-approval` and JSONL events (`exec_approval`, `apply_patch_approval`, …). Not the same as Claude `control_request`. Needs a dedicated translator — not wired yet. Hub currently does not pass `--dangerously-bypass-approvals-and-sandbox`. |
| **gemini-cli** | `gemini` | `--yolo` | **No (known)** | `--approval-mode` is `default` / `auto_edit` / `yolo`. No documented stdio approval handshake for headless `stream-json`. Keep `--yolo` for unattended IM. |
| **cursor** | `agent` | (profile-specific) | **Unknown** | Not surveyed in depth; treat as auto-approve until a stdio protocol is documented. |
| **qwen-code / opencode / aider / kimi / deepseek / …** | various | yolo / yes-always / exec | **No or N/A** | One-shot / TUI / no mid-turn stdio approval suitable for AgentProc. Stay on auto-approve. |
| **recursive** | `recursive` | `--permission-mode auto` | External hooks only | Can set `RECURSIVE_PERMISSION_MODE=default` for external hooks — not AgentProc frames. |
| **agy / echo-agent** | — | skip / n/a | No | |

## Recommendation

1. Ship **claude-code** `permission: true` first (done in this branch).
2. Next candidate: **codebuddy** (copy Claude translator if flag parity holds).
3. **codex** needs its own event map (`exec_approval` ↔ `AGENT_PERMISSION_*`) — separate PR.
4. Everyone else: keep auto-approve / yolo; document the trade-off.
