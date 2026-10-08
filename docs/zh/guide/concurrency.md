# 并发

bridge 是长驻进程：用户什么时候说话就什么时候到，两个用户（或同一个人连击）在同一瞬间发消息是常态。协议是「一 turn 一进程」，因此默认每条 turn 都会 fork 一个新 agent 进程——洪峰直接进程扇出，直到内存耗尽。

runner 为此提供两个可选启用的原语。两者都是 runner 侧编排：**不新增线上事件**，CLI 不受影响。规范性的规则见规范的[并发](/zh/spec/)章节。

## Per-session 串行化

给 turn 一个会话 key，同 key 的 turn 就**逐条执行**，按到达顺序排队。它修的是最糟的故障模式：两个 agent 进程对同一 key 并发 `--resume`。

```python
# Python — agentproc.runner
from agentproc.runner import run, RunOptions

run(profile, RunOptions(message=text, session_key=ctx.session_id))
```

```js
// Node.js — agentproc/src/runner
const { run } = require('agentproc/src/runner');

await run(profile, { message: text, sessionKey: ctx.sessionId });
```

```rust
// Rust
run(&profile, RunOptions::new(text).with_session_key(&ctx.session_id)).await?;
```

用你本来要传给 `session_id` 的那个 id 即可：它就是「不能被并发 resume 两次」的身份。不带 key 的 turn 不受影响——只有显式提供 key 时才串行。

同一个 key 也保护会话历史文件。handler 调 `append_history` / `appendHistory` 写 `~/.agentproc/sessions/<id>.jsonl` 时自身没有任何文件锁，同一会话的两条 turn 同时在跑就可能写交错——按 session id 给 key 正是让这些追加保持有序的手段。

## 全局并发上限

`max_concurrent` / `maxConcurrent` 限制单 runner 实例同时运行的 agent 进程数（默认无限制）。达到上限后的洪峰行为由你显式选择：

| `on_saturated` / `onSaturated` | 行为 |
|--------------------------------|-----------|
| `"queue"`（默认） | 超限 turn 按 FIFO 等待空位，随后正常执行。 |
| `"reject"` | 该 turn 立即以含固定标记 `agentproc: concurrency limit` 的 `error` 终止——绝不启动进程。 |

```python
run(profile, RunOptions(
    message=text,
    session_key=session_id,
    max_concurrent=4,
    on_saturated="reject",   # 想吸收洪峰而非丢弃就改成 "queue"
))
```

宁可延迟也别丢弃的聊天流量用 queue；调用方能重试（或 bridge 想直接回「慢一点」）时用 reject——宁可削峰，也不让队列继续长。

拒绝是终态结果，不是异常：

| SDK | 拒绝的呈现方式 |
|-----|-----------------------|
| Python | `RunResult.error` 含固定标记；`on_error` 也会触发 |
| Node.js | `result.error` 含固定标记；`onError` 也会触发 |
| Rust | `RunResult.error` 含固定标记；`on_error` 也会触发 |

请匹配标记本身而非周边文案——三个 SDK 共用同一固定标记，且拒绝与其它 agent 失败走同一条 `error` 通道。

## 仍然由你决定的部分

SDK 只决定 turn **何时**运行；收到结果后做什么仍由 bridge 决定。被拒绝的 turn 就是一条普通的 `error` 结果：重试、告知用户、或直接丢弃。如果你要超越全局上限的 per-user 公平性，把策略留在 bridge 里，只把会话 key 和上限交给 runner。
