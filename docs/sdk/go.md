# Go SDK

The Go SDK is the fourth official AgentProc SDK, kept at observable parity with the [Python](./python), [Node](./node), and [Rust](./rust) SDKs — verified by the shared [conformance suite](https://github.com/jeffkit/agentproc/tree/main/spec/conformance), which the package's tests drive directly.

It covers both sides of the protocol: a host/bridge-oriented `Run` runner (like Rust) and a handler-based `Serve` entry point for writing agents (like Python/Node's `create_profile`), plus the built-in in-process executor registry.

## Install

```bash
go get github.com/jeffkit/agentproc/sdk/go
```

The only dependency is `gopkg.in/yaml.v3` (profile parsing).

## Run a profile (bridge side)

```go
result, err := agentproc.Run(context.Background(), map[string]any{
    "command":      "python3",
    "args":         []string{"{{PROFILE_DIR}}/bridge.py"},
    "env":          map[string]string{"MY_API_KEY": "${MY_API_KEY}"},
    "env_allowlist": []string{"MY_API_KEY"},
}, &agentproc.RunOptions{
    Message:    "hello",
    SessionID:  previousSessionID,
    ProfileDir: profileDir,
    OnPartial:  func(text string, role *string) { /* stream */ },
})
// result.Reply / result.SessionID / result.ExitCode / result.Usage
```

Implemented per the spec: the three-layer child env, `{{PROFILE_DIR}}` /
`{{SESSION_ID}}` placeholders, earliest-expiry time limits
(`timeout_secs` / `budget_secs` / `deadline`), the graded process-group kill
(SIGTERM → `kill_grace_secs` → SIGKILL), the bounded post-exit drain (a
surviving grandchild holding the pipe cannot hang the turn), exit-code
normalisation (`128 + signal`), error-event precedence, first-non-empty
session ids with wire validation, and the optional permission channel.
Cancelling the `context.Context` performs the cooperative cancel
(exit `125`, `TimedOut=false`).

## Write an agent (agent side)

```go
func main() {
    handler := func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
        ctx.SendPartial("thinking…", "thinking")
        return &agentproc.AgentResult{Response: "answer", SessionID: "s-1"}, nil
    }
    os.Exit(agentproc.Serve(handler))
}
```

Return `&agentproc.ProtocolError{...}` for a fatal, user-visible error; use
`ctx.SendError` for a non-terminal one. `ctx.SendPermissionRequest` /
`ctx.ReadPermissionResponse` drive the optional permission channel.

## In-process executors

The built-in registry mirrors the Python/Node one — `claude-code` (with the
`stdio` permission channel), `codex`, `cursor`, `gemini-cli`, `qwen-code`,
`codebuddy`, `opencode`, `kimi-code`, `grok-build`, `agy`, `aider`,
`deepseek`, `dsh`, `pi`. Register application-local executors with
`agentproc.RegisterExecutor(name, factory)`. The permission-posture rules
are identical: a `permission: true` profile against an executor without a
mid-turn approval channel is refused (error + non-zero exit, never a silent
`--dangerously-skip-permissions`), and `AGENTPROC_AUTO_APPROVE=0` refuses
any auto-approve argv.

## Scope notes

- Library-only (like the Rust SDK): no `agentproc` CLI, no hub fetch.
- The Python SDK's workspace run lock is not implemented; the process-group
  contract is identical.
