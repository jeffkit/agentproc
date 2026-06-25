# agentproc CLI

`agentproc` 命令行工具是 bridge 侧的标准 runner。它读取一个 profile YAML，按协议规范启动 agent 进程、解析 stdout、打印回复。任何符合协议的 agent——不管是 hub profile 还是你自己写的——都可以通过这个统一入口驱动。

CLI 和 Node SDK 在同一个 npm 包里：

```bash
npm install -g agentproc       # 全局安装
# 或：
npx agentproc ...              # 不装直接跑
```

## 快速开始

```bash
agentproc --profile hub/echo-agent/profile.yaml --prompt "hello"
# → You said: hello
```

驱动 hub 里的真实 CLI profile：

```bash
git clone https://github.com/jeffkit/agentproc
cd agentproc

# echo-agent（不需要 API key，用于冒烟测试）
agentproc --profile hub/echo-agent/profile.yaml \
          --prompt "hello" --cwd hub/echo-agent

# claude-code（需要 ANTHROPIC_API_KEY）
agentproc --profile hub/claude-code/profile.yaml \
          --prompt "explain this codebase" \
          --cwd /path/to/your/project
```

## 用法

```
agentproc --profile <path.yaml> --prompt "hello" [options]
```

### 必填

| 选项 | 说明 |
|------|------|
| `--profile`, `-p <path>` | Profile YAML 路径 |
| `--prompt <text>` | 用户消息（或用 `--stdin`） |

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
| `--no-stream` | 设置 `AGENT_STREAMING=0` |

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
| stderr | 实时协议行（`AGENT_PARTIAL:`、`AGENT_SESSION:`、`AGENT_ERROR:`） |
| stdout | 最终回复正文（非协议行），在 agent 退出后打印 |
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
- **env 注入**：`AGENT_MESSAGE`、`AGENT_SESSION_ID`、`AGENT_SESSION_NAME`、`AGENT_FROM_USER`、`AGENT_STREAMING`、`AGENT_PROTOCOL_VERSION`，外加 `profile.env` 里的 `${VAR}` 展开。
- **stdout 分类**：`AGENT_SESSION:`（最后一行生效）、`AGENT_PARTIAL:`（JSON 容错模式）、`AGENT_ERROR:`，其余 = 回复正文。
- **stdin 合约**：当 `profile.stdin: message` 时，写完消息后发送 EOF。
- **超时处理**：SIGTERM → `kill_grace_secs`（默认 5 秒）→ SIGKILL。退出码 124。
- **退出码**：0 成功 · 1 错误（包括出现 `AGENT_ERROR:` 行的情况）· 124 超时。

如果你在用别的语言写自己的 bridge，[`runner.js`](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js) 就是协议的代码化形式。
