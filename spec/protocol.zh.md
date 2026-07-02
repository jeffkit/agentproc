# AgentProc 协议规范

**线协议（Wire protocol）：** `0.1`（注入为 `AGENT_PROTOCOL_VERSION` 的字符串）
**文档修订：** `0.7`
**状态：** 草案

线协议与本文档**独立编号**。线协议版本仅在 stdin/stdout 上的字节发生变化时才更新；文档修订号追踪不影响一致 agent 或 bridge 收发内容的编辑性更新——例如措辞澄清、新增指引。实现者在读取 `AGENT_PROTOCOL_VERSION` 时应遵循下方的[版本治理](#版本治理)规则。

---

## 版本治理

`AGENT_PROTOCOL_VERSION` 是一个**不透明字符串**，不是可比较的数字。agent 与 bridge **MUST NOT** 对它进行排序、比较或范围检查。两个字符串要么相等，要么不相等。

- 如果 bridge 注入了一个 agent 不识别的版本字符串，agent **SHOULD** 按变量未设置处理（best-effort，fail-soft）。
- 如果 agent 期望某个 bridge 未注入的版本字符串，agent **MUST** 回退到其内置默认值。
- 该字符串**不是**能力发现机制：不存在协商、不存在能力声明、也不存在排序。agent 若需要知道某项具体能力（例如图片附件）是否存在，**MUST** 直接检查对应的 env 变量（例如 `AGENT_IMAGE_URL` 非空），而不是检查版本字符串。

理由：任何可比较的版本号都会诱导实现者用 `>= 0.2` 来 gate 行为，而一旦某个 bridge 没有同步 bump 数字，这种判断就会失效。把字符串视为不透明，能让契约保持诚实：某项能力的存在由承载它的 env 变量来表示。

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
env_allowlist: [MY_API_KEY]   # 可选：限制 env 块能通过 ${VAR} 读取哪些变量

# 输出控制
timeout_secs: 600             # stdout 读取超时，默认 1800
kill_grace_secs: 5            # SIGTERM → SIGKILL 的宽限期，默认 5
max_reply_chars: 8000         # 回复最大字符数，超出后截断，默认 8000
truncation_suffix: "\n\n…(输出已截断)"
include_stderr_in_reply: false
send_error_reply: true        # agent 出错时是否通知用户

# 流式回复
streaming: true               # 实时转发 AGENT_PARTIAL: 行
```

### 占位符

`args`、`cwd` 和 `env` 值中的占位符在进程启动前替换，**不经过 shell**。

| 占位符 | 值 |
|--------|-----|
| `{{MESSAGE}}` | 用户消息文本 |
| `{{SESSION_ID}}` | 上一轮返回的 session ID（空 = 新会话） |
| `{{SESSION_NAME}}` | 会话可读名称 |
| `{{PROFILE_DIR}}` | 包含 profile YAML 的目录的绝对路径。让 profile 引用打包脚本（例如 `command: python3 {{PROFILE_DIR}}/bridge.py`），与 agent 的 `cwd` 解耦。bridge 在按路径调用 profile 时设置它；未设置时（例如无文件编程式调用）展开为空字符串。 |

### `env` 值中的 `${VAR}` 展开

profile `env` 块中的值可以用 `${VAR}` 语法引用 bridge 自身的环境变量（例如 `MY_API_KEY: "${MY_API_KEY}"`）。这是**对 bridge 进程的完整环境进行替换**，而不是对 profile 或 agent。

**安全含义。** profile 是**可信输入**——能写 profile 的人就能读取 bridge 能访问到的每一个环境变量（云凭证、token、密钥）。这是设计使然（profile 是配置，不是用户输入），但有一条实操后果值得点明：

> **不要运行来源不受信任的 profile。** `agentproc hub run <name>` 会从 GitHub 仓库拉取 profile 并运行。如果你不信任该仓库维护者拿到你 shell 环境的全部内容，就不要运行他们的 profile。设置了 `env_allowlist`（见下文）的 profile 能把这个边界缩小到它声明的那几个变量——但没有 allowlist 时默认仍是全环境可读，因此信任决策仍由运行 profile 的用户承担。

bridge 用 POSIX shell 语义展开 `${VAR}`：未知变量展开为空字符串，而不是字面值 `${VAR}`。

### `env_allowlist` —— 缩小信任边界

默认情况下，`env` 块里的每个 `${VAR}` 都对 bridge 的完整环境展开，**并且 agent 进程会继承 bridge 的完整环境**。`env_allowlist` 让 profile 把这个边界缩小到它真正需要的变量——既管 `${VAR}` 展开，*也管* 子进程到底能看到哪些变量：

```yaml
env:
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
env_allowlist: [ANTHROPIC_API_KEY]
```

- **可选且 opt-in。** `env_allowlist` 缺省（默认）时，所有 `${VAR}` 引用照常展开，且子进程继承 bridge 的完整环境——即既有的「信任 profile」行为。现有 profile 无需改动。
- **当其存在时，有两件事改变：**
  1. 名字**不**在列表里的 `${VAR}` 展开为空字符串，并向 stderr 记录一条警告（例如 `env_allowlist blocked ${AWS_SECRET_ACCESS_KEY}; expanded to empty`）。进程仍会启动——值或列表里的拼写错误表现为空变量 + 一条警告，而非硬失败。
  2. **子进程不再整包继承 bridge 的环境。** 它的环境由：一组最小 **infra** 集（见下）+ profile `env` 块（allowlist 过滤后）+ 注入的 `AGENT_*` 变量 + CLI `--env` 额外项 构成。bridge 手上、但 profile 未声明的任何密钥都不会到达 agent。没有这第二条规则，`env_allowlist` 只是个 `${VAR}` 展开的化妆品过滤器，而其它所有环境变量照样通过继承泄漏——因此这条对「信任边界」的承诺是承重墙。
- **infra 集。** 当 `env_allowlist` 存在时，bridge 从自身环境拷贝以下名字（若已设置）到子进程：`PATH`、`HOME`、`USER`、`LOGNAME`、`SHELL`、`LANG`、`LC_ALL`、`LC_CTYPE`、`LC_MESSAGES`、`TERM`、`TMPDIR`、`TZ`、`PWD`，以及 Windows 上的 `SystemRoot`、`TEMP`、`TMP`、`USERPROFILE`、`USERNAME`、`PATHEXT`、`COMSPEC`、`APPDATA`、`LOCALAPPDATA`、`PROGRAMDATA`、`NUMBER_OF_PROCESSORS`、`PROCESSOR_ARCHITECTURE`、`OS`。这些是 agent 找解释器/临时目录/locale 所需的运行变量——都不承载凭证。profile 若需要 bridge 持有的额外非密钥变量（如自定义 `WORKSPACE_DIR`），必须在 `env` 块中声明并加入 `env_allowlist`。
- **作用域。** `env_allowlist` 既管 profile `env` 块内的 `${VAR}` 展开，**也管**（当其存在时）子进程的基础环境继承。它不影响 `{{MESSAGE}}` / `{{SESSION_ID}}` / `{{PROFILE_DIR}}` 占位符、bridge 注入的 `AGENT_*` 变量，也不影响根本不含 `${VAR}` 的 `env` 值。
- **不支持通配。** 名字必须精确匹配。`["ANTHROPIC_*"]` 匹配不到 `ANTHROPIC_API_KEY`——请逐一列出全名。这让 allowlist 保持为显式声明，而不是一种可能悄然放大的模式。
- **建议。** Hub profile **SHOULD** 设置 `env_allowlist`，这样 `agentproc hub run <name>` 就只暴露 profile 实际需要的凭证，而不是用户 shell 环境的全部。从第三方仓库拉取的 profile 即便没设 allowlist 也不一定是恶意的，但用户无从判断它读了什么——设上列表正是 profile 作者证明「我只碰我声明的变量」的方式。有了上面的继承规则，「声明的」现在才真正等于「agent 能看到的」。

### 命令执行模型

bridge 从两个字段组装 agent 的 argv：

- **`command`** —— 可执行文件（argv[0]）。把它当作单个 token。如果它包含空格，bridge **MUST** 把整个字符串原样作为 argv[0]，**MUST NOT** 切分。
- **`args`** —— YAML 列表，承载额外的 argv token（argv[1..]）。每个列表元素是一个 argv token，原样使用。

组装出的 argv（`[command, *args]`）传给操作系统的 `execve`（或等价函数），**不调用 shell**。这样能避免通过 `{{MESSAGE}}` 占位符发起的 shell 注入，也让 `command` 能承载带空格的路径。

**遗留简写。** 许多现有 profile 写出多 token 的 command 字符串（例如 `command: python3 {{PROFILE_DIR}}/bridge.py`），不写 `args`。bridge **MUST** 支持这种简写：当 `args` **缺失**且 `command` 含空格时，bridge 按空格切分 `command` 得到 argv。

**`args` 字段是信号。** bridge 通过 `args` 字段的**存在与否**（而非其内容）来决定是否切分：

- `args` **缺失**（profile 中没有这个 key） → 按空格切分 `command`（简写）。
- `args` **存在**（即使是空数组 `[]`） → `command` 是单个 argv token，**绝不**切分。

也就是说 `args: []` 是**有意义的**：它告诉 bridge「不要切分我的 command」。这是给「`command` 含空格但应被视为单个 token」的逃生口。

**引号 / 带空格路径。** 需要调用路径含空格的可执行文件的 profile **MUST** 使用显式形式：

```yaml
command: "/path with spaces/my agent"
args: []                       # 告诉 bridge：不要切分 command
```

或者，如果还需要额外的 argv token：

```yaml
command: "/path with spaces/my agent"
args: ["--flag", "{{MESSAGE}}"]
```

YAML 的双引号标量承载空格；bridge 把字符串作为单个 argv token 传给 `execve`。同样的规则也适用于任何包含空格的 `args` 元素。

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
| `AGENT_MESSAGE` | 用户消息文本。**可为空**——见下方「空消息」。 |
| `AGENT_SESSION_ID` | 上一轮返回的 session ID（空字符串 = 新会话） |
| `AGENT_SESSION_NAME` | 会话可读名称（默认 `"default"`） |
| `AGENT_FROM_USER` | 发送者标识符（平台相关：用户 ID、handle 等） |
| `AGENT_STREAMING` | `"1"` = 流式模式，`"0"` = 单次模式 |
| `AGENT_PROTOCOL_VERSION` | 协议版本字符串，例如 `"0.1"`。**不透明且不可比较**——见[版本治理](#版本治理)。agent **MUST NOT** 对它排序或范围检查。 |

#### 空消息

`AGENT_MESSAGE` **MAY** 是空字符串。当以下任一为真时，视为该轮「携带内容」：

- `AGENT_MESSAGE` 非空
- `AGENT_IMAGE_URL` 非空
- `AGENT_FILE_URL` 非空

若以上都不成立，则该轮为空，bridge / agent **SHOULD** 上报错误而非继续。这条规则适配常见的「仅图片消息」场景：用户发了一张截图但没附文字。

### 附件变量（P0）

附件通过单附件便捷变量传递。bridge 设置与消息匹配的变量；agent 读取非空的那一个。

| 变量名 | 说明 |
|--------|------|
| `AGENT_IMAGE_URL` | 图片附件 URL。当消息恰好含一张图片时设置。 |
| `AGENT_FILE_URL` | 文件附件 URL。当消息恰好含一个文件时设置。 |

多附件变量（`AGENT_ATTACHMENTS`，一个 JSON 数组）曾作为草案，但从未接入 runner——没有任何符合规范的 bridge 真的发送过它，而且「JSON 塞进 env」破坏了 bash `echo` agent 的合法承诺，因此已移除。等真实 bridge 确实需要携带多个附件时，spec 会重新引入一种手写 shell agent 仍能消费的投递机制。

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
- **与 `AGENT_ERROR:` 的交互** —— CLI 的终止事件经常同时携带 session ID 和错误指示（例如 `result{session_id, is_error: true}`）。当同一对话中同时出现 `AGENT_SESSION:` 行和 `AGENT_ERROR:` 行时，bridge **MUST** 仍然为下一轮保留 session ID，即便当前这一轮作为失败上报给用户。错误终止这一轮；它不会使 session 失效。已经知道 session ID 的 agent 在发出 `AGENT_ERROR:` 时 **SHOULD** 先发出 `AGENT_SESSION:` 行（或任意位置——无论如何 bridge 都按「最后一行生效」处理）。

session ID 字符串是**不透明的**——bridge 原样存储和转发，MUST NOT 解释其格式。它可以是 UUID、CLI 内部句柄，或任何短的不透明 token。它 **MUST NOT** 含空白、控制字符或冒号（`:`）：冒号与 `AGENT_SESSION:` 的分隔符冲突，而空白/控制字符会破坏其在 env 变量和 argv 中的往返传递。如果 agent 输出的 `AGENT_SESSION:` 行的值违反此规则，bridge **SHOULD** 向 stderr 记录警告并**忽略该行**（保留之前已捕获的 session id；若此前未捕获过，则 session 保持为空）。合法的 id 在去除首尾空白后非空，且匹配 `^[A-Za-z0-9._~=-]+$`——即 URL 安全的 base64url 字母表（`A-Z`、`a-z`、`0-9`、`-`、`_`）加上 `.`、`~`、`=`。该集合**刻意排除 `/` 和 `+`**：两者都出现在标准 base64 中，但 `/` 会使 id 不能安全地用作文件名组成部分——SDK 的历史记录辅助函数把每个 session 存为 `<id>.jsonl`，含 `/` 的 id（如 `../../tmp/x`）会从 sessions 目录路径穿越出去。输出其他内容的 agent 将无法往返传递。

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

这一行**无论** `streaming` 是否开启都会被识别。bridge 将解码后的字符串作为错误回复转发给用户，并停止转发该轮后续的 `AGENT_PARTIAL:` 行（见下方「与已交付 partial 的交互」——已交付的分块不会被撤回，只会抑制后续的）。

一旦输出了 `AGENT_ERROR:` 行，bridge **MAY** 直接停止读取 agent 的 stdout——错误已经捕获，且（按 session 行规则）若 error 之前出现过 session id 也已拿到最终值。预期 agent 进程会很快退出；若没有，bridge 的常规超时机制生效。（包装 NDJSON 输出 CLI 的 hub bridge 即采用此做法：输出 `AGENT_ERROR:` 后让进程自行收尾，而不是继续解析一个后续事件已不影响用户可见结果的流。）

如果出现了 `AGENT_ERROR:` 行，bridge **MUST** 视这一轮为失败，即便进程退出码为 0。agent 在输出 `AGENT_ERROR:` 后 **SHOULD** 以非零码退出，但 bridge **MUST NOT** 依赖这一点——`AGENT_ERROR:` 行本身就足以标记这一轮失败。

与 `AGENT_ERROR:` 同时产生的回复正文会被丢弃。

#### 与已交付 partial 的交互

当 `AGENT_ERROR:` 之前已经转发了 `AGENT_PARTIAL:` 行（常见的「流式输出一半，然后撞上上游限流」场景），bridge **MUST NOT** 试图撤回、编辑或注释已交付的分块——它们已经展示给用户了。`AGENT_ERROR:` 文本按原样交付，跟在所有 partial 之后。

这意味着用户可能看到半截回复后跟一个错误。这是有意的：大多数消息平台无法撤回已交付文本，硬要这么做（删除 + 重发）既竞态又令人困惑。想要柔化这一点的 bridge **MAY** 在最后一条 partial 与错误消息之间插入可见分隔符（例如换行或「—」分隔线），但 **MUST NOT** 改写或删除 partial 文本。

偏好「干净失败」的 agent **MAY** 选择缓冲输出，直到确信这一轮会成功才输出——但这样就放弃了流式，是个权衡。

### 回复正文

所有**不是**协议行的 stdout 内容构成最终回复正文，在进程退出后发送给用户。

如果所有内容已通过 `AGENT_PARTIAL:` 发出，回复正文为空时 bridge 自动跳过最终发送。

进程产出空回复正文且退出码为 `0`，是**成功**的一轮（例如 agent 把所有内容都通过 `AGENT_PARTIAL:` 转发后退出）。空回复正文只有在伴随非零退出码或 `AGENT_ERROR:` 行时才算失败。

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

### 多个失败信号并存时的优先级

一轮可能产生多个失败信号——例如 agent 输出了 `AGENT_ERROR:` 然后被 bridge 在退出前因超时杀掉，或者 agent 输出了 `AGENT_ERROR:` 后又非零退出。bridge 按以下优先级（从高到低）决定最终退出码：

1. **超时（124）** —— bridge 杀掉了进程。无论杀掉前 agent 输出过什么，超时一律上报为 `124`。
2. **`AGENT_ERROR:`（1）** —— agent 输出了错误行。即便进程随后以 0 退出，也上报为 `1`。
3. **进程退出码** —— 上述都不适用时，采用进程返回的码。

理由：超时是 agent 无法恢复的 bridge 级失败模式，所以优先。`AGENT_ERROR:` 是 agent 自己发出的出错信号，优先于裸退出码（因为 agent 可能出于自诊断原因以 0 退出后输出 `AGENT_ERROR:`）。

stderr 作为调试日志记录，不发给用户（除非 `include_stderr_in_reply: true`）。

---

## 超时处理

当达到 `timeout_secs` 而进程未退出时：

1. bridge 向进程发送 `SIGTERM`。
2. bridge 等待 `kill_grace_secs`（默认 5 秒）让进程退出。
3. 如仍运行，bridge 发送 `SIGKILL`。

在此之前已经收到的 `AGENT_PARTIAL:` 行仍然转发给用户。然后 bridge 发送一条超时错误回复（受 `send_error_reply` 控制）。

agent SHOULD 处理 `SIGTERM`——刷新任何缓冲的 partial 输出并尽快退出。

**Windows 注意。** `SIGTERM` 与 `SIGKILL` 在 Windows 上并不是可投递的信号。运行在 Windows 上的 bridge **MUST** 仍然遵循两步意图——先发一个「礼貌」终止请求（在 Windows 上 `TerminateProcess` 是唯一可用的手段，因此宽限期塌缩为零），然后在 `kill_grace_secs` 之后若进程仍存活则硬终止。POSIX bridge 实现完整的 SIGTERM → 宽限 → SIGKILL 序列。需要在关闭时刷新的 agent 无法依赖在 Windows 上收到信号，**SHOULD** 改用 `atexit` 风格的钩子或在退出前显式刷新。

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

**与 AgentProc 的关系：****更丰富的表亲，但职责不同。** ACP 假设一个交互式 IDE 会话，包含工具调用、文件 diff、模式切换。AgentProc 假设每次进程调用对应一个聊天回合。如果你在构建 IDE，用 ACP；如果你在把聊天机器人桥接到 CLI，用 AgentProc。

重叠只是表象。ACP 客户端必须实现文件系统、终端、权限回调，因为 IDE 拥有用户正在编辑的文件；AgentProc 的 bridge 不拥有任何用户文件，也不渲染 diff。反过来，ACP 不提供无人值守的运行时语义——没有超时、没有 `SIGTERM`/`SIGKILL` 宽限、没有「agent 出错时通知用户」的合约——因为 IDE 用户会手动停掉失控的 agent。消息桥是无人值守运行的，所以这些语义对 AgentProc 是承重的，对 ACP 则超出范围。即便底层 CLI 恰好兼容 ACP（例如 Zed 通过 ACP 驱动 Claude Code），用 ACP 客户端来做 IM 桥也是过度工程：bridge 会实现一堆它根本用不到的能力，却仍然缺少聊天场景必需的超时/错误回复保证。AgentProc 的合约——环境变量进、哨兵前缀 stdout 出、一回合一进程——是刚好能装下「桥到 CLI」这件事的最小合约。

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

本文档修订在此追踪。线协议自首个草案起保持 `0.1` 不变；下方条目记录本文档的编辑性变更与澄清，并非线协议变更。

- **doc 0.7** —— `env_allowlist` 现在是真正的信任边界，而非 `${VAR}` 展开的化妆品过滤器。当 `env_allowlist` 存在时，agent 进程不再整包继承 bridge 的环境；其环境由一组最小 infra 集（`PATH`/`HOME`/`TERM`/……，规格中已枚举）+ profile `env` 块 + `AGENT_*` + CLI `--env` 额外项 构成。此前子进程整包继承 bridge 环境，因此 bridge 持有的任何密钥都会泄漏给 agent，allowlist 形同虚设——与「缩小信任边界」的承诺自相矛盾。`${VAR}` 拦截与警告行为不变。缺省 `env_allowlist` 仍保留兼容的整包继承行为。SDK 版本升至 0.5.2（Python 与 Node 同步）；线协议仍为 `0.1`。跨实现一致性覆盖面扩展到 SDK 入口（`create_profile` / `createProfile`）：新增 `spec/conformance/sdk.json` fixture，两 SDK 以子进程方式跑相同的返回类型 / `send_partial` / `send_error` / `ProtocolError` 场景，并断言一致的 stdout 与退出码。
- **doc 0.5** —— 定义空 `AGENT_MESSAGE` 的语义（有附件时合法）。澄清 `command`/`args`：显式空数组 `args: []` 表示「不要切分」，与 `args` 缺失区分。新增 profile `env` 块的 `${VAR}` 安全警告。新增可选的 `env_allowlist` profile 字段：当其存在时，不在列表里的 `${VAR}` 引用展开为空 + 一条 stderr 警告，把信任边界从完整环境缩小到声明的那几个变量。明确 `AGENT_ERROR:` 与已交付 partial 的交互（不撤回），并明确 bridge 在输出 error 后 MAY 直接停止读取 stdout。重申 session-id 格式约束（不含空白/控制字符/冒号），并定义违规时 bridge 的行为（忽略该行、保留上一个 id、记录警告）。明确退出码优先级（超时 > `AGENT_ERROR:` > 退出码）。记录 SDK `send_error` 的终止性。移除未使用的 `session_line_prefix` profile 字段——bridge 硬编码 `AGENT_SESSION:`，该字段从未被读取。
- **doc 0.4** —— 头部将线协议版本（`0.1`）与文档修订号分开；新增「版本治理」章节，明确 `AGENT_PROTOCOL_VERSION` 是不透明且不可比较的字符串。将 `AGENT_ATTACHMENTS` 从草案提升至 P0，并加上 bridge 同时设置两层变量时的一致性要求。澄清 session 行顺序：当 CLI 同时输出 `AGENT_SESSION:` 与 `AGENT_ERROR:`（`result{is_error}` 的常见形态）时，bridge **MUST** 为下一轮保留 session ID，即便当前这一轮作为失败上报。`AGENT_ERROR:` → bridge **MUST** 视为失败，不论退出码（原为 SHOULD）。把 `command` 定义为 argv[0]、`args` 为其余 argv，并加上引号规则，让含空格路径仍能在不走 shell 的前提下表达。补上 SIGTERM/SIGKILL 超时合约的 Windows 注意事项。
- **0.1.0** — 首个公开草案。定义了环境变量输入、哨兵前缀 stdout、`AGENT_SESSION:` / `AGENT_PARTIAL:` / `AGENT_ERROR:`、session 行「最后一行生效」规则、`AGENT_PROTOCOL_VERSION`、`AGENT_ATTACHMENTS`（草案）、超时/SIGTERM 合约、退出码约定、stdin EOF 合约、命令执行不走 shell 规则。
