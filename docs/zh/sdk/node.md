# Node.js SDK

## 安装

```bash
npm install @agentproc/sdk
```

## 基础用法

```js
const { createProfile } = require('@agentproc/sdk');

createProfile(async (ctx) => {
  // ctx.message      — 用户消息文本
  // ctx.sessionId    — 上一轮 CLI session UUID（空 = 新会话）
  // ctx.sessionName  — 会话可读名称
  // ctx.fromUser     — 发送者标识符
  // ctx.streaming    — 是否流式模式
  // ctx.imageUrl     — 图片附件 URL（无则为空）
  // ctx.fileUrl      — 文件附件 URL（无则为空）
  const reply = await myLLM(ctx.message);
  return { response: reply };
});
```

保存为 `agent.js`，在 profile YAML 中：

```yaml
command: node ./agent.js
timeout_secs: 60
```

## 返回 session ID

```js
createProfile(async ({ message, sessionId }) => {
  const { reply, newSessionId } = await myCLI(message, sessionId);
  return { response: reply, sessionId: newSessionId };
});
```

## 流式输出

```js
createProfile(async (ctx) => {
  let newSessionId = ctx.sessionId;
  for await (const { chunk, sid } of streamLLM(ctx.message, ctx.sessionId)) {
    ctx.sendPartial(chunk);
    newSessionId = sid;
  }
  return { response: '', sessionId: newSessionId };
});
```

## 会话历史

```js
const { createProfile, loadHistory, appendHistory } = require('@agentproc/sdk');

createProfile(async ({ message, sessionId }) => {
  const history = loadHistory(sessionId);
  const messages = [
    ...history.map(e => ({ role: e.role, content: e.content })),
    { role: 'user', content: message },
  ];
  const reply = await callOpenAI(messages);
  appendHistory(sessionId, [
    { role: 'user', content: message },
    { role: 'assistant', content: reply },
  ]);
  return { response: reply, sessionId };
});
```

## 本地测试

```bash
AGENT_MESSAGE="你好" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
node ./agent.js
```
