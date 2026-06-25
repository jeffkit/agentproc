# AgentProc 协议规范

**版本：** 0.1.1
**状态：** 草案

---

## 概述

AgentProc 是一个通过进程接口将任意 agent CLI 接入消息平台的极简协议。它定义了 **bridge**（平台适配器）与 **agent process**（封装 AI agent 的脚本或可执行文件）之间的通信方式。

```
消息平台
    │
    ▼
  Bridge              ← 解析 profile YAML，管理进程生命周期
    │   env 变量（和可选的 stdin 写入）
    ▼
Agent Process         ← 你的脚本或二进制文件（实现下面的合约）
    │   stdout
    ▼
  Bridge              ← 将回复转发给消息平台
```

协议只有两个方向：

- **输入** — bridge 在进程启动前注入的环境变量（可选地附带一次 stdin 写入）
- **输出** — agent process 写入 stdout 的内容，按行的前缀区分类型

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
kill_grace_secs: 5            # SIGTERM → SIGKILL 的宽限期，默认 5
max_reply_chars: 8000         # 回复最大字符数，超出后截断，默认 8000
truncation_suffix: "\n\n…(输出已截断)"
include_stderr_in_reply: false
send_error_reply: true        # agent 出错时是否通知用户

# 流式回复
streaming: true               # 实时转发 AGENT_PARTIAL: 行

