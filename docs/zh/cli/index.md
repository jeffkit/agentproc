# agentproc CLI

`agentproc` 命令行工具是 bridge 侧的标准 runner。它读取一个 profile YAML，按协议规范启动 agent 进程、解析 stdout、打印回复。任何符合协议的 agent——不管是 hub profile 还是你自己写的——都可以通过这个统一入口驱动。

CLI 和 Node SDK 在同一个 npm 包里：

```bash
npm install -g agentproc       # 全局安装
# 或：
npx agentproc ...              # 不装直接跑
```

## 两种调用方式

CLI 有两个等价入口：

| 入口 | 适用场景 |
|------|---------|
| `agentproc hub <subcommand>` | **推荐**。零本地文件运行官方 hub 里的 profile。CLI 首次使用时从 GitHub 拉取，缓存在 `~/.agentproc/cache/hub/<name>/`（24 小时 TTL），默认把你的当前目录作为 agent 的 `cwd`。 |
| `agentproc --profile <path>` | 运行本地已有的 profile YAML（你自己的、安装到本地的 hub profile、或仓库 checkout）。用 `--cwd` 控制 agent 的工作目录。 |

## 快速开始

```bash
# 冒烟测试，不需要 API key、不需要 clone：
agentproc hub run echo-agent -p "hello"
# → You said: hello

# 真实 agent，对当前目录操作：
cd ~/projects/my-app
agentproc hub run claude-code -p "explain this codebase" --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
```

驱动本地 profile（不联网拉取）：

```bash
git clone https://github.com/jeffkit/agentproc
cd agentproc

# echo-agent（{{PROFILE_DIR}} 让 cwd 不再重要）
agentproc --profile hub/echo-agent/profile.yaml --prompt "hello"

# claude-code——用 --cwd 指向你的项目，让 claude 在那跑
agentproc --profile hub/claude-code/profile.yaml \
          --prompt "explain this codebase" \
          --cwd /path/to/your/project \
          --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
```

::: tip GITHUB_TOKEN 可以提高速率限制
匿名 hub 拉取每个 IP 每小时 ~60 次。设置 token 可提到 5,000 次/小时：

```bash
export GITHUB_TOKEN=$(gh auth token)   # 或任意 personal access token
```

如果想完全绕开网络，可以用本地仓库跑：`agentproc --profile ./hub/<name>/profile.yaml ...`。
:::

## Hub 子命令

| 命令 | 用途 |
|------|------|
| `agentproc hub list` | 列出 hub 里所有 profile |
| `agentproc hub show <name>` | 显示某个 profile 的 README |
| `agentproc hub run <name> [opts]` | 拉取（必要时）并运行某个 profile |
| `agentproc hub install <name>` | 把 profile 复制到 `./<name>/` 用于本地编辑 |

`hub run` 接受和 `--profile` 一样的 runner 选项（见下），但有一个便利：不传 `--cwd` 时，默认用你的当前目录（让被包装的 CLI 在你所在的项目里跑）。

任何 hub 命令都可以加 `--refresh` 强制从 GitHub 重新拉取。

## 用法

```
agentproc --profile <path.yaml> --prompt "hello" [options]
```

### 必填（仅 `--profile` 模式）

| 选项 | 说明 |
|------|------|
| `--profile`, `-p <path>` | Profile YAML 路径 |
| `--prompt <text>` | 用户消息（或用 `--stdin`） |

::: warning 关于 `-p`
在 `--profile` 模式下，`-p` 是 `--profile` 的短形式。在 `hub run` 模式下，由于 profile 是按名字（positional 参数）而不是路径指定，`-p` 被复用为 `--prompt` 的短形式。这是唯一一个短形式在两种模式间不同的选项——不确定时用长形式。
:::

### 会话

| 选项 | 说明 |
|------|------|
| `--session <id>` | 上一轮的 session id，用于多轮续接 |
| `--session-name <name>` | 会话可读名称（默认 `default`） |
| `--from <user>` | 发送者标识符 |

### 执行

| 选项 | 说明 |
|------|------|
| `--cwd <path>` | 覆盖 `profile.cwd` |
| `--env KEY=VALUE` | 额外注入的环境变量（可重复） |
| `--timeout <secs>` | 覆盖 `profile.timeout_secs` |
| `--no-stream` | 关闭流式（忽略 `{"type":"partial"}` 事件） |

