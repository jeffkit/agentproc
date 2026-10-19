# Go SDK

Go SDK 是 AgentProc 的第四个官方 SDK，与 [Python](./python)、[Node](./node)、[Rust](./rust) 三个 SDK 保持可观测一致性——由共享的 [conformance 套件](https://github.com/jeffkit/agentproc/tree/main/spec/conformance)驱动验证，包内测试直接消费同一批 fixture。

它覆盖协议两侧：既有面向 host/bridge 的 `Run` runner（同 Rust），也有写 agent 用的 `Serve` handler 入口（同 Python/Node 的 `create_profile`），并内置 in-process executor 注册表。

## 安装

```bash
go get github.com/jeffkit/agentproc/sdk/go
```

唯一依赖是 `gopkg.in/yaml.v3`（profile 解析）。

## 运行 profile（bridge 侧）

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
    OnPartial:  func(text string, role *string) { /* 流式 */ },
})
// result.Reply / result.SessionID / result.ExitCode / result.Usage
```

按规格实现：三层子进程环境、`{{PROFILE_DIR}}` / `{{SESSION_ID}}` 占位符、
`timeout_secs` / `budget_secs` / `deadline` 三者最早过期、进程组分级击杀
（SIGTERM → `kill_grace_secs` → SIGKILL）、子进程退出后的有界排水（存活的
孙进程持有管道不再挂死 turn）、退出码归一（`128 + 信号`）、error 事件
优先级、首个非空 session id（含 wire 校验）、可选权限通道。取消
`context.Context` 即协作式取消（退出码 `125`、`TimedOut=false`）。

## 编写 agent（agent 侧）

```go
func main() {
    handler := func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
        ctx.SendPartial("thinking…", "thinking")
        return &agentproc.AgentResult{Response: "answer", SessionID: "s-1"}, nil
    }
    os.Exit(agentproc.Serve(handler))
}
```

致命错误返回 `&agentproc.ProtocolError{...}`（发出 error 事件 + 非零退出）；
非致命用 `ctx.SendError`（handler 可继续返回 body，桥按规格丢弃）。
`ctx.SendPermissionRequest` / `ctx.ReadPermissionResponse` 驱动可选权限通道。

## In-process executors

内置注册表与 Python/Node 对齐——`claude-code`（带 `stdio` 权限通道）、
`codex`、`cursor`、`gemini-cli`、`qwen-code`、`codebuddy`、`opencode`、
`kimi-code`、`grok-build`、`agy`、`aider`、`deepseek`、`dsh`、`pi`。
应用自有 executor 用 `agentproc.RegisterExecutor(name, factory)` 注册。
权限姿态规则一致：`permission: true` 遇到无审批通道的 executor 直接拒绝
（error + 非零退出，绝不静默回退 `--dangerously-skip-permissions`）；
`AGENTPROC_AUTO_APPROVE=0` 拒绝任何 auto-approve argv。

## Workspace run lock

设置 `RunOptions.RunLockKey`（通常=workspace 绝对路径）即可记录在跑 agent
进程组的「遗言」；接管方开工前调 `CleanupStaleRun`——孤儿（身份核实通过且
spawner 已死）整组击杀后放行；宿主仍活着的健康 agent 返回 `RunLockBusy`
（绝不盲杀）。

## CLI

`go install github.com/jeffkit/agentproc/sdk/go/cmd/agentproc@latest` 构建
`agentproc` 二进制——`agentproc --profile <yaml> --prompt "hi"` 与
`agentproc hub list|show|install|run` 子命令，输出语义与退出码和 Python/Node
CLI 一致。

## 边界说明

- hub 差异：Go CLI 无内置 profile 副本——`hub` 命令走 jsDelivr CDN 并缓存
  24h（`AGENTPROC_HUB_REF` 钉住 ref、按 ref 失效），首次使用需要网络。