# 会话续接
session_line_prefix: "AGENT_SESSION:"  # 标记 session 行的前缀
```

### 占位符

`args`、`cwd` 和 `env` 值中的占位符在进程启动前替换，**不经过 shell**。

| 占位符 | 值 |
|--------|-----|
| `{{MESSAGE}}` | 用户消息文本 |
| `{{SESSION_ID}}` | 上一轮返回的 session ID（空 = 新会话） |
| `{{SESSION_NAME}}` | 会话可读名称 |

### 命令执行模型

`command` 字段 MUST 按空格切分为 argv 数组，并传给操作系统的 `execve`（或等价函数），**不**调用 shell。这样能避免通过 `{{MESSAGE}}` 占位符发起的 shell 注入。

如果 bridge 实现坚持使用 shell（例如为了做环境变量展开），它 MUST 对每个占位符替换值做 POSIX shell 转义。bridge 应当优先选择不走 shell 的形式。

### `stdin` 字段

| 值 | 行为 |
|----|------|
| `none`（默认） | 消息仅通过 `AGENT_MESSAGE` 环境变量传递 |
| `message` | 消息文本同时写入 stdin，**然后立即关闭 stdin（EOF）** |

当 `stdin: message` 时，bridge 写完消息后立即发送 EOF。agent 可以用任何面向行或流的 API（`input()`、`readline`、`fs.readFileSync(0)` 等）读取，且可以确保读取会终止。

---

## 输入 — 环境变量

bridge 在启动进程前注入以下变量，agent process 直接读取。

### 核心变量

| 变量名 | 说明 |
|--------|------|
| `AGENT_MESSAGE` | 用户消息文本 |
| `AGENT_SESSION_ID` | 上一轮返回的 session ID（空字符串 = 新会话） |
| `AGENT_SESSION_NAME` | 会话可读名称（默认 `"default"`） |
| `AGENT_FROM_USER` | 发送者标识符（平台相关：用户 ID、handle 等） |
| `AGENT_STREAMING` | `"1"` = 流式模式，`"0"` = 单次模式 |
| `AGENT_PROTOCOL_VERSION` | 协议版本字符串，例如 `"0.1"`。agent 可据此决定行为。 |

### 附件变量（P0 — 单附件）

| 变量名 | 说明 |
|--------|------|
| `AGENT_IMAGE_URL` | 图片附件 URL（仅当消息恰好含一张图片时设置） |
| `AGENT_FILE_URL` | 文件附件 URL（仅当消息恰好含一个文件时设置） |

### 附件变量（草案 — 多附件）

| 变量名 | 说明 |
|------|------|
| `AGENT_ATTACHMENTS` | JSON 数组，元素为 `{"type":"image\|file\|audio\|video", "url":"...", "name":"..."}`。**草案**：bridge 可在设置单附件变量的同时设置此项；agent 应当优先使用 `AGENT_ATTACHMENTS`（如果存在），否则回退到单附件变量。 |

profile `env` 块中声明的自定义变量也会一并注入。

---

## 输出 — stdout 协议

agent process 写入 stdout，bridge 实时逐行读取。

### 协议行识别规则

当且仅当一行匹配下列前缀之一时，才被当作**协议行**处理，按此顺序判断：

1. `AGENT_SESSION:` — 声明或更新 session ID
2. `AGENT_PARTIAL:` — 输出流式分块
3. `AGENT_ERROR:` — 输出错误消息

其余所有行都是**回复正文**，原样转发。

也就是说，agent 的回复正文 MUST NOT 包含以 `AGENT_SESSION:`、`AGENT_PARTIAL:` 或 `AGENT_ERROR:` 开头的行。如果 agent 必须输出这样的文本（比如用户在讨论协议本身），它 MUST 在行首加一个空格或用其他方式确保不匹配前缀。

> **bridge 实现提示**：如果想容忍 heredoc 等场景的前导空白，可以对去除首尾空白后的行匹配前缀；否则按原始行匹配。bridge 应保持一致。

### `AGENT_SESSION:` — session 行

如果 agent process 自己维护 session 状态（例如带 `--resume` 的 AI CLI），它通过输出下面的行来声明 session ID：

```
AGENT_SESSION:<opaque-string>
```

**Session 行规则（解决顺序歧义）：**

- session 行可以出现在 stdout 的**任意位置**——首行、夹在 partial 之间、或最后一行。
- 如果输出了多行 `AGENT_SESSION:`，**最后一行生效**。bridge 存储最终值，下一轮通过 `AGENT_SESSION_ID` 回传。
- 这条规则兼容了底层 CLI 直到退出才知道自己 session ID 的常见场景（例如 `claude --output-format stream-json` 在终止的 `result` 事件里才发出 session ID）。

session ID 字符串是**不透明的**——bridge 原样存储和转发，MUST NOT 解释其格式。它可以是 UUID、CLI 内部句柄，或任何不含空白和冒号的字符串。

这一行由 bridge 消费，**不会**出现在发给用户的回复中。

### `AGENT_PARTIAL:` — 流式分块

流式输出时，agent process 可以随时输出分块：

```
AGENT_PARTIAL:<JSON 编码的字符串>
```

值 MUST 是 JSON 编码的字符串（如 `"你好"`, `"第一行\n第二二行"`, `"emoji: 😀"`）。

**JSON 解析策略（解决歧义）：**

- bridge 尝试 JSON 解码前缀之后的文本。
- 解码成功时，立即将解码后的字符串转发给用户。
- 解码失败时，bridge **应当**将前缀之后的原始文本作为分块内容（容错模式）转发，并在 stderr 记录告警。bridge 可以选择严格模式（丢弃该行并记录），但默认应当是容错模式，以兼容手写 agent。

profile 中设置 `streaming: false` 时，bridge 忽略所有 `AGENT_PARTIAL:` 行。

### `AGENT_ERROR:` — 错误消息

当 agent 遇到需要告诉用户的错误时，输出：

```
AGENT_ERROR:<JSON 编码的字符串>
```

这一行**无论** `streaming` 是否开启都会被识别。bridge 将解码后的字符串作为错误回复转发给用户，并应当终止任何正在进行的 partial 流。

如果出现了 `AGENT_ERROR:` 行，bridge 应当视进程为失败，即使退出码为 0。agent 在输出 `AGENT_ERROR:` 后应当以非零码退出。

与 `AGENT_ERROR:` 同时产生的回复正文会被丢弃。

### 回复正文

所有**不是**协议行的 stdout 内容构成最终回复正文，在进程退出后发送给用户。

如果所有内容已通过 `AGENT_PARTIAL:` 发出，回复正文为空时 bridge 自动跳过最终发送。

### 完整示例

**流式 + session 在最后才发现（常见的 CLI 包装场景）：**

```
AGENT_PARTIAL:"这是回答的第一部分。"
AGENT_PARTIAL:"这是回答的第二部分。"
AGENT_SESSION:cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c
```

**单次模式 + session 提前声明：**

```
AGENT_SESSION:f47ac10b-58cc-4372-a567-0e02b2c3d479
这是完整的回答。
```

**流式中遇到错误：**

```
AGENT_PARTIAL:"让我查一下... "
AGENT_ERROR:"上游 API 被限流，60 秒后重试。"
```

---

## stdin / EOF 合约

- 当 `stdin: none`（默认）时，bridge 不向 stdin 写入任何内容。agent 的 stdin 读取会立即返回 EOF。
- 当 `stdin: message` 时，bridge 将 `AGENT_MESSAGE` 写入 stdin 后立即发送 EOF。agent 可以通过 `input()`、`readline()`、`fs.readFileSync(0, 'utf8')` 等方式读取，且读取会终止。

当 `stdin: none` 生效时，agent MUST NOT 在 stdin 上阻塞等待。

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功，stdout 内容（去掉协议行）作为回复发送 |
| `1` | 通用 agent 错误 |
| `124` | 超时（bridge 强加的；沿用 GNU `timeout` 的约定） |
| `130` | 被 SIGINT（Ctrl-C）中断 |
| `143` | 被 SIGTERM 终止 |

其他非零码视为通用错误。当 `send_error_reply: true` 且进程以非零码退出（且没有输出过 `AGENT_ERROR:`）时，bridge 发送一条通用错误提示给用户。

stderr 作为调试日志记录，不发给用户（除非 `include_stderr_in_reply: true`）。

---

## 超时处理

当达到 `timeout_secs` 而进程未退出时：

1. bridge 向进程发送 `SIGTERM`。
2. bridge 等待 `kill_grace_secs`（默认 5 秒）让进程退出。
3. 如仍运行，bridge 发送 `SIGKILL`。

在此之前已经收到的 `AGENT_PARTIAL:` 行仍然转发给用户。然后 bridge 发送一条超时错误回复（受 `send_error_reply` 控制）。

agent SHOULD 处理 `SIGTERM`——刷新任何缓冲的 partial 输出并尽快退出。

---

## 设计原则

**1. 进程边界是唯一的合约。**
bridge 不关心 agent 用什么语言写、调用什么 AI 模型、如何管理状态。任何能读取环境变量并写入 stdout 的进程都是合法的 agent。

**2. agent 不感知 bridge。**
agent process 不需要知道任何关于消息平台的事情。它读取消息，处理，写回复。平台相关的关切（发送、限流、session 存储）是 bridge 的职责。

**3. Session ID 是不透明的。**
bridge 存储和转发 session ID，但从不解释它们的含义。agent process 自己拥有 session ID 的语义。

**4. 工作单位是单轮。**
每条用户消息启动一个进程。agent 不被预期是长驻守护进程。（长驻守护进程超出本规范范围，见下文「与相关协议的对比」。）

**5. `type:` 快捷方式不属于本协议。**
内置快捷方式（如 `type: claude-code`）是平台扩展，不是 P0。各实现可以提供它们，但它们超出本规范的范围。

---

## 设计取舍

**为什么输入用环境变量，而不是 stdin 或 JSON 参数？**

三个原因：

1. **可调试性**。你可以直接在 shell 里驱动一个 agent：`AGENT_MESSAGE="hello" ./agent.sh`。无需脚手架、无需测试夹具。
2. **语言中立**。每种编程语言读取环境变量的方式都一样。命令行参数解析在不同语言和 shell 之间差异很大。
3. **无转义歧义**。一条长且多行的消息作为命令行参数需要 shell 转义；环境变量原样携带其完整值。

代价是环境变量有平台相关的体积限制（通常 128 KB – 8 MB）。超过此长度的消息应使用 `stdin: message`。

**为什么用哨兵前缀行，而不是 NDJSON？**

NDJSON（每行一个 JSON 对象）是 Claude Code `stream-json`、MCP、ACP 等内部使用的格式。它是个好格式——但它强制要求每一行发出的内容都是合法 JSON。AgentProc 希望下面这个是一个合法的 agent：

```bash
#!/usr/bin/env bash
echo "You said: $AGENT_MESSAGE"
```

哨兵前缀行让最常见的情况（最终回复正文）就是纯文本，而结构化事件（`AGENT_SESSION:`、`AGENT_PARTIAL:`、`AGENT_ERROR:`）通过前缀显式启用。代价是一条规则：回复正文不能以 `AGENT_` 加已知前缀开头。

**为什么 session 行「最后一行生效」？**

因为底层 CLI 经常直到退出才知道自己的 session ID。`claude --output-format stream-json` 在终止的 `result` 事件里才发出 session ID，而那是整个运行的最后一个事件。「必须在第一行」的规则会迫使 bridge 作者做尴尬的缓冲。「最后一行生效」让 agent 在知道 session ID 的任何时候输出都行。

**为什么除了非零退出码还要 `AGENT_ERROR:`？**

退出码告诉 bridge「*出错了*」，但不告诉它「*该对用户说什么*」。`AGENT_ERROR:` 让 agent 转发一条有意义的、用户可读的错误消息（如「API key 过期」、「被限流，60 秒后重试」），而不是 bridge 的通用模板。

---

## 与相关协议的对比

AgentProc 占据一个特定的生态位。相邻协议在*形态*上相似（子进程 + stdio），但在*目的*上不同。

### MCP — Model Context Protocol（Anthropic）

MCP 把一个 LLM 应用（客户端）连接到**工具和数据源**（服务器，一个子进程）。传输：stdio 或 HTTP+SSE 上的 JSON-RPC 2.0。

**与 AgentProc 的关系：****方向相反。** 在 MCP 中，AI 是客户端、工具提供者是子进程；在 AgentProc 中，bridge 是客户端、AI 包装器是子进程。它们自然组合：一个 AgentProc agent 可以在内部使用 MCP 工具。

- 规范：https://modelcontextprotocol.io/

### ACP — Agent Client Protocol（Zed Industries）

ACP 把代码编辑器连接到 AI 编程 agent。传输：stdio 上的 JSON-RPC 2.0，双向，长生命周期。

**与 AgentProc 的关系：****更丰富的表亲。** ACP 假设一个交互式 IDE 会话，包含工具调用、文件 diff、模式切换。AgentProc 假设每次进程调用对应一个聊天回合。如果你在构建 IDE，用 ACP；如果你在把聊天机器人桥接到 CLI，用 AgentProc。

- 规范：https://agentclientprotocol.com/

### NDJSON / JSON Lines

NDJSON 是每行一个 JSON 对象、换行分隔。它是 Claude Code、Codex、Gemini CLI 流式模式内部使用的传输格式，也被 MCP 使用。

**与 AgentProc 的关系：****备选传输格式。** NDJSON 要求每行都是合法 JSON。AgentProc 用哨兵前缀纯文本来保证手写 agent（`echo "You said: $AGENT_MESSAGE"`）合法。代价是一条消歧规则（回复正文不能以 `AGENT_*:` 开头）。

- 规范：https://jsonlines.org/

### SSE — Server-Sent Events（WHATWG）

SSE 在 HTTP 上流式传输 `event:` / `data:` 行。

**与 AgentProc 的关系：****`AGENT_PARTIAL:` 的语义祖先。** 「换行终止的事件 + 前缀」这个模式借自 SSE，去掉了 HTTP 传输层，固定了字段集合。

- 规范：https://html.spec.whatwg.org/multipage/server-sent-events.html

### LSP / DAP — Language Server / Debug Adapter Protocol（Microsoft）

LSP 和 DAP 把编辑器连接到语言服务器或调试器。传输：stdio 上的 JSON-RPC 2.0，使用 `Content-Length: N` 帧格式。

**与 AgentProc 的关系：****对照系。** LSP 用字节长度前缀分帧（允许二进制负载，但需要解析器）；AgentProc 用换行分帧（仅文本，手写解析也很简单）。这个取舍是刻意的。

- 规范：https://microsoft.github.io/language-server-protocol/ / https://microsoft.github.io/debug-adapter-protocol/

### Unix filter 惯例

POSIX 衍生的「从 stdin 读、向 stdout 写、成功退出码为 0」的惯例——Eric Raymond 的 *The Art of Unix Programming* 中有所总结。

**与 AgentProc 的关系：****哲学基础。** AgentProc 在 Unix filter 惯例之上扩展了两件 filter 没有的东西：session 续接握手（`AGENT_SESSION:`）和流式事件（`AGENT_PARTIAL:`）。其余都是普通的 Unix。

- 参考：http://www.catb.org/~esr/writings/taoup/html/ch01s06.html

### AgentProc *不是* 什么

- **不是机器人框架。** Hubot、Errbot、BotKit、Microsoft Bot Framework 都活在 bridge 的*上游*（进程内适配器、HTTP 连接器）。AgentProc 定义的是 bridge 与 agent *之间*的合约，与这些框架正交。
- **不是 agent 间协议。** A2A / AGNTCY 解决的是另一个问题（agent 之间互相通信）。
- **不是 IDE 协议。** 那是 ACP 的领域。
- **不是工具协议。** 那是 MCP 的领域。

---

## Changelog

- **0.1.0** — 首个公开草案。定义了环境变量输入、哨兵前缀 stdout、`AGENT_SESSION:` / `AGENT_PARTIAL:` / `AGENT_ERROR:`、session 行「最后一行生效」规则、`AGENT_PROTOCOL_VERSION`、`AGENT_ATTACHMENTS`（草案）、超时/SIGTERM 合约、退出码约定、stdin EOF 合约、命令执行不走 shell 规则。
