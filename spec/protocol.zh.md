# AgentProc 协议规范

**线协议（Wire protocol）：** `0.4`（由 turn 对象的 `protocol_version` 字段携带的字符串）
**文档修订：** `1.1`
**状态：** 草案

线协议与本文档**独立编号**。线协议版本仅在 stdin/stdout 上的字节发生变化时才更新；文档修订号追踪不影响一致 agent 或 bridge 收发内容的编辑性更新——例如措辞澄清、新增指引。实现者在读取 `protocol_version` 时应遵循下方的[版本治理](#版本治理)规则。

---

## 版本治理

`protocol_version` 是一个**不透明字符串**，不是可比较的数字。agent 与 bridge **MUST NOT** 对它进行排序、比较或范围检查。两个字符串要么相等，要么不相等。

- 如果 bridge 注入了一个 agent 不识别的版本字符串，agent **SHOULD** 按字段未设置处理（best-effort，fail-soft）。
- 如果 agent 期望某个 bridge 未注入的版本字符串，agent **MUST** 回退到其内置默认值。
- 该字符串**不是**能力发现机制：不存在协商、不存在能力声明、也不存在排序。agent 若需要知道某项具体能力（例如图片附件）是否存在，**MUST** 直接检查 [turn 对象](#输入--stdin-turn-对象)中对应的字段（例如非空的 `attachments` 数组），而不是检查版本字符串。

理由：任何可比较的版本号都会诱导实现者用 `>= 0.4` 来 gate 行为，而一旦某个 bridge 没有同步 bump 数字，这种判断就会失效。把字符串视为不透明，能让契约保持诚实：某项能力的存在由承载它的字段来表示。

---

## 概述

AgentProc 是一个通过进程接口将任意 agent CLI 接入消息平台的极简协议。它定义了 **bridge**（平台适配器）与 **agent process**（封装 AI agent 的脚本或可执行文件）之间的通信方式。

```
消息平台
    │
    ▼
  Bridge              ← 解析 profile YAML，管理进程生命周期
    │   stdin：一个 NDJSON turn 对象（其后可选 permission 响应）
    │   env：  密钥 / 配置（profile 的 env 块）
    │   argv：  通过 {{MESSAGE}} 等占位符注入启动参数
    ▼
Agent Process         ← 你的脚本或二进制文件（实现下面的合约）
    │   stdout：NDJSON 事件（每行一个 JSON 对象）
    ▼
  Bridge              ← 将回复转发给消息平台
```

协议有三条输入路径和一条输出路径：

- **输入 — stdin：** 单个 NDJSON [turn 对象](#输入--stdin-turn-对象)，描述本次 turn，在进程启动前（或刚启动后）写入。当启用[可选的工具授权](#可选工具授权)时，stdin 保持打开，bridge 在 turn 进行中继续写入 NDJSON `permission_response` 对象。
- **输入 — 环境变量：** 密钥与配置（profile 的 `env` 块，外加一组最小 infra 集合）。0.4 中**每轮请求不再走 env 变量**——它走 stdin 的 turn 对象。
- **输入 — argv 占位符：** `{{MESSAGE}}`、`{{SESSION_ID}}`、`{{SESSION_NAME}}`、`{{PROFILE_DIR}}` 在启动前替换进 `command`/`args`，供把消息作为 CLI 参数传给底层 CLI 的 agent 使用。
- **输出 — stdout：** NDJSON 事件，每行一个 JSON 对象，通过 `type` 字段区分类型。

没有 HTTP，没有 socket，没有共享内存，只有进程。

---

## Profile YAML

profile 是一个 YAML 文件，告诉 bridge 如何启动 agent process。

```yaml
# 必填：要执行的程序（始终是 argv[0]，永不拆分）
command: python3
args: ["{{PROFILE_DIR}}/bridge.py"]   # argv[1..]；省略时默认为 []

# 执行环境
cwd: /path/to/workspace       # 工作目录（支持 ~ 和占位符）
env:                          # 额外环境变量（密钥 / 配置）
  MY_API_KEY: "${MY_API_KEY}" # 用 ${VAR} 引用已有 env 变量
env_allowlist: [MY_API_KEY]   # 可选：限制 env 块可读取哪些 ${VAR}

# 输出控制
timeout_secs: 600             # stdout 读取超时，默认 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL 的宽限期，默认 5
max_reply_chars: 8000         # 在此长度截断（正文 + 流式 partial），默认 8000
truncation_suffix: "\n\n…(truncated)"   # 回复被截断时追加的提示，默认如图。空字符串禁用提示（截断仍然生效）。
include_stderr_in_reply: false
send_error_reply: true        # agent 出错时告知用户

# 流式（bridge 侧提示）
streaming: true               # 实时转发 {"type":"partial"} 事件

# 可选工具授权（默认 false —— 见「可选工具授权」）
permission: false             # true → 保持 stdin 打开；处理 permission_request/response 帧
```

### 占位符

`command`、`args`、`cwd`、`env` 值中的占位符在进程启动前替换。它们**不**经过 shell。

| 占位符 | 值 |
|-------------|-------|
| `{{MESSAGE}}` | 用户消息文本 |
| `{{SESSION_ID}}` | 上一轮的会话 ID（空 = 新会话） |
| `{{SESSION_NAME}}` | 人类可读的会话名 |
| `{{PROFILE_DIR}}` | 包含 profile YAML 的目录的绝对路径。让 profile 能引用内置脚本（例如 `command: python3`，`args: ["{{PROFILE_DIR}}/bridge.py"]`），与 agent 的 `cwd` 无关。bridge 在按路径调用 profile 时设置此项；未设置时（例如不带文件的编程式使用）展开为空。 |

### `env` 值中的 `${VAR}` 展开

profile `env` 块中的值可用 `${VAR}` 语法引用 bridge 自身的环境变量（例如 `MY_API_KEY: "${MY_API_KEY}"`）。这是**针对 bridge 进程完整环境的替换**，不是针对 profile 或 agent 的。

**安全含义。** profile 是**受信输入**——任何能写 profile 的人都能通过 `${VAR}` 展开读取 bridge 能访问的每一个环境变量（云凭据、token、密钥）。这是设计如此（profile 是配置，不是用户输入），但有一点值得指出：

> **不要运行来自不受信任来源的 profile。** `agentproc hub run <name>` 会从 GitHub 仓库拉取 profile 并运行。如果你不信任该仓库维护者通过 `${VAR}` 引用*读取*你的 shell 环境，就不要运行他们的 profile。`env_allowlist`（见下）缩小了 `${VAR}` 可展开的范围。信任决策仍由运行 profile 的用户承担。

bridge 按 POSIX shell 语义展开 `${VAR}`：未知变量展开为空字符串，而非字面量 `${VAR}`。

### `env_allowlist` —— 收窄 `${VAR}` 展开

默认情况下，`env` 块中的每个 `${VAR}` 都针对 bridge 的完整环境展开（这样 profile 作者能拉取他们声明的凭据）。`env_allowlist` 让 profile 把展开范围精确收窄到它需要的变量：

```yaml
env:
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
env_allowlist: [ANTHROPIC_API_KEY]
```

- **可选。** `env_allowlist` 缺省时，所有 `${VAR}` 引用针对 bridge 完整环境展开。
- **存在时：** 名字**不在**列表中的 `${VAR}` 展开为空字符串，bridge 向 stderr 记一条警告（例如 `env_allowlist blocked ${AWS_SECRET_ACCESS_KEY}; expanded to empty`）。进程仍会启动——值或列表中的拼写错误表现为空变量加一条警告，而非硬失败。
- **范围。** `env_allowlist` 仅治理 profile `env` 块内的 `${VAR}` 展开。它不影响 infra 集（见下）、`{{MESSAGE}}` / `{{SESSION_ID}}` / `{{PROFILE_DIR}}` 占位符，或完全不含 `${VAR}` 的 `env` 值。
- **不支持通配。** 名字必须精确匹配。`["ANTHROPIC_*"]` 不匹配 `ANTHROPIC_API_KEY`——请逐个完整列出。
- **建议。** Hub profile **SHOULD** 设置 `env_allowlist`，使 `${VAR}` 展开成为「profile 读取哪些凭据」的显式声明。结合始终最小的 infra 集，「他们声明的」就是 agent 看到的。

### 子进程环境构成

agent process 的环境由恰好三层按以下顺序构成（后层覆盖前层）：

1. **Infra 集** —— bridge 从自身环境拷贝这些名字到子进程（当其已设置时）：`PATH`、`HOME`、`USER`、`LOGNAME`、`SHELL`、`LANG`、`LC_ALL`、`LC_CTYPE`、`LC_MESSAGES`、`TERM`、`TMPDIR`、`TZ`、`PWD`，以及 Windows 上的 `SystemRoot`、`TEMP`、`TMP`、`USERPROFILE`、`USERNAME`、`PATHEXT`、`COMSPEC`、`APPDATA`、`LOCALAPPDATA`、`PROGRAMDATA`、`NUMBER_OF_PROCESSORS`、`PROCESSOR_ARCHITECTURE`、`OS`。这些是 agent 找到解释器、临时目录和 locale 所需的运维变量——均不含凭据。Infra 集**始终**应用；0.3 中没有「继承一切」模式。
2. **Profile `env` 块** —— 经 `${VAR}` 展开和 `env_allowlist` 过滤后。
3. **CLI `--env` 附加** —— 若 bridge 的 CLI 暴露了按运行覆盖的入口。

profile 若需要 bridge 持有的某个非密钥变量（例如自定义 `WORKSPACE_DIR`），必须在 `env` 块中声明。未声明的环境变量**不会**到达 agent。（0.3 移除了 0.2 中存在的 `env_inherit: all` 逃生舱；依赖环境变量的 profile 必须显式声明。）

### `cwd` 语义

| 来源 | 发生什么 |
|--------|--------------|
| profile 中的 `cwd`（绝对路径） | 原样使用 |
| profile 中的 `cwd`（相对路径） | 相对 `{{PROFILE_DIR}}`（profile 自身目录）解析，而非 bridge 进程的 cwd。这让 `cwd: .` 表示「profile 所在目录」 |
| 省略 `cwd` | 默认为 bridge 进程的 cwd（通常是用户当前目录） |
| `--cwd` 标志（若 bridge 的 CLI 暴露） | 覆盖以上所有 |

`{{PROFILE_DIR}}`（定位内置脚本）与 `cwd`（agent 实际运行处）的分离是有意为之：hub profile 可以内置 bridge 脚本，同时让 `claude`/`codex` 等在用户所在的项目里运行。

### 命令执行模型

bridge 从两个字段拼装 agent 的 argv：

- **`command`** —— 可执行文件（argv[0]）。单个 token，**永不拆分**，即使含空格。若含空格，bridge 把整串作为一个 argv token 传给 `execve`。
- **`args`** —— YAML 列表，额外的 argv token（argv[1..]）。每个列表元素是一个 argv token，原样传递。**省略时默认为 `[]`。**

结果 argv（`[command, *args]`）传给平台的 `execve`（或等价物），**不调用 shell**。这避免了通过 `{{MESSAGE}}` 占位符的 shell 注入攻击，也让 `command` 能携带含空格的路径。

```yaml
# 多 token 命令 —— 常规形式：
command: python3
args: ["{{PROFILE_DIR}}/bridge.py", "--flag"]

# 单 token，无 args —— args 默认为 []：
command: ./my_agent

# 含空格的路径 —— 无需特殊处理，command 是单个 token：
command: "/path with spaces/my agent"
args: []
```

**从 0.2 迁移。** 0.2 允许一种简写：`args` *缺省* 且 `command` 含空格时表示「按空格拆分 `command` 为 argv」。0.3 移除此简写：`command` 始终是单个 token。写了 `command: python3 {{PROFILE_DIR}}/bridge.py` 的 profile 必须拆成 `command: python3` + `args: ["{{PROFILE_DIR}}/bridge.py"]`。迁移是机械的。

若 bridge 实现选择使用 shell（例如为做环境变量展开），它 **MUST** 对每个占位符替换应用 POSIX shell 引用。bridge **SHOULD** 优先采用不走 shell 的形式。

### `permission` 字段

| 值 | 行为 |
|-------|----------|
| `false` / 缺省（默认） | 可选 permission 帧**不**属于本次 turn。看到 `{"type":"permission_request"}` 事件的 bridge **SHOULD** 记一条警告并忽略它（不阻塞）。需要工具批准但无此字段的 agent **MUST** 使用 CLI 侧的自动批准模式（例如 `--dangerously-skip-permissions`、`--yolo`）或预先允许工具。 |
| `true` | 为此 profile 启用可选 permission 通道。bridge **MUST** 保持 stdin 打开，处理 `{"type":"permission_request"}` 事件并写入匹配的 `{"type":"permission_response"}` 对象，并在 [turn 对象](#输入--stdin-turn-对象)中设置 `permission: true`。 |

`permission` 是**可选启用**。没有 turn 中批准通道的 profile 和 CLI 保持不变。

---

## 输入 — stdin turn 对象

在 agent process 读取 stdin 第一个字节之前，bridge 写入**恰好一行** NDJSON：turn 对象。它是单个 JSON 对象，以 `\n` 结尾。

```json
{"type":"turn","message":"hello","session_id":"","session_name":"default",
 "from_user":"u1","attachments":[],"permission":false,"protocol_version":"0.4"}
```

### 必填字段

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `type` | string | 字面量 `"turn"`。 |
| `message` | string | 用户消息文本。可为 `""`——见下文「空 turn」。 |
| `session_id` | string | 上一轮的会话 ID（`""` = 新会话）。 |
| `from_user` | string | 发送者标识（平台相关：用户 ID、handle 等）。 |
| `protocol_version` | string | 协议版本字符串，例如 `"0.4"`。**不透明且不可比较**——见[版本治理](#版本治理)。 |

### 可选字段（存在 = 相关）

可选字段采用**存在即能力**：当某能力在起作用时 bridge 包含该字段，否则省略（或发送中性值）。需要知道某能力是否受支持的 agent 检查该字段是否存在，而不是检查版本字符串。

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `session_name` | string | 人类可读的会话名。缺省时默认为 `"default"`。 |
| `attachments` | array | 本次 turn 的附件列表。每个元素是一个对象，至少含 `kind`（字符串，例如 `"image"`、`"file"`）和 `url`（字符串）；bridge **MAY** 包含额外字段（例如 `filename`、`mime_type`、`size`）。缺省或 `[]` = 本次 turn 无附件。不再有单独的单附件便利字段——`attachments` 是唯一的附件通道。 |
| `permission` | boolean | 当 profile 设了 `permission: true` 且 bridge 支持可选 permission 通道时为 `true`；否则缺省或 `false`。能发出 permission 请求的 agent **MUST** 在依赖 turn 中批准前检查此项——缺省意味着只能自动批准 / skip-permissions。 |

profile `env` 块中声明的自定义变量通过环境注入，而非 turn 对象。

### 空 turn

当以下**任一**成立时，turn 视为「带有内容」：

- `message` 非空
- `attachments` 存在且非空

若以上都不成立，turn 为空，bridge / agent **SHOULD** 报错而非继续。此规则照顾了常见的「纯图片消息」场景：用户发截图但不附文字。

### 读取 turn

agent 从 stdin 读取恰好一行并 JSON 解码。之后：

- 当 `permission` 缺省/false 时，bridge 在 turn 行之后立即关闭 stdin（EOF）。agent **MUST NOT** 在读完 turn 后阻塞于 stdin。
- 当 `permission` 为 true 时，bridge 保持 stdin 打开；agent **MAY** 继续读后续行（见[可选工具授权](#可选工具授权)）。

---

## 输出 — stdout NDJSON 事件

agent process 写入 stdout。bridge 逐行实时读取。**每行都是一个 JSON 对象**，以 `\n` 结尾，携带 `type` 字段说明事件类型。

### 事件类型

| `type` | 方向 | 描述 |
|--------|-----------|-------------|
| `partial` | agent → bridge | 流式分块，立即转发给用户。 |
| `result` | agent → bridge | 本次 turn 的终止性成功正文（至多一条）。 |
| `error` | agent → bridge | 终止性错误消息，转发给用户。 |
| `permission_request` | agent → bridge | 可选的工具授权请求（仅当 `permission: true`）。 |

### 封闭词汇表

上述事件集合是**封闭**的。六个 `type` 值——`turn`（stdin）、`partial`、`result`、`error`、`permission_request` 与 `permission_response`（stdin）——是整个协议的全部词汇。AgentProc 故意**不**为工具调用、文件 diff、计划更新、推理块等更丰富语义增长类型化事件。需要这些的 agent 应在内部封装一个面向 IDE 的协议（例如 ACP）；AgentProc bridge 不渲染 diff、不持有用户文件，因此与这类事件无关。bridge **MUST NOT** 期待额外事件类型，agent **MUST NOT** 发明新类型来向本协议走私更丰富的语义——那条路只会把 ACP 实现得很糟。未知 `type` 按[格式错误的行](#格式错误的行)处理。

本协议范围限定为**一个 turn**：一条用户消息、一个进程、一条回复。长生命周期会话、turn 中取消、并发请求、客户端提供的回调（文件系统、终端）按设计不在范围内——正是这些让 ACP 成为 IDE 协议而非聊天桥接协议。

### 事件上的 `session_id`

会话连续性作为 stdout 事件上的可选字段携带，**不是**独立事件类型。

| 规则 | 要求 |
|------|-------------|
| 持久化 | bridge 持久化本 turn 中观察到的**第一个**非空 `session_id`，并在下一轮作为 `turn.session_id` 传回。若没有任何事件携带非空 `session_id`，bridge 不会从本 turn 学到新的 agent 会话 id。 |
| 一旦已知 | 非空 `session_id` 已知后，agent **SHOULD** 在后续每条 stdout 事件上附着同一值。早期事件 **MAY** 省略该字段（例如在等待 CLI `init` 时）。先省略后出现**不**构成协议违规。 |
| 存在时必须非空 | 输出上，`session_id` **MUST NOT** 为 `""`。输入上的空字符串（`turn.session_id`）仍表示「新会话」；该含义不适用于输出。 |
| 无状态 agent | 没有原生 resume/session 的 agent **MUST** 在每条事件上**省略**该字段。当底层工具无法用该 id 恢复时，它们 **MUST NOT** 铸造 id。生成工具本身作为输入所需的 id（例如 CLI `--session <id>` 标志）并在事件上返回同一 id 是允许的——那不是「仅为填充字段而铸造」。 |
| 入站盖章 | 当 `turn.session_id` 非空时，可 resume 的 agent **SHOULD** 从第一条事件起在每条 stdout 事件上包含同一值（不做发现缓冲）。发现缓冲仅适用于新会话（`turn.session_id` 为 `""`）且 CLI 异步分配 id 的情形。 |
| 不缓冲 permission | agent **MUST NOT** 为等待会话 id 而缓冲 `permission_request`（或任何需要 stdin 响应的事件）。优先在入站 `turn.session_id` 存在时盖章，或在 id 已知前不带该字段发出请求。 |
| 不一致 | 在已观察到一个非空 `session_id` 之后出现**不同的**非空值，属于协议违规。bridge **SHOULD** 向 stderr 记警告，并 **MUST** 保留第一个非空值（fail-soft）。它 **MUST NOT** 发明 id。 |

线上的 `session_id` 是任意 JSON 字符串——**MAY** 含冒号、斜杠、空格或任何可 JSON 转义的字符。把每个会话存为 `<id>.jsonl` 的 SDK 历史助手施加了**存储级**约束：作为文件名组件不安全的 id 无法在文件存储中往返。持久化 id 的 bridge/SDK **SHOULD** 清洗或拒绝此类 id 并记 stderr 警告；线上本身不加限制。

### 终止事件上的可选 `usage`

`result` 与 `error` **MAY** 包含 `usage` 对象，用于 token/费用统计。bridge **MAY** 忽略它。推荐键（均可选）：`input_tokens`、`output_tokens`、`total_tokens`（数字）。额外键向前兼容，无法识别时 **SHOULD** 忽略。**没有**独立的 `usage` 事件类型。

### `{"type":"partial"}` —— 流式分块

```json
{"type":"partial","text":"hello ","role":"output","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `type` | string | 字面量 `"partial"`。 |
| `text` | string | 分块文本。可含换行、emoji 等——按需 JSON 转义。 |
| `role` | string | 可选。`"output"`（默认）或 `"thinking"`。让 agent 区分助手输出与推理/思考文本。bridge **MAY** 以不同方式渲染 thinking（例如折叠、灰显）但 **MUST** 转发。未知值原样转发。 |
| `session_id` | string | 见[事件上的 `session_id`](#事件上的-session_id)。 |

agent **MAY** 包含额外字段（向前兼容）；bridge **SHOULD** 忽略它不理解的字段。

当 profile 设了 `streaming: false` 时，bridge 忽略所有 `partial` 事件，仅从 `result` 事件拼装回复。

### `{"type":"result"}` —— 终止性成功正文

```json
{"type":"result","text":"Here is the complete answer.","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c","usage":{"input_tokens":12,"output_tokens":34}}
```

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `type` | string | 字面量 `"result"`。 |
| `text` | string | 本 turn 的最终回复正文。当完整正文已通过 `partial` 投递时 **MAY** 为 `""`（适当时 bridge 跳过重复的最终发送）。 |
| `session_id` | string | 见[事件上的 `session_id`](#事件上的-session_id)。 |
| `usage` | object | 可选。见[终止事件上的可选 `usage`](#终止事件上的可选-usage)。 |

**每个 turn 至多一条 `result`。** 若出现第二条 `result`，bridge **SHOULD** 记警告并 **MUST** 忽略它。

**拼装用户可见正文：**

- 当 `streaming: true`：用户可见正文是已转发 `partial` 文本的拼接。若 `result.text` 非空且未转发任何 `partial`，bridge 以 `result.text` 作为正文。若已转发一条或多条 `partial`，bridge **MUST NOT** 再次追加 `result.text`（仅将其视为 `usage` / 完整性的终止元数据——许多 CLI 会在自身的终止事件中重复完整拼装文本）。
- 当 `streaming: false`：正文仅为 `result.text`（所有 `partial` 事件都被忽略）。

未产生 `result`（且未产生 `partial`）但退出 `0` 的 turn 是**成功**的 turn。仅通过 `partial` 流式输出并退出 `0`、且无 `result` 的 turn 也是成功的（用户可见正文就是已转发的 partial）。有 `usage` 要发布的 agent **SHOULD** 仍发出一条尾随的 `{"type":"result","text":"",...}`，以便统计有归属。空输出只有在搭配非零退出码或 `error` 事件时才是失败。

本 turn 中第一条 `error` 之后，bridge **MUST** 将 turn 视为失败；后续 `error` 或 `result` 事件 **SHOULD** 被忽略。`partial` 事件上的 `usage` **MUST** 被忽略（bridge **MAY** 警告）。

### `{"type":"error"}` —— 错误事件

```json
{"type":"error","message":"Upstream API rate limited. Try again in 60s.","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `type` | string | 字面量 `"error"`。 |
| `message` | string | 用户可读的错误消息。 |
| `session_id` | string | 见[事件上的 `session_id`](#事件上的-session_id)。当 agent 有会话时存在（包括常见的「CLI `result` 带 `is_error`」情形），以便 bridge 在失败 turn 后仍能持久化连续性。 |
| `usage` | object | 可选。见[终止事件上的可选 `usage`](#终止事件上的可选-usage)。 |

此事件**无论** `streaming` 模式都被尊重。bridge 把消息作为错误回复转发给用户，并 **MUST** 停止转发同一 turn 的后续 `partial` 事件（已投递的分块不被撤回，仅抑制后续——见下文「与已投递 partial 的交互」）。`error` 之后到达的 `result` 事件 **MUST** 被丢弃（它不能贡献给失败 turn 的回复正文）。

一旦发出 `error` 事件，bridge **MAY** 完全停止读取 agent 的 stdout——它已捕获错误及（若存在）会话 id。agent 进程预期随后很快退出；若不退出，bridge 的正常超时生效。

若发出 `error` 事件，bridge **MUST** 将 turn 视为失败，即使进程退出 0。agent **SHOULD** 在发出 `error` 后以非零码退出，但 bridge **MUST NOT** 依赖这一点——`error` 事件本身足以标记 turn 失败。

**SDK 约定。** 封装本协议的 SDK（例如官方 Python 与 Node 的 `create_profile`/`createProfile` 助手）把 `send_error()` 视为**终止性**：agent **SHOULD NOT** 在调用它之后再发 `partial` 或 `result`。SDK **MAY** 通过在 `send_error()` 后立即退出来强制。这比原始协议更严格（协议允许 agent 发 `error` 后继续写——bridge 只是丢弃余下内容），但这是推荐的 SDK 习惯，因为把错误与后续内容混在一起会令用户困惑。

#### 与已投递 partial 的交互

当 `partial` 事件在 `error` 到达前已转发（常见的「答到一半撞上上游限流」情形），bridge **MUST NOT** 试图撤回、编辑或标注已投递分块——它们已展示给用户。`error` 消息原样投递，在所有 partial 之后。

这意味着用户可能看到半截回复后跟一个错误。这是有意的：在大多数消息平台上撤回已投递文本不可行，尝试这样做（删除 + 重发）既竞争又意外。想弱化这一点的 bridge **MAY** 在最后一段 partial 与错误消息之间插入可见分隔（例如换行或「—」线），但 **MUST NOT** 改写或删除 partial 文本。

偏好干净失败的 agent **MAY** 选择缓冲输出，直到确信 turn 会成功才发 `partial`——但那样就失去了流式，这是权衡。

### 格式错误的行

不是合法 JSON、是合法 JSON 但非对象、或缺少受认可 `type` 的 stdout 行属于协议违规。bridge **SHOULD** 记 stderr 警告并**忽略该行**。它**不**作为回复正文转发给用户——0.4 中回复正文仅由 `result`（以及流式时的实时 `partial`）携带。（这比 0.2 更严格，0.2 把任何无前缀的行视为正文。代价是手写的 `echo "hello"` shell agent 不再是合法 agent；见[设计理由](#设计理由)。）

### `max_reply_chars` —— 长度上限（两种模式都适用）

`max_reply_chars`（默认 8000）限制用户可见输出的总字符长度：

- **非流式（`streaming: false`）：** `result.text` 正文在投递前截断为 `max_reply_chars` 个字符。
- **流式（默认）：** 跟踪所有已转发 `partial` 分块的累计长度。一旦达到 `max_reply_chars`，bridge 追加截断提示（profile 的 `truncation_suffix`）并停止转发当前 turn 的后续 partial。

**实现自定义：边界分块。** 当一个 `partial` 分块跨越上限（分块本身大于剩余预算）时，bridge **MAY** 选择 (a) 先转发填满剩余预算的尾段截断切片，再发截断提示；或 (b) 整体丢弃该分块，仅转发截断提示。两者均合规。截断提示是告知用户回复已被封顶的标志；用户是否看到那片挤得下的切片，是各 bridge 的 UX 选择。

上限统一施加，使得在 profile 中设 `max_reply_chars: 2000` 对流式 agent（`partial`）和单正文 agent（`result`）效果相同。缓冲所有输出并以 `result` 发出的 agent，或把一切作为 `partial` 转发的 agent，都撞同一堵墙。

### 完整示例

**流式，每条事件都带会话 id（可 resume 的 agent）：**

```
{"type":"partial","text":"Here is the first part of the answer. ","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
{"type":"partial","text":"And here is the second part.","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
{"type":"result","text":"","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c","usage":{"input_tokens":12,"output_tokens":34}}
```

**一次性带会话（正文仅在 `result` 中）：**

```
{"type":"result","text":"Here is the complete answer.","session_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479"}
```

**无状态 agent（无 `session_id` 字段）：**

```
{"type":"result","text":"Here is the complete answer."}
```

**思考 + 输出，流式：**

```
{"type":"partial","role":"thinking","text":"Let me consider the options... ","session_id":"cli-sess-…"}
{"type":"partial","role":"output","text":"The answer is 42.","session_id":"cli-sess-…"}
{"type":"result","text":"","session_id":"cli-sess-…"}
```

**流中出错（仍携带会话）：**

```
{"type":"partial","text":"Let me look that up... ","session_id":"cli-sess-…"}
{"type":"error","message":"Upstream API rate limited. Try again in 60s.","session_id":"cli-sess-…"}
```

**多附件 turn（bridge 写入 stdin）：**

```
{"type":"turn","message":"compare these two","session_id":"","from_user":"u1",
 "attachments":[{"kind":"image","url":"https://.../a.png"},{"kind":"image","url":"https://.../b.png"}],
 "permission":false,"protocol_version":"0.4"}
```

**turn 中可选授权**（profile `permission: true`；bridge 保持 stdin 打开）：

```
{"type":"partial","text":"I'll create that file.","session_id":"cli-sess-…"}
{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo ok > f.txt"},"session_id":"cli-sess-…"}
```

用户在消息 UI 批准后，bridge 写入 stdin：

```
{"type":"permission_response","request_id":"1","behavior":"allow"}
```

agent 继续，然后结束：

```
{"type":"partial","text":"Done.","session_id":"cli-sess-…"}
{"type":"result","text":"","session_id":"cli-sess-…"}
```

---

## 可选工具授权

本节是**可选的**。一致的 bridge 或 agent **MAY** 完全忽略它。profile 默认 `permission: false`。没有 turn 中批准通道的 CLI（或仅支持无人值守运行的 bridge）继续使用自动批准标志如 `--dangerously-skip-permissions` 或 `--yolo`——那仍是有效且预期的部署模式。

### 这是什么（以及不是什么）

- **是：** turn 进行中**工具执行授权**的通道——agent（或被封装的 CLI）在运行 Bash、Write 等之前需要 allow/deny。
- **不是：** 通用的 human-in-the-loop 问答协议。澄清问题应放在正常的 `result` / `partial` 内容中；用户在**下一** IM turn 回答。因此在无头 IM bridge 中禁用交互式问卷工具（例如 Claude Code 的 `AskUserQuestion`）是合理且推荐的。

### 启用

当 profile 设 `permission: true` 时：

1. bridge **MUST** 在 [turn 对象](#输入--stdin-turn-对象)中设置 `permission: true`。
2. bridge **MUST** 保持 agent 的 stdin 打开，直到进程退出或 bridge 超时（见 [stdin / EOF 合约](#stdin--eof-合约)）。
3. bridge **MUST** 识别 `{"type":"permission_request"}` 事件并 **MUST** 向 stdin 写入匹配的 `{"type":"permission_response"}` 对象。
4. 封装带原生控制协议的 CLI 的 agent（例如 Claude Code `--permission-prompt-tool stdio` 发出 `control_request` / 接受 `control_response`）在 agent 进程内于该协议与这些帧之间翻译。AgentProc 不要求每个底层 CLI 都讲 `control_request`；只有选择启用的 agent 需要翻译层。

当 `permission` 缺省或 `false` 时，bridge **MUST NOT** 要求 agent 讲这些帧，且 **MUST NOT** 仅为 permission 流量保持 stdin 打开。

### `{"type":"permission_request"}` —— agent → bridge（stdout）

```json
{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo ok"},"description":"Write a file","session_id":"cli-sess-…"}
```

必填字段：

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `type` | string | 字面量 `"permission_request"`。 |
| `request_id` | string | 此请求的不透明 ID。**MUST** 在 turn 内唯一。匹配的响应 **MUST** 回显同一 ID。**MUST NOT** 含空格、控制字符或换行。 |
| `tool_name` | string | 工具或动作名（例如 `Bash`、`Write`）。 |
| `input` | object | 工具参数，JSON 对象（**MAY** 为空 `{}`）。 |

agent **MAY** 为 UI / 策略包含的可选字段：

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `description` | string | 给消息 UI 的简短人类可读摘要。 |
| `tool_use_id` | string | 底层 CLI / 模型的工具使用 ID（若有）。 |
| `session_id` | string | 见[事件上的 `session_id`](#事件上的-session_id)。一旦已知 **SHOULD** 包含；**MUST NOT** 为等待它而延迟请求。 |

遇到格式错误的事件（无效 JSON 或缺必填字段），bridge **SHOULD** 记 stderr 警告且 **MUST NOT** 阻塞 turn 等待用户决策；仅当仍能解析出 `request_id` 时 **SHOULD** 写一条 deny 响应，否则忽略该事件。

请求事件由 bridge 消费，**不**出现在用户可见的回复正文中。bridge 通常在消息平台上把它渲染为批准提示（按钮、回复键盘等）。

### `{"type":"permission_response"}` —— bridge → agent（stdin）

```json
{"type":"permission_response","request_id":"1","behavior":"allow"}
```

由 bridge 写入 agent 的 **stdin**，作为一行 NDJSON，随后 bridge 继续等待更多请求或进程退出。必填字段：

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `type` | string | 字面量 `"permission_response"`。 |
| `request_id` | string | **MUST** 匹配挂起的请求。 |
| `behavior` | string | `"allow"` 或 `"deny"`。 |

可选：

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `updated_input` | object | 当 `behavior` 为 `"allow"` 时，agent **SHOULD** 使用的输入——当 bridge 想覆盖或明确确认请求的 `input` 时。封装需要更新输入 blob 的 CLI 的 agent（例如 Claude Code `updatedInput`）**MUST** 透传。allow 时若响应省略 `updated_input`，agent **MUST** 回退到请求的原始 `input`——bridge **不**会代为填充。当上游批准者未提供时，bridge **MUST NOT** 用请求的原始 `input` 预填 `updated_input`：这样做会抹掉下游 CLI 「用户明确接受未修改」与「用户根本没动过」之间的区别。 |
| `message` | string | 当 `behavior` 为 `"deny"` 时，agent **MAY** 转给模型或用户的原因。 |

### 顺序、阻塞与超时

- agent **MAY** 在一个 turn 中发出多个 permission 请求（顺序或与其他事件交错）。每个未决 `request_id` 需要各自的响应。
- 发出 `permission_request` 后，agent（或被封装的 CLI）通常**阻塞**该工具调用直到匹配响应到达。AgentProc bridge **MUST NOT** 在请求未答时关闭 stdin，除非 turn 超时 / 进程死亡。
- **`timeout_secs` 仍适用于整个 turn。** 若用户在消息 UI 始终不批准，bridge 的正常超时触发（SIGTERM → 宽限 → SIGKILL）。超时且有挂起 permission 请求时，bridge **SHOULD** 在 stdin 仍可写时优先发带超时 `message` 的 deny 响应，然后继续正常 kill 序列——但 **MUST NOT** 为等用户而挂起超过 `timeout_secs`。
- bridge **MAY** 施加更短的 permission 专属等待；若如此，**MUST** deny（或 kill）而非让 agent 无限阻塞。

### 与其他事件的交互

- `partial` / `result` **MAY** 在同一 turn 的 permission 流量前后出现。
- `error` 仍使 turn 失败。挂起的 permission 请求作废；bridge **SHOULD** 停止等待用户批准。
- 当 turn 已有已知的 `session_id` 时，`permission_request` 事件 **SHOULD** 包含它（见[事件上的 `session_id`](#事件上的-session_id)）。agent **MUST NOT** 为等待会话发现而延迟发出 `permission_request`。

### 与自动批准模式的关系

若底层 CLI 无 stdio permission 提示（或 profile 留 `permission: false`），支持的选项仍是：

- CLI 自动批准 / skip-permissions / yolo 标志
- 通过 CLI 标志或配置预先允许特定工具
- 对 agent 进程沙箱化，在沙箱内接受完全自动批准

可选 permission 不替代这些模式；它是 agent 与 bridge 双方启用时的另一选择。

---

## stdin / EOF 合约

- 当 `permission` 缺省或 `false` 时，bridge 写入恰好一行——[turn 对象](#输入--stdin-turn-对象)——随后 EOF。agent 读一行、解码，此后 **MUST NOT** 阻塞于 stdin。
- 当 `permission: true` 时，bridge 写入 turn 行**不带** EOF；随后写入零或多个 `{"type":"permission_response"}` 行；仅在进程退出或 bridge 结束 turn（超时 / kill）时关闭 stdin。

当 `permission` 不为 `true` 时，agent 在读完 turn 行后 **MUST NOT** 阻塞于 stdin。

---

## 退出码

| 码 | 含义 |
|------|---------|
| `0` | 成功——stdout 内容（由 `result` 与已转发 `partial` 事件拼装）作为回复发送 |
| `1` | 通用 agent 错误 |
| `124` | 超时（bridge 施加；匹配 GNU `timeout` 约定） |
| `130` | 被 SIGINT（Ctrl-C）中断 |
| `143` | 被 SIGTERM 终止 |

其他非零码视为通用错误。当 `send_error_reply: true` 且进程非零退出（且未发 `error` 事件）时，bridge 向用户发送一条通用错误消息。

### 多个失败信号并存时的优先级

一个 turn 可能产生不止一个失败信号——例如 agent 发出 `error` 后 bridge 在它退出前因超时 kill 它，或 agent 发出 `error` 后非零退出。bridge 按以下优先级（从高到低）决定最终退出码：

1. **超时（124）** —— bridge kill 了进程。超时始终报为 `124`，不论 agent 在 kill 前发了什么。
2. **`error` 事件（1）** —— agent 发出了错误事件。即使进程随后退出 0 也报为 `1`。
3. **进程退出码** —— 以上都不适用时使用。

理由：超时是 agent 无法恢复的 bridge 级失败，故优先。`error` 是 agent 自身「出错了」的信号，优先于原始退出码（因为 agent 可能在发出 `error` 后因自诊断原因退出 0）。

stderr 作为调试日志捕获，不展示给用户，除非 `include_stderr_in_reply: true`。

---

## 超时处理

当到达 `timeout_secs` 而进程未退出时：

1. bridge 向进程发 `SIGTERM`。
2. bridge 等待 `kill_grace_secs`（默认 5）让进程退出。
3. 若仍在运行，bridge 发 `SIGKILL`。

已收到的 `partial` 事件转发给用户。bridge 随后发送超时错误回复（受 `send_error_reply` 约束）。

agent **SHOULD** 通过刷新任何缓冲的 partial 输出并 promptly 退出来处理 `SIGTERM`。

**Windows 注意事项。** `SIGTERM` 与 `SIGKILL` 在 Windows 上不作为可投递信号存在。Windows 上的 bridge **MUST** 仍尊重两步意图——先「礼貌」终止请求（Windows 上 `TerminateProcess` 是唯一可用杠杆，故宽限期坍缩为零），然后在 `kill_grace_secs` 后进程仍存活时硬终止。POSIX bridge 实现完整 SIGTERM → 宽限 → SIGKILL 序列。需要在关闭时刷新的 agent 无法在 Windows 上依赖收到信号，**SHOULD** 改用 `atexit` 式钩子或显式的退出前刷新纪律。

---

## 设计原则

**1. 进程边界是唯一契约。**
bridge 不关心 agent 用什么语言编写、调用什么 AI 模型、如何管理状态。任何从 stdin 读 turn 并向 stdout 写 NDJSON 事件的进程都是合法 agent。

**2. agent 中不含 bridge 逻辑。**
agent 进程不需要知道消息平台的任何事。它读 turn、做事、写事件。平台相关事项（投递、限流、会话存储）是 bridge 的责任。

**3. 会话 ID 不透明。**
bridge 存储并转发会话 ID，但从不解释它。agent 进程拥有其会话 ID 的含义。

**4. 工作单元是一个 turn。**
每条用户消息派生一个进程。agent 不被期望为长驻守护。（长驻守护不在范围；见下文「与相关协议的比较」。）

**5. `type:` 快捷方式不属于本协议。**
内置快捷方式（例如 `type: claude-code`）是平台扩展，不是 P0。实现可提供它们，但不在本规范范围。

---

## 设计理由

**为什么 stdout 用 NDJSON，而不是带哨兵前缀的行？**

0.2 用带哨兵前缀的纯文本（`AGENT_PARTIAL:...`），好让手写 bash agent（`echo "You said: $AGENT_MESSAGE"`）是合法 agent。代价有三：(a) 冲突规则——回复正文必须避免以 `AGENT_*:` 开头的行；(b) 编码不对称——`AGENT_PARTIAL:` 携带 JSON 编码字符串而最终正文是纯文本，同一逻辑「文本块」有两种编码；(c) `partial` 载荷是裸字符串，无余地放元数据（例如区分思考与输出）。

0.3 让每个 stdout 行成为带 `type` 字段的 JSON 对象。这移除了冲突规则（正文在 0.4 中是 `{"type":"result"}`；在 0.3 中曾是 `{"type":"text"}`），统一了编码（`partial` 与终止性正文事件都携带 `text` 字符串字段），并让 `partial` 能长出 `role` 等字段。代价是裸 `echo "hello"` 不再是合法 agent——它必须发 JSON 事件。真实 agent 都是封装脚本（每个 hub profile 都是围绕底层 CLI 的 Python 或 Node 封装，而底层 CLI 内部已经在发 NDJSON），所以它们本来就在做 JSON；唯一失去的是 5 分钟 bash 冒烟测试，而一段 3 行的 Python/Node 脚本即可替代。

**为什么输入用 stdin turn 对象，而不是环境变量？**

0.2 把每轮请求放在 env 变量里（`AGENT_MESSAGE`、`AGENT_SESSION_ID`……）。每个输入字段都是 env 变量，密钥也是 env 变量——于是输入通道和密钥通道是同一通道。这种混同有硬上限：env 变量不能携带结构，故多附件（`AGENT_ATTACHMENTS`）被起草又移除，因为「env 里塞 JSON 破坏了 bash echo agent 的承诺」。协议的能力上限被 bash echo agent 钉死了。

0.3 按用途分离三条输入路径：

- **stdin** —— 动态的每轮请求（turn 对象）。携带任意结构：`attachments` 数组、嵌套字段、JSON 能表达的任何东西。
- **argv** —— 通过 `{{MESSAGE}}` / `{{SESSION_ID}}` / `{{PROFILE_DIR}}` 注入启动参数，供把消息作为 CLI 参数传给底层 CLI 的 agent 使用。
- **env** —— 密钥与配置（profile `env` 块），刻意留在 env 中，使其不作为 turn 载荷被记录。

可调试性几乎不变：`AGENT_MESSAGE="hello" ./agent.sh` 变成 `echo '{"type":"turn","message":"hello","session_id":"","from_user":"u1","protocol_version":"0.4"}' | ./agent`——仍是一行，只是不再是 env 赋值。

**为什么去掉 `env_inherit`？**

0.2 加了 `env_inherit: minimal|all`，让安全默认（`minimal`）可被 `all` 逃逸，以照顾依赖环境 shell 变量的遗留 profile。实践中这个逃生舱让信任边界始终模糊。0.3 把子进程基础 env 固定为始终是 infra 集；profile 需要的环境变量必须在 `env` 块声明。这让「agent 看到的」等于「profile 声明的」加固定 infra 集——更干净的边界。0.2 中设 `env_inherit: all` 的 profile 必须声明它们依赖的变量。

**为什么用事件上的 `session_id` 字段，而不是 `session` 事件？**

0.3 从 0.2 的 `AGENT_SESSION:` 行前缀继承了独立的 `{"type":"session"}` 事件（以及「最后一行生效」规则）。那种形态把会话连续性当作离散事件，是错误的抽象：id 是 turn 级元数据。编码 CLI 已经在自己的 NDJSON 事件上附着 `session_id` / `sessionID` / `thread_id`（常常从首次 `init` / `thread.started` 起）；hub bridge 却在发明第二种事件，只为了再声明一次。

0.4 把 `session_id` 放在事件本身上。bridge 持久化**第一个**非空值；agent 一旦已知 **SHOULD** 将其附着到后续事件。早期事件 **MAY** 省略它，以免延迟流式与 turn 中的 `permission_request`。之后出现不同的非空值属于协议违规（fail-soft：保留第一个）。无状态 agent 完全省略该字段；它们 **MUST NOT** 铸造底层工具无法用来恢复的 id（生成 CLI 作为 `--session` 输入所需的 id 是可以的）。当 `turn.session_id` 已非空时，agent **SHOULD** 从第一条事件起盖章——无需等待发现。

**为什么用 `result` 而不是 `text`？**

`text` 听起来像「又一段正文碎片」，会诱使发出多条拼接行，从而使 turn 级元数据（`session_id`、`usage`）变得含糊。`result` 是终止性成功结果——每 turn 至多一条——与许多 CLI 已发出的 `result` 形态事件对齐，并为可选 `usage` 提供自然归属。

**为什么除非零退出码外还要 `error` 事件？**

退出码告诉 bridge *出了*错，但不告诉该对用户*说什么*。`error` 事件让 agent 转发有意义、用户可读的错误消息（例如「API key 过期」「限流；60 秒后重试」），而非 bridge 的通用模板。

**为什么用可选 permission 帧而非通用 HIL？**

消息 bridge 已经给了用户下一 turn。澄清问题属于回复正文。无头编码 CLI 在 turn 中独特需要的是**工具授权**（在某 Bash/Write 运行前 allow）。那映射到 Claude Code 的 stdio `control_request` / `control_response`——而非 AskUserQuestion。把通道设为可选（`permission: true`）让无批准提示的 CLI 或部署仍可用自动批准 / `--dangerously-skip-permissions` / `--yolo`。

---

## 与相关协议的比较

AgentProc 占据特定定位。邻近协议在*形状*上相似（子进程 + stdio），但在*目的*上不同。

### MCP — Model Context Protocol（Anthropic）

MCP 把一个 LLM 应用（客户端）连接到**工具与数据源**（服务端，子进程）。传输：JSON-RPC 2.0 over stdio 或 HTTP+SSE。

**与 AgentProc 的关系：** **方向相反。** MCP 中 AI 是客户端、工具提供方是子进程。AgentProc 中 bridge 是客户端、AI 封装是子进程。它们自然组合：一个 AgentProc agent 可在内部使用 MCP 工具。

- 规范：https://modelcontextprotocol.io/

### ACP — Agent Client Protocol（Zed Industries）

ACP 把代码编辑器连接到 AI 编码 agent。传输：JSON-RPC 2.0 over stdio，双向，长生命周期。

**与 AgentProc 的关系：** **更丰富的表亲，职责不同。** ACP 假定一个带工具调用、文件 diff、模式切换的交互式 IDE 会话。AgentProc 假定每次进程调用一个聊天 turn。构建 IDE 用 ACP；把聊天机器人桥接到 CLI 用 AgentProc。

重叠只是表面的。ACP 客户端必须实现文件系统、终端、权限回调，因为 IDE 拥有用户正在编辑的文件；AgentProc bridge 不持有用户文件、不渲染 diff。反之，ACP 不提供无人值守运行语义——没有超时、没有 `SIGTERM`/`SIGKILL` 宽限、没有「agent 出错时告知用户」契约——因为 IDE 用户手动停止失控 agent。消息 bridge 无人值守运行，故这些对 AgentProc 是承重的，对 ACP 不在范围。即便底层 CLI 恰好 ACP 兼容（例如 Zed 通过 ACP 驱动 Claude Code），在 ACP 客户端之上构建 IM bridge 也是过度工程：bridge 会实现它从不用的能力，却仍缺失聊天场景所需的超时/错误回复保证。AgentProc 的契约——stdin 上的 turn 对象、stdout 上的 NDJSON 事件、每 turn 一个进程——是适合 bridge→CLI 这份工作的最小契约。

- 规范：https://agentclientprotocol.com/

### NDJSON / JSON Lines

NDJSON 是每行一个 JSON 对象、换行分隔。它是 Claude Code、Codex、Gemini CLI 流式模式以及 MCP 内部使用的线上格式。

**与 AgentProc 的关系：** **0.3 起同为线上格式（0.4 细化）。** AgentProc 双向都是 NDJSON：stdin 上一行一个 JSON 对象（turn，其后可选 permission 响应），stdout 上也是（事件）。与裸 NDJSON 的区别在于固定、小的事件词汇表（0.4 中为 `turn` / `partial` / `result` / `error` / `permission_request` / `permission_response`）以及每 turn 一进程的生命周期，而非长生命周期双向 RPC 流。

- 规范：https://jsonlines.org/

### SSE — Server-Sent Events（WHATWG）

SSE 通过 HTTP 流 `event:` / `data:` 行。

**与 AgentProc 的关系：** **`partial` 的语义祖先。** 「换行终止、带类型判别的事件」这一模式借自 SSE，去掉 HTTP 传输并采用固定字段集。0.3 的 `{"type":"partial","text":"..."}` 是同一思想的 JSON 对象形式。

- 规范：https://html.spec.whatwg.org/multipage/server-sent-events.html

### LSP / DAP — Language Server / Debug Adapter Protocols（Microsoft）

LSP 与 DAP 把编辑器连接到语言服务器或调试器。传输：JSON-RPC 2.0 over stdio，带 `Content-Length: N` 帧。

**与 AgentProc 的关系：** **帧对比。** LSP 用字节长度前缀帧（允许二进制载荷，需解析器）。AgentProc 用换行分隔帧（仅文本，解析平凡）。这一权衡是有意的。

- 规范：https://microsoft.github.io/language-server-protocol/ / https://microsoft.github.io/debug-adapter-protocol/

### Unix filter 约定

POSIX 衍生的「从 stdin 读、向 stdout 写、成功退出 0」约定——在 Eric Raymond 的 *The Art of Unix Programming* 中形式化。

**与 AgentProc 的关系：** **哲学基础。** AgentProc 用 filter 没有的两样东西扩展了 Unix filter 约定：会话连续性交接（事件上的 `session_id`）和流式事件（`{"type":"partial"}`）。其余都是普通 Unix。0.3 起 filter 变成「JSON 行进、JSON 行出」而非裸文本，但形状仍是 filter。

- 参考：http://www.catb.org/~esr/writings/taoup/html/ch01s06.html

### AgentProc *不是*什么

- **不是机器人框架。** Hubot、Errbot、BotKit、Microsoft Bot Framework 在 bridge 的*消费*侧运作（进程内适配器、HTTP 连接器）。AgentProc 定义 bridge 与 agent *之间*的契约，与这些框架正交。
- **不是 agent 间协议。** A2A / AGNTCY 解决不同问题（agent 互相对话）。
- **不是 IDE 协议。** 用 ACP。
- **不是工具协议。** 用 MCP。

---

## 从 0.3 迁移

线协议 0.4 是**硬切换**（与 0.2 → 0.3 姿态相同）。没有双读过渡期。

| 0.3 | 0.4 |
|-----|-----|
| `{"type":"session","id":"..."}` | 移除。按需将 `session_id` 放在 `partial` / `result` / `error` / `permission_request` 上。 |
| `{"type":"text","text":"..."}`（可重复；拼接） | 单条 `{"type":"result","text":"..."}`。额外正文分块 → `partial`。 |
| 会话「最后一行生效」 | 持久化**第一个**非空 `session_id`；之后不同值 = 违规（保留第一个）。 |
| `protocol_version`：`"0.3"` | `"0.4"` —— 仅当 stdout 词汇表匹配 0.4 时 bump。 |

仍发出或期望 `type:session` / `type:text` 的 bridge 不合 0.4。在 0.4 bridge 下，这些行是未知 `type`，按[格式错误的行](#格式错误的行)处理（警告 + 忽略）——它们**不**贡献会话 id 或回复正文。不透明版本的 fail-soft（「未识别版本 → 按未设置处理」）**并不**意味着 0.3 agent 在 0.4 bridge 下仍可用。

此前仅从 CLI 终止性 `result` 事件读取 `session_id` 的 hub 封装 **SHOULD** 也从最早携带它的 CLI 事件读取（例如 `system/init`），以免流式必须等到进程退出。它们 **MUST NOT** 仅为满足会话标记而缓冲整个 turn 的 `partial`，也 **MUST NOT** 为等待 id 而缓冲 `permission_request`。

---

## 变更日志

文档修订在此追踪。线协议 bump 显式标出；其余条目除非注明均为编辑性。

- **wire 0.4 / doc 1.1** —— 破坏性 stdout 形态变更。移除 `{"type":"session"}` 与 `{"type":"text"}`。会话连续性改为 stdout 事件上的可选 `session_id` 字段：bridge 持久化第一个非空值；agent 一旦已知 **SHOULD** 附着；早期省略允许；之后冲突值属违规（保留第一个）。永不铸造工具无法用来恢复的 id；输出上永不使用 `""`。最终成功正文为单条 `{"type":"result","text":...}`（可选 `usage`）。流式正文拼装：已转发的 `partial` 优先于重复的 `result.text`。相对 0.3 硬切换（见[从 0.3 迁移](#从-03-迁移)）。0.3 的「最后会话事件生效」理由废止。
- **wire 0.3 / doc 1.0** —— 双向 NDJSON。输入：stdin 上单个 [turn 对象](#输入--stdin-turn-对象)取代所有 `AGENT_*` 环境变量；密钥/配置留在 env；argv 占位符不变。输出：stdout 现为按 `type` 字段区分的 NDJSON 事件（`partial` / `text` / `session` / `error` / `permission_request`），取代 `AGENT_*:` 哨兵前缀。`partial` 新增可选 `role`（`output` | `thinking`）。附件收并为 turn 对象中单个 `attachments` 数组（每个元素 `{kind, url, ...}`），取代 0.2 的 `AGENT_IMAGE_URL` / `AGENT_FILE_URL` 单附件便利变量——不再有单/多双重表示。会话 ID 线上改为任意 JSON 字符串（字符集限制移至存储级关注）。Profile 变更：`command` 始终是 argv[0] 且永不拆分（移除 `args` 缺省时按空格拆分的简写；`args` 默认 `[]`）；移除 `stdin` 字段（stdin 始终携带 turn）；`streaming` 变为 bridge 侧提示而非线上字段；移除 `env_inherit`（子进程基础 env 始终是 infra 集）。格式错误的 stdout 行被记日志并忽略，而非作为回复正文。事件词汇表声明为封闭，以抵御向 ACP 式更丰富事件的漂移。这是从 0.2 的硬切换；runner 不支持两者并存。
- **wire 0.2 / doc 0.9** —— 安全默认的子进程环境继承。新增 profile 字段 `env_inherit: minimal|all`（默认 `minimal`）。继承与 `env_allowlist` 解耦：allowlist 仅 gate `${VAR}` 展开；完整 `process.env` / `os.environ` 继承需显式 `env_inherit: all`。SDK 包 bump 至 0.6.1；线协议保持 `0.2`。
- **wire 0.2 / doc 0.8** —— 可选工具授权通道：profile `permission: true`、env `AGENT_PERMISSION`、stdout `AGENT_PERMISSION_REQUEST:<json>`、stdin `AGENT_PERMISSION_RESPONSE:<json>`，以及保持 stdin 打开的 turn 规则。仅可选启用；默认 profile 与自动批准 / skip-permissions 部署不变。线协议字符串变为 `0.2`，因为新的协议行前缀与 turn 中 stdin 帧上了线。
- **doc 0.7** —— `env_allowlist` 现为真正的信任边界，而非装饰性 `${VAR}` 过滤器。当 `env_allowlist` 存在时，agent 进程不再继承 bridge 的完整环境；其 env 由最小 infra 集（`PATH`/`HOME`/`TERM`/…，在规范中枚举）+ profile `env` 块 + `AGENT_*` + CLI `--env` 附加构成。此前子进程整体继承 bridge env，故 bridge 持有的任何密钥都泄漏给 agent，无视 allowlist——与「收窄信任边界」的声称矛盾。`${VAR}` 阻断与警告行为不变。`env_allowlist` 缺省时保留向后兼容的完整继承行为。SDK 版本 bump 至 0.5.2（Python 与 Node）；线协议保持 `0.1`。跨实现一致性覆盖扩展到 SDK 入口（`create_profile` / `createProfile`），通过新 `spec/conformance/sdk.json` 夹具：两个 SDK 现以子进程运行相同的返回类型 / `send_partial` / `send_error` / `ProtocolError` 场景并断言一致的 stdout + 退出码。
- **doc 0.5** —— 定义空 `AGENT_MESSAGE` 语义（有附件时合法）。消歧 `command`/`args`：`args: []`（显式空）现表示「不拆分」，区别于 `args` 缺省。为 profile `env` 块新增 `${VAR}` 安全警告。新增可选 `env_allowlist` profile 字段：存在时，不在列表中的 `${VAR}` 引用展开为空 + stderr 警告，把信任边界从完整环境收窄到所声明变量。将 `AGENT_ERROR:` 与已投递 partial 的交互（不撤回）形式化，并明确 bridge 在错误后 MAY 停止读 stdout。重述会话 ID 格式约束（无空格/控制/冒号）并定义违规时 bridge 行为（忽略该行、保留先前 id、警告）。将退出码优先级（timeout > `AGENT_ERROR:` > 退出码）形式化。记录 SDK `send_error` 的终止性。移除未用的 `session_line_prefix` profile 字段——bridge 硬编码 `AGENT_SESSION:` 且该字段从未被读。
- **doc 0.4** —— 头部将线协议版本（`0.1`）与文档修订号分开；新增「版本治理」章节，明确 `AGENT_PROTOCOL_VERSION` 是不透明且不可比较的字符串。将 `AGENT_ATTACHMENTS` 从草案提升至 P0，并加上 bridge 同时设置两层变量时的一致性要求。澄清 session 行顺序：当 CLI 同时输出 `AGENT_SESSION:` 与 `AGENT_ERROR:`（`result{is_error}` 的常见形态）时，bridge **MUST** 为下一轮保留 session ID，即便当前这一轮作为失败上报。`AGENT_ERROR:` → bridge **MUST** 视为失败，不论退出码（原为 SHOULD）。把 `command` 定义为 argv[0]、`args` 为其余 argv，并加上引号规则，让含空格路径仍能在不走 shell 的前提下表达。补上 SIGTERM/SIGKILL 超时合约的 Windows 注意事项。
- **0.1.0** — 首个公开草案。定义了环境变量输入、哨兵前缀 stdout、`AGENT_SESSION:` / `AGENT_PARTIAL:` / `AGENT_ERROR:`、session 行「最后一行生效」规则、`AGENT_PROTOCOL_VERSION`、`AGENT_ATTACHMENTS`（草案）、超时/SIGTERM 合约、退出码约定、stdin EOF 合约、命令执行不走 shell 规则。
