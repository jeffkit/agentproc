# Rust SDK

Rust SDK 是 AgentProc 的第三个官方 SDK，与 [Python](./python)、[Node](./node) 两个 SDK 保持可观测一致性。三者实现同一套 wire 协议（`0.4`）和同一套 in-process executor 机制。

与 Python / Node SDK 提供 `create_profile` handler API（用来写 agent 脚本）不同，Rust SDK **面向 host/bridge**：它提供 `run()` runner，让你从 Rust 应用（如 IM 桥接、CI bot）里以编程方式驱动 profile。它也内置了 in-process executor 注册表，让 Rust 宿主可以直接 spawn 目标 CLI，省掉 bridge 子进程。

[![crates.io](https://img.shields.io/crates/v/agentproc.svg)](https://crates.io/crates/agentproc)
[![docs.rs](https://docs.rs/agentproc/badge.svg)](https://docs.rs/agentproc)

## 安装

```toml
[dependencies]
agentproc = "0.11"
```

默认 feature 会带入内置 executor 注册表、YAML profile 解析和多模态附件下载：

```toml
[dependencies]
# 只保留最小 runner —— 不要 executors / YAML / 多模态拉取
agentproc = { version = "0.11", default-features = false }
```

可用 feature（默认全部开启）：

| Feature | 带入的内容 |
|---------|------------|
| `executors` | 内置 executor 注册表（`codex`、`claude-code`、`gemini-cli` 等） |
| `yaml` | `Profile::from_path` / serde_yaml 解析——关掉则只能以编程方式构造 `&Profile` |
| `multimodal` | 图片/PDF base64 下载，供需要向 CLI 喂 content block 的 executor 使用 |

## 快速上手 —— 驱动一个 profile

```rust
use agentproc::{run, Profile, RunOptions};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let profile = Profile::from_path("profile.yaml")?;
    let result = run(&profile, RunOptions::new("what is this codebase?")).await?;
    println!("{}", result.reply);
    println!("session: {}", result.session_id);
    Ok(())
}
```

## 流式

传入 `on_partial` 回调，在 profile 的 `streaming: true` 时实时转发分片：

```rust
use agentproc::{run, Profile, RunOptions};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let profile = Profile::from_path("profile.yaml")?;
    let opts = RunOptions::new("explain this codebase")
        .on_partial(|chunk| {
            print!("{chunk}");
            std::io::Result::<()>::Ok(())
        });
    let result = run(&profile, opts).await?;
    println!("\nsession: {}", result.session_id);
    Ok(())
}
```

## 多轮会话

把上一轮的 `result.session_id` 喂回下一轮：

```rust
use agentproc::{run, Profile, RunOptions};

async fn turn(profile: &Profile, message: &str, session_id: &str) -> anyhow::Result<String> {
    let opts = RunOptions::new(message).session_id(session_id);
    let result = run(profile, opts).await?;
    Ok(result.session_id)
}
```

runner 会持久化第一个非空 `session_id` 并在 `RunResult` 上暴露出来；把它传回来即可续接。

## In-process executors

在 profile 里设置 `executor:`，可跳过 bridge 子进程，让 runner 直接 spawn 目标 CLI：

```yaml
agentproc:
  executor: codex          # 装了 Rust SDK 的宿主走 in-process
  command: python3         # 没装 Rust executor 的宿主走 fallback
  args: ["{{PROFILE_DIR}}/bridge.py"]
  timeout_secs: 600
```

内置 executor 名（开启 `executors` feature 时）：

```rust
use agentproc::executors::executor_names;

println!("{:?}", executor_names());
// ["claude-code", "codebuddy", "codex", "cursor", "gemini-cli", "grok-build", ...]
```

运行时注册自己的 executor：[`agentproc::executors::register_executor`](https://docs.rs/agentproc/latest/agentproc/executors/fn.register_executor.html)。

## `RunResult` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `reply` | `String` | 组装好的回复正文（流式已全部转发时为空） |
| `session_id` | `String` | 任意事件里第一个有效 session id；无则 `""` |
| `error` | `String` | `{"type":"error"}` 事件的错误信息；无则 `""` |
| `exit_code` | `i32` | agent 进程退出码（124 = 超时） |
| `timed_out` | `bool` | 是否因超时被 kill |
| `usage` | `Option<Usage>` | 终止事件的 token/成本统计；无则 `None` |
| `duration_ms` | `u64` | 本次运行墙钟耗时 |

常见 `usage` 字段（均可选）：`input_tokens`、`output_tokens`、`total_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`、`reasoning_tokens`、`duration_ms`、`cost_usd`。

## 权限通道

对于支持 turn 内工具授权的 CLI（如 Claude Code），在 profile 上设 `permission: true` 并提供 `on_permission` 回调。runner 会 `await` 你的 `PermissionDecision`——`Allow`（可选带 `updated_input`）或 `Deny`（带原因）——再转发给 agent：

```rust
use agentproc::{run, Profile, RunOptions, PermissionDecision};

async fn decide(req: PermissionRequest) -> PermissionDecision {
    // 问人、记日志，或按策略自动放行
    PermissionDecision::allow()
}
```

## 本地测试

用和 hub 一样的路径驱动 profile：

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

或者像 bridge 那样把 turn 对象写到 stdin：

```bash
echo '{"type":"turn","message":"hello","session_id":"","protocol_version":"0.4"}' | cargo run --example run_profile
```

## API 参考

完整 API 文档发布在 [docs.rs/agentproc](https://docs.rs/agentproc)。runner 的权威实现位于 [`sdk/rust/src/runner.rs`](https://github.com/jeffkit/agentproc/blob/main/sdk/rust/src/runner.rs)。
