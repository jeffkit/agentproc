# Node.js SDK

## Install

```bash
npm install agentproc
```

## Basic usage

```js
const { createProfile } = require('agentproc');

createProfile(async (ctx) => {
  // ctx.message           — user message text
  // ctx.sessionId         — previous session id (empty = new session)
  // ctx.sessionName       — human-readable session name
  // ctx.fromUser          — sender identifier
  // ctx.streaming         — true if bridge expects streaming output
  // ctx.protocolVersion   — protocol version string (e.g. "0.1")
  // ctx.imageUrl          — image attachment URL (empty if none)
  // ctx.fileUrl           — file attachment URL (empty if none)
  // ctx.attachments       — parsed AGENT_ATTACHMENTS (draft, [] if unset)
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

## Error handling

Surface a user-readable error via `AGENT_ERROR:`. Two equivalent forms:

```js
const { createProfile, protocolError } = require('agentproc');

createProfile(async (ctx) => {
  // Form 1: call sendError, then return / throw
  if (rateLimited()) {
    ctx.sendError('rate limited; retry in 60s');
    return;                          // bridge discards any reply body
  }

  // Form 2: throw a protocol error — SDK emits AGENT_ERROR: and exits 1
  if (badInput(ctx.message)) {
    throw await protocolError('bad input');
  }

  return { response: await myLLM(ctx.message) };
});
```

`protocolError(msg)` returns a rejected promise whose error is tagged `isProtocolError`; the SDK serializes its message as an `AGENT_ERROR:` line and exits 1. Any other uncaught error is logged to stderr and exits 1 without an `AGENT_ERROR:` line.

## Multi-attachment input (draft)

When the bridge sets `AGENT_ATTACHMENTS`, the SDK parses it into `ctx.attachments`:

```js
const { createProfile } = require('agentproc');

createProfile(async (ctx) => {
  // Prefer the multi-attachment list when present, fall back to single vars
  const images = (ctx.attachments.filter(a => a.type === 'image').length
    ? ctx.attachments.filter(a => a.type === 'image')
    : (ctx.imageUrl ? [{ type: 'image', url: ctx.imageUrl }] : []));
  const reply = await myVisionLLM(ctx.message, images.map(a => a.url));
  return { response: reply };
});
```

`ctx.attachments` is empty when `AGENT_ATTACHMENTS` is unset or unparseable. The single-attachment vars (`ctx.imageUrl`, `ctx.fileUrl`) remain the P0 path.

## Conversation history

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

History is stored as JSONL files under `~/.agentproc/sessions/<session_id>.jsonl`.

## Local testing

Test your agent through the same CLI the hub uses — the most faithful end-to-end check:

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

::: tip Don't have a profile YAML yet?
Save this as `myagent.yaml` next to your agent:

```yaml
command: node ./agent.js
timeout_secs: 60
```
:::

<details>
<summary>Prefer to drive the script directly?</summary>

Set the env vars the bridge would inject. This is exactly what the CLI does internally:

```bash
AGENT_MESSAGE="hello" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
AGENT_PROTOCOL_VERSION="0.1" \
node ./agent.js
```

Useful when debugging the script in isolation.
</details>
