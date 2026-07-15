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
  // ctx.protocolVersion   — protocol version string (e.g. "0.4")
  // ctx.attachments       — array of {kind, url, ...} objects (empty = none)
  // ctx.permission        — true if the bridge enabled the permission channel
  const reply = await myLLM(ctx.message);
  return { response: reply };
});
```

The SDK reads the `{"type":"turn",...}` object from stdin, calls your handler, and writes NDJSON events to stdout. Save as `agent.js`, then in your profile YAML:

```yaml
command: node
args: ["./agent.js"]
timeout_secs: 60
```

## Returning a session ID

```js
createProfile(async ({ message, sessionId }) => {
  const { reply, newSessionId } = await myCLI(message, sessionId);
  return { response: reply, sessionId: newSessionId };
});
```

The SDK emits a `{"type":"result","text":...}` event with optional `session_id`. The bridge persists the first non-empty `session_id` and passes it back as `sessionId` on the next turn.

## Streaming

Use `ctx.sendPartial()` to send chunks immediately. The bridge forwards them in real time when the profile's `streaming: true`:

```js
createProfile(async (ctx) => {
  let newSessionId = ctx.sessionId;
  for await (const { chunk, sid } of streamLLM(ctx.message, ctx.sessionId)) {
    ctx.sendPartial(chunk);
    newSessionId = sid;
  }
  // response: '' — all content was already streamed via sendPartial.
  return { response: '', sessionId: newSessionId };
});
```

`sendPartial` accepts an optional `role` (`"output"` | `"thinking"`):

```js
ctx.sendPartial('reasoning...', 'thinking');
```

## Error handling

Surface a user-readable error via a `{"type":"error"}` event. Two equivalent forms:

```js
const { createProfile, protocolError } = require('agentproc');

createProfile(async (ctx) => {
  // Form 1: call sendError, then return / throw
  if (rateLimited()) {
    ctx.sendError('rate limited; retry in 60s');
    return;                          // bridge discards any reply body
  }

  // Form 2: throw a protocol error — SDK emits {"type":"error"} and exits 1
  if (badInput(ctx.message)) {
    throw protocolError('bad input');
  }

  return { response: await myLLM(ctx.message) };
});
```

`protocolError(msg)` returns a `ProtocolError` instance tagged `isProtocolError`; the SDK serializes its message as a `{"type":"error"}` event and exits 1. Any other uncaught error is logged to stderr and exits 1 without an `error` event.

## Attachments

Read `ctx.attachments` — an array of `{kind, url, ...}` objects:

```js
createProfile(async (ctx) => {
  const images = ctx.attachments.filter(a => a.kind === 'image');
  const reply = await myVision(ctx.message, images.map(a => a.url));
  return { response: reply };
});
```

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

## Using `run()` as a host / bridge

If you're building a host application (e.g. an IM bridge, a CI bot) that needs to drive AgentProc profiles programmatically, use `run()` from the runner module:

```js
const { run } = require('agentproc/src/runner');

const result = await run(
  { command: 'python3', args: ['./bridge.py'], timeout_secs: 60 },
  {
    message: 'what files are here?',
    sessionId: '',              // empty = new session
    onPartial: (chunk) => process.stdout.write(chunk),
    onError: (msg) => console.error('agent error:', msg),
  }
);

console.log(result.reply);      // assembled reply body
console.log(result.sessionId);  // session id for the next turn
console.log(result.exitCode);   // 0 = success, 1 = error, 124 = timeout
console.log(result.usage);      // { input_tokens, output_tokens, ... } or null
```

### Using in-process executors

For profiles backed by a known CLI (claude, codex, gemini, …), set `executor:` to skip the bridge-subprocess fork:

```js
const result = await run(
  { executor: 'claude-code', timeout_secs: 600, streaming: true,
    env: { ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY } },
  { message: 'explain this codebase', onPartial: (c) => process.stdout.write(c) }
);
// result.usage?.input_tokens — token count from claude
```

See all built-in names:
```js
const { executorNames } = require('agentproc');
// ['claude-code', 'codebuddy', 'codex', 'cursor', 'gemini-cli', ...]
```

### `RunResult` shape

| Field | Type | Description |
|-------|------|-------------|
| `reply` | `string` | Assembled reply body (empty when streaming forwarded all chunks) |
| `sessionId` | `string` | First valid session id from any event; `''` if none |
| `error` | `string` | Error message from a `{"type":"error"}` event; `''` if none |
| `exitCode` | `number` | Agent process exit code (124 = timeout) |
| `timedOut` | `boolean` | Whether the run was killed by timeout |
| `usage` | `object\|null` | Token/cost stats from the terminal event; `null` when absent |

Common `usage` keys (all optional): `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `reasoning_tokens`, `duration_ms`, `cost_usd`.

## Local testing

Test your agent through the same CLI the hub uses — the most faithful end-to-end check:

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

::: tip Don't have a profile YAML yet?
Save this as `myagent.yaml` next to your agent:

```yaml
command: node
args: ["./agent.js"]
timeout_secs: 60
```
:::

<details>
<summary>Prefer to drive the script directly?</summary>

Write the turn object to stdin the way the bridge does:

```bash
echo '{"type":"turn","message":"hello","session_id":"","from_user":"test","protocol_version":"0.4"}' | node ./agent.js
```

Useful when debugging the script in isolation.
</details>
