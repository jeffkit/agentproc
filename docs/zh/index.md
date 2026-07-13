---
layout: home

hero:
  name: AgentProc
  text: 把任意 Agent CLI 接入任意消息平台
  tagline: 一个极简的进程级协议。不用 HTTP、不用 socket——只用 stdin 和 stdout。
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

::: tip macOS 用户：找不到 `pip`/`pipx`？
Homebrew 的 Python 默认不暴露 `pip`。可以跑 `python3 -m ensurepip && python3 -m pip install --user pipx`，或者直接用上面的 `npm`——反正 `agentproc` CLI 本来就是 npm 包，Node 是必需的。
:::

验证可用：

```bash
agentproc --version
# agentproc 0.7.0 (protocol 0.3)
```

## ② 浏览 hub

```bash
agentproc hub list
#   claude-code   official    Connect the claude CLI (Anthropic) as an AgentProc agent
#   codex         official    Connect the codex CLI (OpenAI) as an AgentProc agent
#   codebuddy     official    Connect the codebuddy CLI (Tencent) as an AgentProc agent
#   agy           community   Connect the agy CLI as an AgentProc agent
#   echo-agent    official    Minimal hello-world agent
```

[Profile Hub](/zh/hub/) 收录了主流 AI CLI 的开箱即用 profile。不用 clone、不用复制、不用改 YAML——CLI 首次使用时从 GitHub 拉取，缓存在 `~/.agentproc/cache/hub/<name>/`（24 小时 TTL）。

::: tip 遇到 GitHub 限流？
匿名拉取每个 IP 每小时 ~60 次。设置 token 可以提到 5,000 次/小时：

```bash
export GITHUB_TOKEN=$(gh auth token)   # 或任意 personal access token
```

如果你想完全绕开网络，可以用本地仓库：`agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"`。
:::

## ③ 一行命令跑起来

先跑冒烟测试（不需要 API key）：

```bash
agentproc hub run echo-agent -p "hello"
# → You said: hello
```

然后跑真实的。`claude-code` 支持流式输出和多轮会话续接：

```bash
cd ~/projects/my-app          # agent 在哪个目录跑，就 cd 到哪里
agentproc hub run claude-code \
  -p "what is this codebase?" \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
```

::: tip 不需要改任何 profile YAML
`agentproc hub run` 自动把**你当前所在目录**作为 agent 的 `cwd`，并通过 `{{PROFILE_DIR}}` 占位符找到打包的 bridge 脚本。只要 cd 到你想让 agent 操作的项目目录，跑就行。
:::

stderr 上会实时看到 NDJSON 事件，stdout 是最终回复：

```
{"type":"partial","text":"This codebase is..."}
{"type":"session","id":"13c2f6ec-1f97-42c4-be9e-9475129e243c"}
agentproc:session:13c2f6ec-1f97-42c4-be9e-9475129e243c
```

捕获 session id，继续对话：

```bash
agentproc hub run claude-code \
  -p "tell me about the auth module" \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --session 13c2f6ec-1f97-42c4-be9e-9475129e243c
```

::: tip 短回复可能看不到 {"type":"partial"}
有些 agent 在回答较短时一次性吐完全部内容——你只会看到 `{"type":"session"}` 和回复正文，没有 `{"type":"partial"}` 行。这是正常的；流式只分片长回复。
:::

## ④ 接到你的消息平台

AgentProc agent 不直接和微信或 Slack 通信——那是 **bridge** 的工作。bridge 是一个小程序，职责是：

1. 从消息平台收到消息（通过 webhook、轮询等）
2. 启动你的 agent 进程，往它的 stdin 写入一个 `{"type":"turn",...}` 对象
3. 读取 agent stdout 上的 NDJSON 事件（按 AgentProc 协议）
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
- **[故障排除](/zh/guide/troubleshooting)** —— 卡住了？常见错误和确切修法

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
