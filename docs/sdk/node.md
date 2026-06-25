# Node.js SDK

## Install

```bash
npm install @agentproc/sdk
```

## Basic usage

```js
const { createProfile } = require('@agentproc/sdk');

createProfile(async (ctx) => {
  // ctx.message      — user message text
  // ctx.sessionId    — previous CLI session UUID (empty = new session)
  // ctx.sessionName  — human-readable session name
  // ctx.fromUser     — sender identifier
  // ctx.streaming    — true if bridge expects streaming output
  // ctx.imageUrl     — image attachment URL (empty if none)
  // ctx.fileUrl      — file attachment URL (empty if none)
  const reply = await myLLM(ctx.message);
  return { response: reply };
});
```

Save as `agent.js`, then in your profile YAML:

```yaml
command: node ./agent.js
timeout_secs: 60
```

## Returning a session ID

```js
createProfile(async ({ message, sessionId }) => {
  const { reply, newSessionId } = await myCLI(message, sessionId);
  return { response: reply, sessionId: newSessionId };
});
```

## Streaming

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

## Conversation history

```js
const { createProfile, loadHistory, appendHistory } = require('@agentproc/sdk');

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

History is stored as JSONL files under `~/.agentproc/sessions/<session_id>.jsonl`.

## Local testing

```bash
AGENT_MESSAGE="hello" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
node ./agent.js
```
