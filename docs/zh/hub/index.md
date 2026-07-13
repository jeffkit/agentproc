# Profile Hub

Hub 是一组开箱即用的 AgentProc profile，覆盖主流 AI agent CLI。**不用 clone 仓库、不用复制文件、不用改 YAML**——`agentproc` CLI 按需从 GitHub 拉取 profile，本地缓存，直接运行。

## 一行命令上手

```bash
# 挑一个 profile、指定一个目录、就跑
cd ~/projects/my-app
agentproc hub run claude-code -p "what is this codebase?"
```

就是这样。CLI 会自动：

1. 首次使用时从 GitHub 拉 `hub/claude-code/`
2. 缓存到 `~/.agentproc/cache/hub/claude-code/`（24 小时 TTL）
3. 把**你的当前目录**作为 agent 的 `cwd`（可用 `--cwd` 覆盖）
4. 通过 `{{PROFILE_DIR}}` 占位符定位打包的 bridge 脚本——`cwd` 和脚本位置彻底解耦
5. 向 agent 的 stdin 写入一个 `{"type":"turn",...}` 对象，并转发你传的任何 `--env`

::: tip 遇到 GitHub 限流？
匿名拉取每个 IP 每小时 ~60 次。设置 token 可以提到 5,000 次/小时：

```bash
export GITHUB_TOKEN=$(gh auth token)   # 或任意 personal access token
```

设置 `GITHUB_TOKEN`（或 `GH_TOKEN`）后，CLI 会带 `Authorization: Bearer <token>`。如果你想完全绕开网络，可以用本地仓库：`agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"`。
:::

## 全部 hub 命令

| 命令 | 用途 |
|------|------|
| `agentproc hub list` | 列出 hub 里所有 profile |
| `agentproc hub show <name>` | 显示某个 profile 的 README |
| `agentproc hub run <name> [opts]` | 拉取（必要时）并运行某个 profile |
| `agentproc hub install <name>` | 把 profile 复制到当前目录（便于自己改） |

加 `--refresh` 强制从 GitHub 重新拉取。

## 现有 profile

| Profile | CLI | 测试状态 | 语言 |
|---------|-----|---------|------|
| [claude-code](https://github.com/jeffkit/agentproc/tree/main/hub/claude-code) | `claude`（Anthropic） | official | Python · Node |
| [codex](https://github.com/jeffkit/agentproc/tree/main/hub/codex) | `codex`（OpenAI） | official | Python · Node |
| [codebuddy](https://github.com/jeffkit/agentproc/tree/main/hub/codebuddy) | `codebuddy`（腾讯） | official | Python · Node |
| [agy](https://github.com/jeffkit/agentproc/tree/main/hub/agy) | `agy` | community | Python · Node |
| [recursive](https://github.com/jeffkit/agentproc/tree/main/hub/recursive) | `recursive`（自改进 Rust agent） | community | Python · Node |
| [echo-agent](https://github.com/jeffkit/agentproc/tree/main/hub/echo-agent) | （无 CLI） | official | Python · Node · Bash |

`tested` 含义：
- **official** — 维护者按 CLI 官方行为验证过
- **community** — 社区提交且报告可用，但维护者未端到端验证
- **unverified** — 提交时未验证

## 示例

### 浏览和试用

```bash
# 看看有什么
agentproc hub list

# 读它的文档
agentproc hub show claude-code

# 跑冒烟测试（不需要 API key）
agentproc hub run echo-agent -p "hello"
# → You said: hello
```

### 用真实的 CLI

```bash
cd ~/projects/my-app

# claude-code
agentproc hub run claude-code \
  -p "explain this codebase" \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"

# codex
agentproc hub run codex \
  -p "find the bug in src/auth.py" \
  --env OPENAI_API_KEY="$OPENAI_API_KEY"

# codebuddy（用它自己的登录）
agentproc hub run codebuddy -p "refactor this function"

# recursive（自改进 Rust agent；先用 `recursive init` 配置）
agentproc hub run recursive -p "find the bug in src/auth.rs"
```

### 多轮对话

```bash
agentproc hub run claude-code -p "what files are in this dir?" 2>/tmp/err.log
session=$(grep '^agentproc:session:' /tmp/err.log | cut -d: -f3)
agentproc hub run claude-code -p "now read src/main.py" --session "$session"
```

### 安装到本地编辑

如果想长期持有一个 profile 并自己改：

```bash
agentproc hub install claude-code
# → installed to: ./claude-code/

# 随意编辑 ./claude-code/profile.yaml
agentproc --profile ./claude-code/profile.yaml -p "hi" --cwd ./claude-code
```

## 缓存机制

- 缓存位置：`~/.agentproc/cache/hub/<name>/`
- TTL：24 小时（拉取后这段时间内直接用本地副本，不联网）
- 强制刷新：任何 hub 命令加 `--refresh`
- 每个 profile 是平铺目录：`profile.yaml`、`bridge.py`、`bridge.js`、`README.md`

CLI 用 GitHub 的 git-tree API（1 个请求拿到全部文件清单）+ raw.githubusercontent.com（无 rate limit），所以未鉴权用户也能保持流畅。

## Profile schema

```yaml
name: <kebab-case-id>           # 必填，与目录名一致
description: <一句话描述>
cli: <command-name>             # 被包装的可执行文件
cli_install: |                  # CLI 的安装方法
  npm install -g ...
agentproc:                      # 真正的 AgentProc P0 profile
  command: python3                          # argv[0]——单个 token，永不切分
  args: ["{{PROFILE_DIR}}/bridge.py"]       # argv[1..]；{{PROFILE_DIR}} 解析为 profile 自己所在目录
  # cwd 故意不写：`hub run` 默认用用户当前目录
  # （让被包装的 CLI 在用户项目里跑）。bridge 脚本通过
  # {{PROFILE_DIR}} 定位，与 cwd 无关。
  timeout_secs: 600
  streaming: true
  env:
    API_KEY: "${API_KEY}"       # 运行时解析的环境变量引用
tested: official | community | unverified
maintainer: <github-handle>
tags: [<分类>, ...]
notes: |                        # 可选：注意事项
  ...
```

Hub profile 是**纯 AgentProc P0**——不使用 bridge 专属的 `type:` 快捷方式。任何遵循协议的 bridge 都能驱动它们。

## 贡献新 profile

1. 在 [agentproc 仓库](https://github.com/jeffkit/agentproc)里创建 `hub/<cli-name>/` 目录，包含 `profile.yaml`、`bridge.py`、`bridge.js`、`README.md`。
2. 设置 `tested: unverified`，除非你已端到端验证过。
3. 在 [`hub/README.md`](https://github.com/jeffkit/agentproc/blob/main/hub/README.md) 的表格里加一行。
4. 提交 PR。维护者会评审、必要时实测，并相应升级 `tested` 等级。

仓库范围的约定见 [`CONTRIBUTING.md`](https://github.com/jeffkit/agentproc/blob/main/CONTRIBUTING.md)。

## 与 ilink-hub-bridge 的关系

AgentProc 脱胎于 [`ilink-hub-bridge`](https://github.com/jeffkit)——一个带内置 `type:` 处理器（支持 `claude-code`、`cursor`、`codebuddy-code` 等）的消息平台 bridge。在实际生产使用中我们意识到 bridge↔agent 协议本身可以独立复用，于是有了 AgentProc。

Hub 中的 profile 是那些 `type:` 处理器内部逻辑的**纯 P0 重写**。它们适用于任何 conformant bridge，不仅是某个特定实现。
