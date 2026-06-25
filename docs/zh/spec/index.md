# 协议规范速查

**版本：** 0.1.0 · **状态：** 草案

完整规范维护在仓库的 [`spec/protocol.zh.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.zh.md)。本页是快速查阅版。

---

## Profile YAML

```yaml
command: ./my_agent.py        # 要执行的命令（按空格切 argv，不走 shell）
args: []                      # 支持 {{MESSAGE}}、{{SESSION_ID}}、{{SESSION_NAME}} 占位符
stdin: none                   # none | message（message = 写入后立即关闭 stdin）

cwd: /path/to/workspace
env:                          # 额外注入的环境变量，支持 ${VAR} 引用
  MY_API_KEY: "${MY_API_KEY}"

timeout_secs: 600             # stdout 读取超时，默认 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL 宽限期，默认 5
max_reply_chars: 8000
truncation_suffix: "\n\n…(输出已截断)"
include_stderr_in_reply: false
send_error_reply: true        # agent 失败时是否通知用户

streaming: true               # 实时转发 AGENT_PARTIAL: 行
session_line_prefix: "AGENT_SESSION:"
```

---

## 输入 — 环境变量

### 核心

| 变量名 | 说明 |
|--------|------|
| `AGENT_MESSAGE` | 用户消息文本 |
| `AGENT_SESSION_ID` | 上一轮返回的 session ID（空 = 新会话） |
| `AGENT_SESSION_NAME` | 会话可读名称（默认 `"default"`） |
| `AGENT_FROM_USER` | 发送者标识符 |
| `AGENT_STREAMING` | `"1"` = 流式，`"0"` = 单次 |
| `AGENT_PROTOCOL_VERSION` | 协议版本字符串，例如 `"0.1"` |

### 附件（P0 — 单附件）

| 变量名 | 说明 |
|--------|------|
| `AGENT_IMAGE_URL` | 图片附件 URL（仅当消息恰好含一张图片时设置） |
| `AGENT_FILE_URL` | 文件附件 URL（仅当消息恰好含一个文件时设置） |

### 附件（草案 — 多附件）

| 变量名 | 说明 |
|--------|------|
| `AGENT_ATTACHMENTS` | JSON 数组，元素为 `{type, url, name}`，`type` 取值 `image` / `file` / `audio` / `video`。若存在，agent 应优先使用此项，否则回退到上面的单附件变量。 |

---

## 输出 — stdout 协议

按行的前缀区分类型。判断顺序：`AGENT_SESSION:` → `AGENT_PARTIAL:` → `AGENT_ERROR:`，其余行原样作为回复正文。

```
AGENT_SESSION:<opaque-id>         ← 可选，任意位置，最后一行生效
AGENT_PARTIAL:<json-string>       ← 可选，流式分块
AGENT_ERROR:<json-string>         ← 可选，向用户透出错误
<回复正文>                         ← 其余所有行
```

### Session 行

可在 stdout 任意位置输出，**多行时最后一行生效**。这一行兼容了底层 CLI 直到退出才知道 session ID 的常见场景。

```
AGENT_PARTIAL:"回答中..."
AGENT_SESSION:cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c
```

### Partial 行

值为 JSON 编码的字符串。`streaming: false` 时忽略。

```
AGENT_PARTIAL:"这是第一部分。"
AGENT_PARTIAL:"这是第二部分。"
```

### Error 行

向用户透出一条错误消息。无论 `streaming` 是否开启都会被识别。bridge 转发后会终止正在进行的 partial 流，并将本次进程视为失败——即便退出码为 0。与 `AGENT_ERROR:` 同时产生的回复正文会被丢弃。

```
AGENT_ERROR:"上游 API 被限流，60 秒后重试。"
```

### 回复正文

所有非协议行构成最终回复，进程退出后发送。若全部内容已通过 `AGENT_PARTIAL:` 发出，正文为空时 bridge 自动跳过最终发送。

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 通用 agent 错误 |
| `124` | 超时（沿用 GNU `timeout` 约定） |
| `130` | 被 SIGINT（Ctrl-C）中断 |
| `143` | 被 SIGTERM 终止 |

当 `send_error_reply: true` 且进程以非零码退出（且未输出过 `AGENT_ERROR:`）时，bridge 发送一条通用错误提示。stderr 作为调试日志记录，不发给用户（除非 `include_stderr_in_reply: true`）。

---

## 超时处理

达到 `timeout_secs` 时：bridge 发送 `SIGTERM`，等待 `kill_grace_secs`（默认 5 秒），如仍运行则发送 `SIGKILL`。在此之前已收到的 `AGENT_PARTIAL:` 行仍然转发。agent SHOULD 处理 `SIGTERM`——刷新缓冲的 partial 输出并尽快退出。

---

## stdin EOF 合约

- `stdin: none`（默认）— bridge 不写 stdin，agent 的 stdin 读取立即返回 EOF。agent MUST NOT 在 stdin 上阻塞等待。
- `stdin: message` — bridge 将 `AGENT_MESSAGE` 写入 stdin 后立即发送 EOF。agent 可用 `input()`、`readline()`、`fs.readFileSync(0, 'utf8')` 等方式读取，且读取会终止。

---

## 设计原则

1. **进程边界是唯一的合约。** 任何能读取环境变量并写入 stdout 的进程都是合法的 agent。
2. **agent 不感知 bridge。** 平台相关的事（发送、限流、session 存储）是 bridge 的职责。
3. **Session ID 是不透明的。** bridge 只负责存储和转发，agent 拥有其含义。
4. **工作单位是单轮。** 每条用户消息启动一个进程。长驻守护进程超出本协议范围。
5. **`type:` 快捷方式不属于本规范。** 内置快捷方式是平台扩展，不是 P0。
