# P0 协议规范

**版本：** 0.1.0 · **状态：** 草案

完整规范维护在仓库的 [`spec/protocol.zh.md`](https://github.com/jeffkit/agentproc/blob/main/spec/protocol.zh.md)。

---

## Profile YAML

```yaml
command: ./my_agent.py        # 要执行的命令
args: []                      # 支持 {{MESSAGE}}、{{SESSION_ID}}、{{SESSION_NAME}} 占位符
stdin: none                   # none | message

cwd: /path/to/workspace
env:
  MY_API_KEY: "${MY_API_KEY}"

timeout_secs: 600
max_reply_chars: 8000
truncation_suffix: "\n\n…(输出已截断)"
include_stderr_in_reply: false

streaming: true
cli_session_first_line_prefix: "AGENT_SESSION:"
```

---

## 输入 — 环境变量

| 变量名 | 说明 |
|--------|------|
| `AGENT_MESSAGE` | 用户消息文本 |
| `AGENT_SESSION_ID` | 上一轮 CLI 返回的 session UUID（空 = 新会话） |
| `AGENT_SESSION_NAME` | 会话可读名称 |
| `AGENT_FROM_USER` | 发送者标识符 |
| `AGENT_STREAMING` | `"1"` = 流式，`"0"` = 单次 |
| `AGENT_IMAGE_URL` | 图片附件 URL（有图片时注入） |
| `AGENT_FILE_URL` | 文件附件 URL（有文件时注入） |

---

## 输出 — stdout 协议

```
AGENT_SESSION:<uuid>              ← 可选，仅限第一行，声明 session
AGENT_PARTIAL:<json-string>       ← 可选，任意行，流式分块
<回复正文>                         ← 其余所有内容 = 最终回复
```

### Session 行

在 stdout 第一行声明 CLI session UUID。bridge 存储后，下次同一会话来消息时通过 `AGENT_SESSION_ID` 回传。

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
```

### Partial 行

随时输出流式分块，值为 JSON 编码的字符串。

```
AGENT_PARTIAL:"这是第一部分内容。"
AGENT_PARTIAL:"这是第二部分内容。"
```

### 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| 非 `0` | 失败（`send_error_reply: true` 时向用户发送错误提示） |

---

## 设计原则

1. **进程边界是唯一的合约。** 任何能读取环境变量并写入 stdout 的进程都是合法的 agent。
2. **agent 不感知平台。** 平台相关的事（发送、限流）是 bridge 的职责。
3. **Session ID 是不透明的。** bridge 只负责存储和转发，agent 拥有其含义。
4. **`type:` 不属于本规范。** 内置快捷方式是平台扩展，不是 P0。
