# AgentProc 协议规范

**版本：** 0.1.0  
**状态：** 草案

---

## 概述

AgentProc 是一个通过进程接口将任意 Agent CLI 接入消息平台的极简协议。它定义了 **bridge**（平台适配器）与 **agent process**（封装 AI agent 的脚本或可执行文件）之间的通信方式。

```
消息平台
    │
    ▼
  Bridge              ← 解析 profile YAML，管理进程生命周期
    │   stdin / env
    ▼
Agent Process         ← 你的脚本或二进制文件（P0 协议的实现方）
    │   stdout
    ▼
  Bridge              ← 将回复转发给消息平台
```

协议只有两个方向：

- **输入** — bridge 在进程启动前注入的环境变量
- **输出** — agent process 写入 stdout 的内容

没有 HTTP，没有 socket，没有共享内存，只有进程。

---

## Profile YAML

profile 是一个 YAML 文件，告诉 bridge 如何启动 agent process。

```yaml
# 必填：要执行的命令
command: ./my_agent.py        # 脚本或二进制的路径
args: []                      # 可选参数（支持占位符）
stdin: none                   # none | message

# 执行环境
cwd: /path/to/workspace       # 工作目录（支持 ~ 和占位符）
env:                          # 额外注入的环境变量
  MY_API_KEY: "${MY_API_KEY}" # 用 ${VAR} 引用已有环境变量

# 输出控制
timeout_secs: 600             # stdout 读取超时，默认 1800
max_reply_chars: 8000         # 回复最大字符数，超出后截断，默认 8000
truncation_suffix: "\n\n…(输出已截断)"
include_stderr_in_reply: false

# 流式回复
streaming: true               # 实时转发 AGENT_PARTIAL: 行

# 会话续接
cli_session_first_line_prefix: "AGENT_SESSION:"
```

### 占位符

`args`、`cwd` 和 `env` 值中的占位符在进程启动前替换，**不经过 shell**。

| 占位符 | 值 |
|--------|-----|
| `{{MESSAGE}}` | 用户消息文本 |
| `{{SESSION_ID}}` | 上一轮的 session UUID（空 = 新会话） |
| `{{SESSION_NAME}}` | 会话可读名称 |

### `stdin` 字段

| 值 | 行为 |
|----|------|
| `none`（默认） | 消息仅通过 `AGENT_MESSAGE` 环境变量传递 |
| `message` | 消息文本同时写入 stdin（适合消息较长或含换行的场景） |

---

## 输入 — 环境变量

bridge 在启动进程前注入以下变量，agent process 直接读取。

| 变量名 | 说明 |
|--------|------|
| `AGENT_MESSAGE` | 用户消息文本 |
| `AGENT_SESSION_ID` | 上一轮返回的 CLI session UUID（空字符串 = 新会话） |
| `AGENT_SESSION_NAME` | 会话可读名称（默认 `"default"`） |
| `AGENT_FROM_USER` | 发送者标识符 |
| `AGENT_STREAMING` | `"1"` = 流式模式，`"0"` = 单次模式 |
| `AGENT_IMAGE_URL` | 图片附件 URL（消息含图片时注入） |
| `AGENT_FILE_URL` | 文件附件 URL（消息含文件时注入） |

profile `env` 块中声明的自定义变量也会一并注入。

---

## 输出 — stdout 协议

agent process 写入 stdout，bridge 实时逐行读取。

### Session 行（可选）

如果 agent process 自己维护 session 状态（例如带 `--resume` 的 AI CLI），可以在 stdout **第一行**声明 session ID：

```
AGENT_SESSION:<uuid>
```

bridge 存储该 UUID，下次同一会话来消息时通过 `AGENT_SESSION_ID` 回传。这样就实现了多轮续接，而 bridge 无需理解底层 AI 系统的工作方式。

这一行由 bridge 消费，**不会**出现在发给用户的回复中。

### Partial 行（可选，流式）

流式输出时，agent process 可以随时输出分块：

```
AGENT_PARTIAL:<JSON 编码的字符串>
```

值必须是 JSON 编码的字符串（如 `"你好"`, `"第一行\n第二行"`）。bridge 收到每个 partial 后立即转发给用户，不等进程退出。

profile 中设置 `streaming: false` 时，bridge 忽略所有 `AGENT_PARTIAL:` 行。

### 回复正文

所有**不是** session 行或 partial 行的 stdout 内容构成最终回复，在进程退出后发送给用户。

如果所有内容已通过 `AGENT_PARTIAL:` 发出，回复正文为空时 bridge 自动跳过最终发送。

### 完整示例

流式模式：

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
AGENT_PARTIAL:"这是回答的第一部分。"
AGENT_PARTIAL:"这是回答的第二部分。"
```

非流式模式：

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
这是完整的回答。
```

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功，stdout 内容作为回复发送 |
| 非 `0` | 失败，`send_error_reply: true` 时 bridge 发送错误提示 |

stderr 作为调试日志记录，不发给用户（除非 `include_stderr_in_reply: true`）。

---

## 设计原则

**1. 进程边界是唯一的合约。**  
bridge 不关心 agent 用什么语言写、调用什么 AI 模型、如何管理状态。任何能读取环境变量并写入 stdout 的进程都是合法的 agent。

**2. agent 不感知 bridge。**  
agent process 不需要知道任何关于消息平台的事情。它读取消息，处理，写回复。平台相关的关切（发送、限流、session 存储）是 bridge 的职责。

**3. Session ID 是不透明的。**  
bridge 存储和转发 session ID，但从不解释它们的含义。agent process 自己拥有 session ID 的语义。

**4. `type:` 不属于本协议。**  
内置快捷方式（如 `type: claude-code`）是平台扩展，不是 P0。各实现可以提供它们，但它们超出本规范的范围。
