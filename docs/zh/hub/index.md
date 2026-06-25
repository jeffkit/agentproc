# Profile Hub

主流 AI agent CLI 的开箱即用 AgentProc profile 合集。每个 profile 都把一个 CLI 包装成符合 AgentProc 协议的 agent，任何遵循协议的 bridge 都能直接驱动——不依赖任何 bridge 专属快捷方式，没有魔法。

Hub 存放在仓库的 [`hub/`](https://github.com/jeffkit/agentproc/tree/main/hub) 目录下。本页是入口；单个 profile 的详细文档直接在 GitHub 上阅读。

## 现有 profile

| Profile | CLI | 测试状态 | 语言 |
|---------|-----|---------|------|
| [claude-code](https://github.com/jeffkit/agentproc/tree/main/hub/claude-code) | `claude`（Anthropic） | official | Python · Node |
| [codex](https://github.com/jeffkit/agentproc/tree/main/hub/codex) | `codex`（OpenAI） | official | Python · Node |
| [codebuddy](https://github.com/jeffkit/agentproc/tree/main/hub/codebuddy) | `codebuddy`（腾讯） | official | Python · Node |
| [agy](https://github.com/jeffkit/agentproc/tree/main/hub/agy) | `agy` | community | Python · Node |
| [echo-agent](https://github.com/jeffkit/agentproc/tree/main/hub/echo-agent) | （无 CLI） | official | Python · Node · Bash |

`测试状态` 含义：

- **official** — AgentProc 维护者按 CLI 的官方行为验证过。
- **community** — 社区提交且报告可用，但维护者未端到端验证。
- **unverified** — 提交时未验证。

## 快速开始

1. 从上表挑一个 profile，在 GitHub 上打开它的 README。
2. 把 `profile.yaml` 和一个 bridge 脚本（`bridge.py` 或 `bridge.js`）复制到你的项目里。
3. 在 profile 里调整 `cwd:` 和必要的鉴权环境变量。
4. 把你的消息 bridge 指向这个 profile YAML。

例如要接入 `claude-code`：

```bash
cp hub/claude-code/profile.yaml     ./profile.yaml
cp hub/claude-code/bridge.py        ./bridge.py
# 修改 profile.yaml 里的 cwd: 指向你的项目
```

本地自检（不需要消息 bridge）：

```bash
AGENT_MESSAGE="hello" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="1" \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
python3 bridge.py
```

预期输出：

```
AGENT_PARTIAL:"Hi! How can I help?"
AGENT_SESSION:13c2f6ec-1f97-42c4-be9e-9475129e243c
```

## 一个 profile 里有什么？

```
hub/<name>/
├── profile.yaml         # AgentProc P0 profile（用 command:，不用 type:）
├── bridge.py            # Python bridge 脚本
├── bridge.js            # Node.js bridge 脚本
└── README.md            # 安装、用法、注意事项
```

bridge 脚本是真正把目标 CLI 的输出（NDJSON、纯文本等）翻译成 AgentProc 哨兵前缀 stdout 协议的部分。Python 和 Node 两个版本保持对等——挑适合你技术栈的。

## 设计原则

- **仅 P0。** 不使用 `type:` 快捷方式、不使用 `routing:` 块、不依赖任何 bridge 扩展。任何遵循协议的 bridge 都能跑。
- **一个目录一个 profile。** 想要多个变体（不同模型、不同提示词），复制并重命名目录即可。
- **双语言 bridge。** Python 和 Node 都维护到对等。Bash 仅 `echo-agent` 用（它是参考实现，不是真正的 CLI 包装）。
- **不含密钥。** Profile YAML 通过环境变量引用（`${ANTHROPIC_API_KEY}`），从不内嵌凭证。

## 贡献新 profile

1. 创建 `hub/<cli-name>/` 目录，包含 `profile.yaml`、`bridge.py`、`bridge.js`、`README.md`。
2. 在 profile 元数据里设置 `tested: unverified`，除非你已端到端验证过。
3. 在 [`hub/README.md`](https://github.com/jeffkit/agentproc/blob/main/hub/README.md) 的表格里加一行。
4. 提交 PR。维护者会评审、必要时实测，并相应升级 `tested` 等级。

仓库范围的约定见 [`CONTRIBUTING.md`](https://github.com/jeffkit/agentproc/blob/main/CONTRIBUTING.md)。

## Profile schema

```yaml
name: <kebab-case-id>           # 必填，与目录名一致
description: <一句话描述>
cli: <command-name>             # 被包装的可执行文件
cli_install: |                  # CLI 的安装方法
  npm install -g ...
agentproc:                      # 真正的 AgentProc P0 profile
  command: python3 ./bridge.py  # 或：node ./bridge.js
  cwd: ~/your-project
  timeout_secs: 600
  streaming: true
  env:
    API_KEY: "${API_KEY}"
tested: official | community | unverified
maintainer: <github-handle>
tags: [<分类>, ...]
notes: |                        # 可选：注意事项、坑
  ...
```

## 与 ilink-hub-bridge 的关系

AgentProc 脱胎于 [`ilink-hub-bridge`](https://github.com/jeffkit)——一个带内置 `type:` 处理器（支持 `claude-code`、`cursor`、`codebuddy-code` 等）的消息平台 bridge。在实际生产使用中，我们意识到 bridge↔agent 的协议本身可以独立复用——于是有了 AgentProc。

Hub 中的 profile 是那些 `type:` 处理器内部逻辑的**纯 P0 重写**。它们存在的意义是：让任何 bridge 都能驱动 `claude` / `codex` / `codebuddy` / `agy`，而不需要自己实现 type 处理器。如果你的 bridge 已经有 `type:` 快捷方式，你不需要这些 profile。
