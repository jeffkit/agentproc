# 协议规范速查

**线协议：** `0.4` · **文档修订：** `1.2` · **状态：** 稳定

完整规范维护在仓库的 [`spec/protocol.zh.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.zh.md)。本页是快速查阅版。

---

## Profile YAML

```yaml
command: python3                      # argv[0] — 始终单个 token，永不切分
args: ["{{PROFILE_DIR}}/my_agent.py"] # argv[1..]；省略时默认为 []
                                       # {{PROFILE_DIR}} = profile 所在目录

# cwd 可选。省略时默认为 bridge 进程的 cwd。
# 若为相对路径，相对 {{PROFILE_DIR}}（profile 所在目录）解析。
cwd: /path/to/workspace
env:
  MY_API_KEY: "${MY_API_KEY}"
env_allowlist: [MY_API_KEY]           # 可选：限制 ${VAR} 展开

timeout_secs: 600             # 每轮挂钟超时（秒），默认 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL 宽限期，默认 5

streaming: true               # 实时转发 {"type":"partial"} 事件

permission: false             # 可选工具授权（保持 stdin 打开；见完整规范）
```

占位符**不**经 shell 替换。argv 由两个字段构成：

- **`command`** — argv[0]。单个 token，**永不切分**，即使含空格。
- **`args`** — argv[1..] 的 YAML 列表。**省略时默认为 `[]`。**

最终 argv（`[command, *args]`）直接传给 `execve`，允许 `command` 携带含空格的路径：

```yaml
command: "/path with spaces/my agent"
args: []
```

（0.2 曾有「`args` 缺省 + `command` 含空格 = 切分 `command`」的简写。0.3 移除——`command` 始终是单个 token。）

---

## 输入 — stdin turn 对象

在 agent 读取 stdin 第一个字节之前，bridge 写入**恰好一行** NDJSON：turn 对象。

```json
{"type":"turn","message":"hello","session_id":"","session_name":"default",
 "attachments":[],"permission":false,"protocol_version":"0.4"}
```

### 必填字段

| 字段 | 说明 |
|------|------|
| `type` | 字面量 `"turn"`。 |
| `message` | 用户消息文本。可为 `""`（见完整规范「空 turn」）。 |
| `session_id` | 上一轮的 session ID（`""` = 新会话）。 |
| `protocol_version` | 协议版本字符串，例如 `"0.4"`。**不透明且不可比较**——agent MUST NOT 对它排序或范围检查。 |

### 可选字段（出现即相关）

| 字段 | 说明 |
|------|------|
| `session_name` | 人类可读的会话名（默认 `"default"`）。 |
| `attachments` | `{kind, url, ...}` 数组（如 `{"kind":"image","url":"https://..."}`）。唯一的附件通道——没有单附件便捷变量。缺省/`[]` = 无。 |
| `permission` | profile 开启 `permission: true` 时为 `true`；否则缺省/`false`。 |

密钥和 per-CLI 配置走**环境变量**（profile 的 `env` 块），不走 turn 对象。0.4 中单轮请求**不**走环境变量。

### stdin / EOF

- 当 `permission` 缺省/false 时，bridge 写入 turn 行后关闭 stdin（EOF）。agent 读完 turn 后 MUST NOT 再阻塞等待 stdin。
- 当 `permission: true` 时，bridge 保持 stdin 打开，以接收轮中的 `{"type":"permission_response"}` 行（见完整规范）。

---

## 输出 — stdout NDJSON 事件

stdout 每一行都是一个以 `\n` 结尾的 JSON 对象，带 `type` 字段。词汇表是**封闭的**：`partial`、`result`、`error`，以及（开启权限时的）`permission_request`。

```
{"type":"partial","text":"..."}            ← 流式分块；streaming: true 时转发
{"type":"result","text":"..."}             ← 终端回复正文（至多一条）；可选 usage
{"type":"error","message":"..."}           ← 用户可读错误；任意模式；使本轮失败
{"type":"permission_request",...}          ← 可选工具授权（permission: true）
```

这些事件上都可以带可选的 `session_id`。0.4 **没有** `{"type":"session"}` 或 `{"type":"text"}` 事件。

不是合法 JSON、不是对象、或 `type` 不被识别的行会被**忽略**（记到 stderr）——不作为回复正文。回复正文由 `result`（以及流式时已转发的 `partial`）承载。

### 事件上的 `session_id` — 第一个非空值

会话连续性是事件上的字段，不是独立事件类型。

- bridge 持久化本轮观察到的**第一个**非空 `session_id`。
- 早期事件 MAY 省略该字段；一旦已知，agent SHOULD 在后续事件上带上。
- 无状态 agent 完全省略 `session_id`（且 MUST NOT 铸造底层工具无法 resume 的 id）。
- 之后出现不同的非空值是违规（保留第一个；警告）。

```
{"type":"partial","text":"回答中..."}
{"type":"partial","text":"答案","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
{"type":"result","text":"","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

若同一轮既有 `session_id` 又有 `error`，bridge **MUST** 仍持久化 session id 供下一轮使用，即使本轮被报告为失败。

### Partial 事件

流式分块。`streaming: true` 时立即转发；否则 runner 忽略。可选 `role`（`"output"` | `"thinking"`）区分助手输出与推理。

```
{"type":"partial","text":"这是第一部分。"}
{"type":"partial","role":"thinking","text":"让我想想..."}
```

### Result 事件 — 终端回复正文

每轮至多一条 `result`。可选 `usage` 用于 token/费用统计。若流式已通过 `partial` 送达正文，`result.text` MAY 为 `""`。

```
{"type":"result","text":"这是完整答案。","session_id":"cli-sess-…"}
```

### Error 事件

用户可读错误。**无论** `streaming` 模式如何都生效。bridge 转发给用户、抑制后续 partial，且**即使退出码为 0 也 MUST** 将本轮视为失败。`error` 之后的 `result` 会被丢弃。

```
{"type":"error","message":"上游 API 限流。请 60 秒后重试。"}
```

### 可选工具权限

通过 profile `permission: true` 开启。不是通用 HIL——仅工具授权。没有轮中审批通道的 CLI 继续使用 `--dangerously-skip-permissions` / `--yolo`。

```
{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo ok > f.txt"}}
```

用户批准后 bridge 写入 stdin：

```
{"type":"permission_response","request_id":"1","behavior":"allow"}
```

stdin 保持打开规则、超时与字段定义见完整规范。

---

## 退出码

| 码 | 含义 |
|----|------|
| `0` | 成功 |
| `1` | 通用 agent 错误 |
| `124` | 超时（bridge 施加；与 GNU `timeout` 一致） |
| `130` | 被 SIGINT 中断 |
| `143` | 被 SIGTERM 终止 |

多种失败信号同时出现时的优先级：**超时 (124) > `error` 事件 (1) > 进程退出码**。

进程非零退出且未发出 `error` 事件时，bridge **SHOULD** 向用户发送通用错误消息。

---

## 超时处理

到达 `timeout_secs` 时：

1. bridge 发送 `SIGTERM`。
2. bridge 等待 `kill_grace_secs`（默认 5）让进程退出。
3. 若仍在运行，bridge 发送 `SIGKILL`。

已收到的 `partial` 事件仍会转发。agent SHOULD 在收到 `SIGTERM` 时冲刷缓冲的 partial 并尽快退出。

---

## 设计原则

1. **进程边界是唯一契约。** 任何从 stdin 读 turn、向 stdout 写 NDJSON 事件的进程都是合法 agent。
2. **Agent 不感知平台。** 投递、限流等平台事务是 bridge 的职责。
3. **Session ID 不透明。** bridge 存储并转发；含义由 agent 拥有。
4. **每进程一轮。** 长会话与轮中取消按设计不在范围内。
5. **`type:` 不属于本规范。** 内置快捷方式是平台扩展，不是 P0。

设计动机以及与 MCP、ACP、NDJSON、SSE、LSP、Unix filter 的对比，见 [GitHub 上的完整规范](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.zh.md)。
