---
layout: home

hero:
  name: AgentProc
  text: 把任意 Agent CLI 接入任意消息平台
  tagline: 一个极简的进程级协议。不用 HTTP、不用 socket——只用 stdin、stdout 和环境变量。
  actions:
    - theme: brand
      text: 快速开始
      link: /zh/guide/getting-started
    - theme: alt
      text: 协议规范
      link: /zh/spec/

features:
  - icon: 🚀
    title: 5 分钟装好
    description: 装 CLI，指向一个 hub profile，开始和你的 agent 对话。不用跑服务器，不用学框架。
  - icon: 🤖
    title: 支持 claude、codex、codebuddy
    description: 你已经在用的 AI CLI 都有开箱即用的 profile。从 Profile Hub 挑一个，跑一行命令。
  - icon: 🔌
    title: 接入任意消息平台
    description: 微信、Slack、Telegram、Discord——bridge 把 AgentProc 适配到你的用户所在的任何平台。
  - icon: 📜
    title: 开放规范，零锁定
    description: 一页协议，Node 和 Python 都有参考实现。5 分钟读完，半天能自己实现一遍。
---

<div class="get-started">

# 5 分钟上手

## ① 安装 CLI

::: code-group

```bash [npm]
npm install -g agentproc
```

```bash [pipx]
pipx install agentproc
```

```bash [pip]
pip install agentproc
```

:::

验证可用：

```bash
agentproc --version
# agentproc 0.2.0 (protocol 0.1)
```

## ② 选一个 profile

浏览 [Profile Hub](/zh/hub/)——每个 profile 是一个目录，包含 `profile.yaml`、bridge 脚本和 README。官方首批 5 个 profile：

| Profile | CLI | 状态 |
|---------|-----|------|
| [claude-code](https://github.com/jeffkit/agentproc/tree/main/hub/claude-code) | `claude`（Anthropic） | official |
| [codex](https://github.com/jeffkit/agentproc/tree/main/hub/codex) | `codex`（OpenAI） | official |
| [codebuddy](https://github.com/jeffkit/agentproc/tree/main/hub/codebuddy) | `codebuddy`（腾讯） | official |
| [agy](https://github.com/jeffkit/agentproc/tree/main/hub/agy) | `agy` | community |
| [echo-agent](https://github.com/jeffkit/agentproc/tree/main/hub/echo-agent) | （hello world） | official |

## ③ 跑起来

先 clone 仓库，用 `echo-agent` 试一下（不需要 API key）：

```bash
git clone https://github.com/jeffkit/agentproc
cd agentproc

agentproc --profile hub/echo-agent/profile.yaml \
          --prompt "hello" \
          --cwd hub/echo-agent
# → You said: hello
```

然后跑真实的 CLI。以 `claude-code` 为例，支持流式输出和多轮会话续接：

```bash
agentproc --profile hub/claude-code/profile.yaml \
          --prompt "what is this codebase?" \
          --cwd ~/projects/my-app \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
```

stderr 上会实时看到协议行，stdout 是最终回复：

```
AGENT_PARTIAL:"This codebase is..."
AGENT_SESSION:13c2f6ec-1f97-42c4-be9e-9475129e243c
agentproc:session:13c2f6ec-1f97-42c4-be9e-9475129e243c
```

捕获 session id，继续对话：

```bash
agentproc --profile hub/claude-code/profile.yaml \
          --prompt "tell me about the auth module" \
          --cwd ~/projects/my-app \
          --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
          --session 13c2f6ec-1f97-42c4-be9e-9475129e243c
```

## ④ 接到你的消息平台

AgentProc agent 不直接和微信或 Slack 通信——那是 **bridge** 的工作。bridge 是一个小程序，职责是：

1. 从消息平台收到消息（通过 webhook、轮询等）
2. 启动你的 agent 进程，注入 `AGENT_MESSAGE` 环境变量
3. 读取 agent 的 stdout（按 AgentProc 协议）
4. 把回复转发给用户

下面是一个 ~30 行的 Node.js bridge 示例，把 `agentproc` 接到任何平台：

```js
// bridge.js — 一个极简的 AgentProc bridge
const { run } = require('agentproc');
const fs = require('fs');

async function handleMessage(message, sessionId) {
  const profile = JSON.parse(fs.readFileSync('./profile.json'));
  const result = await run(profile, {
    message,
    sessionId,
    onPartial: (chunk) => console.log(`[streaming] ${chunk}`),
  });
  console.log(`Reply: ${result.reply}`);
  console.log(`Session: ${result.sessionId}`);  // 下一轮把这个传回来
}

// 替换成你平台的 SDK：
// yourMessagingPlatform.onMessage(handleMessage);
handleMessage(process.argv[2] || 'hello', '');
```

存为 `bridge.js`，指向一个 profile，再接到你消息平台的 webhook。[`runner.js` 源码](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js)就是协议的代码化形式——读它就是读规范。

## 接下来去哪

- **[读协议规范](/zh/spec/)** —— 1 页，定义全部
- **[Profile Hub](/zh/hub/)** —— 主流 CLI 的开箱即用 profile
- **[CLI 参考](/zh/cli/)** —— 每个选项和参数
- **[Python SDK](/zh/sdk/python) / [Node SDK](/zh/sdk/node)** —— 在你 bridge 里嵌入 AgentProc
- **[示例](/zh/examples/)** —— claude_code bridge、裸脚本等

</div>

<style>
.get-started {
  max-width: 880px;
  margin: 0 auto;
  padding: 40px 24px 60px;
}
.get-started h1 {
  font-size: 2.2rem;
  font-weight: 700;
  margin-bottom: 32px;
  text-align: center;
}
.get-started h2 {
  margin-top: 48px;
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--vp-c-divider);
  font-size: 1.4rem;
}
</style>