### 输出

| 选项 | 说明 |
|------|------|
| `--verbose` | 把协议行打到 stderr（默认） |
| `--quiet` | 不打协议行 |
| `--raw` | 不解析 stdout，原样转发 agent 输出 |
| `--stdin` | 从 stdin 读 prompt（而不是 `--prompt`） |

### 其他

| 选项 | 说明 |
|------|------|
| `--version` | 打印版本并退出 |
| `--help`, `-h` | 显示帮助 |

## 输出语义

### 默认模式

| 流 | 内容 |
|----|------|
| stderr | 实时 NDJSON 事件（`{"type":"partial"}`、`{"type":"session"}`、`{"type":"error"}`） |
| stdout | 最终回复正文（由 `{"type":"text"}` 事件拼接），在 agent 退出后打印 |
| 退出码 | `0` 成功 · `1` 错误 · `124` 超时（按 spec） |

最终的 session id 也会以 `agentproc:session:<id>` 的形式打到 stderr 末尾，便于 shell 脚本捕获：

```bash
output=$(agentproc -p prof.yaml --prompt "hi" 2>/tmp/err.log)
session=$(grep '^agentproc:session:' /tmp/err.log | cut -d: -f3)
agentproc -p prof.yaml --prompt "follow up" --session "$session"
```

### `--raw` 模式

完全不解析 stdout——原样转发 agent 的 stdout。适用于把 agent 的原始输出 pipe 给别的工具，或调试 bridge 脚本本身。

```bash
agentproc -p prof.yaml --prompt "hi" --raw | some-other-tool
```

## 示例

### 用 claude-code 多轮对话

```bash
# 第一轮——捕获 session id
agentproc -p hub/claude-code/profile.yaml \
          --prompt "what is this codebase?" \
          --cwd ~/projects/myapp \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
          2>/tmp/err.log
session=$(grep '^agentproc:session:' /tmp/err.log | cut -d: -f3)

# 第二轮——续接会话
agentproc -p hub/claude-code/profile.yaml \
          --prompt "tell me more about the auth module" \
          --cwd ~/projects/myapp \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
          --session "$session"
```

### 从 stdin 读 prompt

```bash
echo "what files are in this directory?" | \
  agentproc -p hub/claude-code/profile.yaml --cwd . --stdin
```

### 安静模式（stdout 干净，便于管道）

```bash
agentproc -p hub/claude-code/profile.yaml --prompt "summarize" --quiet | jq .
```

## 它如何实现协议

CLI 是 SDK `run()` 函数（[`sdk/node/src/runner.js`](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js)）之上的薄薄一层。那个模块是 AgentProc bridge 侧合约的标准参考实现：

- **profile 解析**：同时支持顶层形式（`command:` 在根）和 hub 形式（`command:` 嵌套在 `agentproc:` 下）。
- **占位符替换**：`args`、`cwd`、`env` 中的 `{{MESSAGE}}`、`{{SESSION_ID}}`、`{{SESSION_NAME}}`——不走 shell。
- **Turn 输入（stdin）**：向 agent 的 stdin 写入一行 `{"type":"turn",...}` NDJSON（`message`、`session_id`、`session_name`、`from_user`、`attachments`、`permission`、`protocol_version`），随后 EOF——除非 `permission: true`，此时 stdin 保持打开以收 `{"type":"permission_response"}` 帧。单轮请求**不**走环境变量。
- **env 注入**：profile 的 `env` 块（`${VAR}` 展开受 `env_allowlist` 约束），外加固定的 infra 集合（`PATH`/`HOME`/`TERM`/…）。`--env KEY=VALUE` 追加运行期额外变量。
- **stdout 分类**：每一行是按 `type` 派发的 JSON 对象——`{"type":"session"}`（最后一行生效）、`{"type":"partial"}`（`streaming: true` 时转发）、`{"type":"text"}`（回复正文，按序拼接）、`{"type":"error"}`（使本轮失败）。非 JSON / 未知 `type` 的行记日志并忽略。
- **超时处理**：SIGTERM → `kill_grace_secs`（默认 5 秒）→ SIGKILL。退出码 124。
- **退出码**：0 成功 · 1 错误（包括出现 `{"type":"error"}` 事件的情况）· 124 超时。

如果你在用别的语言写自己的 bridge，[`runner.js`](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js) 就是协议的代码化形式。
