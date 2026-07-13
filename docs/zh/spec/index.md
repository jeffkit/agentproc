# 协议规范速查

**线协议：** `0.3` · **文档修订：** `1.0` · **状态：** 草案

完整规范维护在仓库的 [`spec/protocol.zh.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.zh.md)。本页是快速查阅版。

---

## Profile YAML

```yaml
command: python3                      # argv[0]——始终是单个 token，永不切分
args: ["{{PROFILE_DIR}}/my_agent.py"] # argv[1..]；缺省时默认为 []
                                      # {{PROFILE_DIR}} = profile 所在目录

# cwd 可选。缺省时默认为 bridge 进程的 cwd。
# 若为相对路径，则相对 {{PROFILE_DIR}}（profile 所在目录）解析。
cwd: /path/to/workspace
env:
  MY_API_KEY: "${MY_API_KEY}"
env_allowlist: [MY_API_KEY]           # 可选：限制 ${VAR} 展开

timeout_secs: 600             # stdout 读取超时，默认 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL 宽限期，默认 5
max_reply_chars: 8000
truncation_suffix: "\n\n…(输出已截断)"
include_stderr_in_reply: false
send_error_reply: true        # agent 失败时是否通知用户

streaming: true               # 实时转发 {"type":"partial"} 事件

permission: false             # 可选工具授权（保持 stdin 打开；见完整规范）
```

占位符替换**不经过 shell**。argv 由两个字段拼成：

- **`command`** —— argv[0]。单个 token，**永不切分**，即使含空格。
- **`args`** —— argv[1..] 的 token 列表。**缺省时默认为 `[]`。**

最终 argv（`[command, *args]`）直接传给 `execve`，既能避免通过 `{{MESSAGE}}` 的 shell 注入，也允许 `command` 携带带空格的路径：

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
 "from_user":"u1","attachments":[],"permission":false,"protocol_version":"0.3"}
```

### 必填字段

| 字段 | 说明 |
|------|------|
| `type` | 字面量 `"turn"`。 |
| `message` | 用户消息文本。可为 `""`（见完整规范的「空 turn」）。 |
| `session_id` | 上一轮返回的 session ID（`""` = 新会话）。 |
| `from_user` | 发送者标识符（平台相关）。 |
| `protocol_version` | 协议版本字符串，例如 `"0.3"`。**不透明且不可比较**——agent MUST NOT 对它排序或范围检查。 |

### 可选字段（出现即相关）

| 字段 | 说明 |
|------|------|
| `session_name` | 会话可读名称（默认 `"default"`）。 |
| `attachments` | `{kind, url, ...}` 数组（如 `{"kind":"image","url":"https://..."}`）。0.3 中唯一的附件通道——没有单附件便捷变量。缺省/`[]` = 无。 |
| `permission` | profile `permission: true` 时为 `true`；否则缺省/`false`。 |

密钥和 per-CLI 配置走**环境变量**（profile 的 `env` 块），不走 turn 对象。0.3 中单轮请求**不**走环境变量。

### stdin / EOF

- `permission` 缺省/false 时，bridge 写入 turn 行后即关闭 stdin（EOF）。agent 读到 turn 后 MUST NOT 在 stdin 上阻塞等待。
- `permission: true` 时，bridge 保持 stdin 打开，用于中途的 `{"type":"permission_response"}` 行（见完整规范）。

---

## 输出 — stdout NDJSON 事件

stdout 每一行都是一个以 `\n` 结尾的 JSON 对象，带 `type` 字段。词汇表是**封闭的**：`partial`、`text`、`session`、`error`，以及（开启权限时的）`permission_request`。

```
{"type":"partial","text":"..."}            ← 流式分块；streaming: true 时转发
{"type":"text","text":"..."}               ← 最终回复正文；多个事件按序拼接
{"type":"session","id":"<opaque-id>"}      ← session id；任意位置；最后一行生效
{"type":"error","message":"..."}           ← 面向用户的错误；任意模式；使本轮失败
{"type":"permission_request",...}          ← 可选工具授权（permission: true）
```

不是合法 JSON、不是对象、或 `type` 不被识别的行会被**忽略**（记到 stderr）——不作为回复正文。回复正文只由 `text` 事件承载。

### Session 事件 — 最后一行生效

session 事件可出现在 stdout **任意位置**。若输出多行，**最后一行生效**。这兼容了底层 CLI 直到退出才知道 session ID 的常见场景。

如果同一对话中同时出现 `session` 事件和 `error` 事件，bridge **MUST** 仍然为下一轮保留 session ID，即便当前这一轮作为失败上报。

```
{"type":"partial","text":"回答中..."}
{"type":"partial","text":"答案"}
{"type":"session","id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

### Partial 事件

流式分块。`streaming: true` 时立即转发；否则被 runner 忽略。可选 `role`（`"output"` | `"thinking"`）区分助手输出与推理。

```
{"type":"partial","text":"这是第一部分。"}
{"type":"partial","role":"thinking","text":"让我想想..."}
```

### Text 事件 — 回复正文

最终回复正文。多个 `text` 事件按序拼接。若全部内容已通过 `partial` 发出、没有 `text` 事件，则最终回复为空，bridge 跳过最终发送。

```
{"type":"text","text":"这是完整答案。"}
```

### Error 事件

面向用户的错误。无论 `streaming` 是否开启都会被识别。bridge 转发后抑制后续 partial，并 **MUST** 将这一轮视为失败——即便退出码为 0。与 `error` 同时产生的 `text` 会被丢弃。

```
{"type":"error","message":"上游 API 被限流，60 秒后重试。"}
```

### 可选工具授权

通过 profile `permission: true` 可选开启。这不是通用 HIL，只做工具执行授权。没有中途授权通道的 CLI 继续使用 `--dangerously-skip-permissions` / `--yolo`。

```
{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo ok > f.txt"}}
```

用户批准后，bridge 向 stdin 写入：

```
{"type":"permission_response","request_id":"1","behavior":"allow"}
```

stdin 保持打开、超时与字段定义见完整规范。

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 通用 agent 错误 |
| `124` | 超时（沿用 GNU `timeout` 约定） |
| `130` | 被 SIGINT（Ctrl-C）中断 |
| `143` | 被 SIGTERM 终止 |

多个失败信号同时到来时的优先级：**超时 (124) > `error` 事件 (1) > 进程退出码**。

当 `send_error_reply: true` 且进程以非零码退出（且未输出过 `error` 事件）时，bridge 发送一条通用错误提示。

---

## 超时处理

达到 `timeout_secs` 时：

1. bridge 发送 `SIGTERM`。
2. bridge 等待 `kill_grace_secs`（默认 5 秒）让进程退出。
3. 如仍运行，bridge 发送 `SIGKILL`。

此前已收到的 `partial` 事件仍然转发。agent SHOULD 处理 `SIGTERM`——刷新缓冲的 partial 输出并尽快退出。

---

## 设计原则

1. **进程边界是唯一的合约。** 任何能从 stdin 读取 turn、向 stdout 写 NDJSON 事件的进程都是合法的 agent。
2. **agent 不感知平台。** 平台相关的事（发送、限流）是 bridge 的职责。
3. **Session ID 是不透明的。** bridge 只负责存储和转发，agent 拥有其含义。
4. **工作单位是单轮。** 长驻守护进程和中途取消按设计超出范围。
5. **`type:` 不属于本规范。** 内置快捷方式是平台扩展，不是 P0。

完整规范（设计理由、与 MCP/ACP/NDJSON/SSE/LSP/Unix filter 的对比）见 [GitHub 上的完整规范](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.zh.md)。
