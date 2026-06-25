# Node.js SDK

## 安装

```bash
npm install @agentproc/sdk
```

SDK 版本与协议版本对齐（当前 **0.1.0**），要求 Node.js 18+。

## 基础用法

```js
const { createProfile } = require('@agentproc/sdk');

createProfile(async (ctx) => {
  // ctx.message          — 用户消息文本
  // ctx.sessionId        — 上一轮返回的 session ID（空 = 新会话）
  // ctx.sessionName      — 会话可读名称
  // ctx.fromUser         — 发送者标识符
  // ctx.streaming        — 是否流式模式
  // ctx.protocolVersion  — bridge 实现的协议版本（如 "0.1"）
  // ctx.imageUrl         — 图片附件 URL（无则为空）
  // ctx.fileUrl          — 文件附件 URL（无则为空）
  // ctx.attachments      — 多附件数组（草案，见下文）
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

SDK 会把 `AGENT_SESSION:` 行输出到 stdout 最后（spec 规定 session 行「最后一行生效」，所以在末尾输出是正确做法）。

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

`ctx.sendPartial(text)` 写入一行 `AGENT_PARTIAL:<json>`。空字符串会被忽略。`streaming: false` 时 bridge 会忽略这些行，调用仍是安全的。

## 错误透出

当 agent 遇到需要告诉用户的错误（如限流、上游 API 失效），用 `AGENT_ERROR:` 让 bridge 转发一条有意义的消息——而不是 bridge 的通用模板。

### 方式一：`ctx.sendError`

```js
createProfile(async (ctx) => {
  try {
    const reply = await myLLM(ctx.message);
    return { response: reply };
  } catch (err) {
    if (err instanceof RateLimitError) {
      ctx.sendError('被限流，60 秒后重试。');
      return; // 输出 AGENT_ERROR: 后应立即返回；并发的回复正文会被 bridge 丢弃。
    }
    throw err;
  }
});
```

### 方式二：`throw await sdk.protocolError(...)`

异常形式更符合控制流风格。SDK 会捕获标记为 `isProtocolError` 的异常，输出 `AGENT_ERROR:` 行并以退出码 1 退出。

```js
const { createProfile, protocolError } = require('@agentproc/sdk');

createProfile(async (ctx) => {
  if (!ctx.message.trim()) {
    throw await protocolError('bad input: 消息不能为空');
  }
  // ...
});
```

两种方式都会让 bridge 视本次进程为失败（即使后续退出码为 0 也会被覆盖）。

## 多附件（草案）

如果 bridge 注入了 `AGENT_ATTACHMENTS`（一个 JSON 数组），SDK 会解析为 `Attachment` 数组：

```js
createProfile(async (ctx) => {
  for (const att of ctx.attachments) {
    // att.type — "image" | "file" | "audio" | "video"
    // att.url  — bridge 提供的可获取 URL
    // att.name — 可选的文件名或显示名
    console.error(`收到 ${att.type}: ${att.url}`);
  }

  if (ctx.attachments.length > 0) {
    const first = ctx.attachments[0];
    return { response: `收到 ${first.type}：${first.name || first.url}` };
  }
  return { response: '（无附件）' };
});
```

::: warning 草案状态
`AGENT_ATTACHMENTS` 当前是草案。bridge 可能同时设置单附件变量（`AGENT_IMAGE_URL` / `AGENT_FILE_URL`）和此项。agent 应优先使用 `ctx.attachments`，为空时再回退到 `ctx.imageUrl` / `ctx.fileUrl`。
:::

## 协议版本

`ctx.protocolVersion` 反映 bridge 实现的协议版本（来自 `AGENT_PROTOCOL_VERSION`）。当 bridge 未注入时，SDK 回退到自身的 `PROTOCOL_VERSION`（当前 `"0.1"`）。

```js
const { createProfile, PROTOCOL_VERSION } = require('@agentproc/sdk');

createProfile(async (ctx) => {
  if (ctx.protocolVersion !== PROTOCOL_VERSION) {
    console.error(`warn: bridge=${ctx.protocolVersion}, sdk=${PROTOCOL_VERSION}`);
  }
  // ...
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

历史记录存储在 `~/.agentproc/sessions/<session_id>.jsonl`。

## 本地测试

不需要启动 bridge，手动设置环境变量即可测试：

```bash
AGENT_MESSAGE="你好" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
AGENT_PROTOCOL_VERSION="0.1" \
node ./agent.js
```

要模拟多附件场景：

```bash
AGENT_MESSAGE="看看这张图" \
AGENT_ATTACHMENTS='[{"type":"image","url":"https://example.com/a.png","name":"a.png"}]' \
node ./agent.js
```
