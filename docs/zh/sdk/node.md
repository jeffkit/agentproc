# Node.js SDK

## 安装

```bash
npm install agentproc
```

## 基础用法

```js
const { createProfile } = require('agentproc');

createProfile(async (ctx) => {
  // ctx.message           — 用户消息文本
  // ctx.sessionId         — 上一轮返回的 session ID（空 = 新会话）
  // ctx.sessionName       — 会话可读名称
  // ctx.fromUser          — 发送者标识符
  // ctx.protocolVersion   — bridge 实现的协议版本（如 "0.3"）
  // ctx.attachments       — 附件数组，元素为 {kind, url, ...} 对象（空 = 无）
  // ctx.permission        — bridge 是否开启了权限通道
  const reply = await myLLM(ctx.message);
  return { response: reply };
});
```

SDK 从 stdin 读取 `{"type":"turn",...}` 对象，调用你的 handler，再把 NDJSON 事件写到 stdout。保存为 `agent.js`，在 profile YAML 中：

```yaml
command: node
args: ["./agent.js"]
timeout_secs: 60
```

## 返回 session ID

```js
createProfile(async ({ message, sessionId }) => {
  const { reply, newSessionId } = await myCLI(message, sessionId);
  return { response: reply, sessionId: newSessionId };
});
```

SDK 会输出 `{"type":"session","id":...}` 事件（随后是 `{"type":"text"}` 回复事件）。bridge 在下一轮把它作为 `sessionId` 传回。

## 流式输出

用 `ctx.sendPartial()` 立即发送分片。当 profile 的 `streaming: true` 时，bridge 会实时转发：

```js
createProfile(async (ctx) => {
  let newSessionId = ctx.sessionId;
  for await (const { chunk, sid } of streamLLM(ctx.message, ctx.sessionId)) {
    ctx.sendPartial(chunk);
    newSessionId = sid;
  }
  // response: '' —— 内容已通过 sendPartial 全部流式发出。
  return { response: '', sessionId: newSessionId };
});
```

`sendPartial` 可选 `role` 参数（`"output"` | `"thinking"`）：

```js
ctx.sendPartial('推理中...', 'thinking');
```

## 错误透出

当 agent 遇到需要告诉用户的错误，用 `{"type":"error"}` 事件让 bridge 转发。两种等价形式：

```js
const { createProfile, protocolError } = require('agentproc');

createProfile(async (ctx) => {
  // 方式一：调用 sendError，然后返回 / 抛出
  if (rateLimited()) {
    ctx.sendError('被限流，60 秒后重试。');
    return;                          // bridge 会丢弃并发的回复正文
  }

  // 方式二：抛一个 protocol error —— SDK 输出 {"type":"error"} 并以退出码 1 退出
  if (badInput(ctx.message)) {
    throw protocolError('消息不能为空');
  }

  return { response: await myLLM(ctx.message) };
});
```

`protocolError(msg)` 返回一个带 `isProtocolError` 标记的 `ProtocolError` 实例；SDK 把它的消息序列化为 `{"type":"error"}` 事件并以退出码 1 退出。其他未捕获错误会打到 stderr，以退出码 1 退出但不输出 `error` 事件。

## 附件

读 `ctx.attachments` —— 一个 `{kind, url, ...}` 对象数组：

```js
createProfile(async (ctx) => {
  const images = ctx.attachments.filter(a => a.kind === 'image');
  const reply = await myVision(ctx.message, images.map(a => a.url));
  return { response: reply };
});
```

## 会话历史

```js
const { createProfile, loadHistory, appendHistory } = require('agentproc');

createProfile(async ({ message, sessionId }) => {
  const history = loadHistory(sessionId);
  const messages = [...history.map(e => ({ role: e.role, content: e.content })),
                    { role: 'user', content: message }];

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

用 hub 同款 CLI 跑你的 agent —— 最忠实的端到端验证：

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

::: tip 还没有 profile YAML？
在你 agent 旁边存一个 `myagent.yaml`：

```yaml
command: node
args: ["./agent.js"]
timeout_secs: 60
```
:::

<details>
<summary>想直接驱动脚本？</summary>

像 bridge 那样把 turn 对象写进 stdin：

```bash
echo '{"type":"turn","message":"hello","session_id":"","from_user":"test","protocol_version":"0.3"}' | node ./agent.js
```

单独调试脚本时很有用。
</details>
