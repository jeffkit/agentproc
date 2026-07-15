# Python SDK

## Install

```bash
pip install agentproc
```

## Basic usage

```python
from agentproc import create_profile

async def handler(ctx):
    # ctx.message           — user message text
    # ctx.session_id        — previous session id (empty = new session)
    # ctx.session_name      — human-readable session name
    # ctx.from_user         — sender identifier
    # ctx.protocol_version  — protocol version string (e.g. "0.4")
    # ctx.attachments       — list of {kind, url, ...} dicts (empty = none)
    # ctx.permission        — True if the bridge enabled the permission channel
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

The SDK reads the `{"type":"turn",...}` object from stdin, calls your handler, and writes NDJSON events to stdout. Save as `agent.py`, then in your profile YAML:

```yaml
command: python3
args: ["./agent.py"]
timeout_secs: 60
```

## Returning a session ID

Return an `AgentResult` to persist a session id for multi-turn continuity:

```python
from agentproc import create_profile, AgentResult

async def handler(ctx):
    reply, new_session_id = await my_cli(ctx.message, ctx.session_id)
    return AgentResult(response=reply, session_id=new_session_id)

create_profile(handler)
```

The SDK emits a `{"type":"result","text":...}` event with optional `session_id`. The bridge persists the first non-empty `session_id` and passes it back on the next turn.

## Streaming

Use `ctx.send_partial()` to send chunks immediately without waiting for the full response. The bridge forwards them in real time when the profile's `streaming: true`:

```python
from agentproc import create_profile, AgentResult

async def handler(ctx):
    session_id = ctx.session_id
    async for chunk, session_id in stream_llm(ctx.message, ctx.session_id):
        await ctx.send_partial(chunk)
    # response="" — all content was already streamed via send_partial.
    return AgentResult(response="", session_id=session_id)

create_profile(handler)
```

`send_partial` accepts an optional `role` (`"output"` | `"thinking"`):

```python
await ctx.send_partial("reasoning...", role="thinking")
```

## Error handling

Surface a user-readable error via a `{"type":"error"}` event. Two equivalent forms:

```python
from agentproc import create_profile, AgentResult, ProtocolError

async def handler(ctx):
    # Form 1: call send_error, then return / raise
    if rate_limited():
        await ctx.send_error("rate limited; retry in 60s")
        return                       # bridge discards any reply body

    # Form 2: raise ProtocolError — SDK emits {"type":"error"} and exits 1
    if bad_input(ctx.message):
        raise ProtocolError("bad input")

    return AgentResult(response=await my_llm(ctx.message))

create_profile(handler)
```

`ProtocolError` is the exception form; the SDK serializes its message as a `{"type":"error"}` event and exits non-zero. Any other uncaught exception is logged to stderr and exits 1 without an `error` event.

## Attachments

Read `ctx.attachments` — a list of `{kind, url, ...}` dicts:

```python
async def handler(ctx):
    images = [a for a in ctx.attachments if a.get("kind") == "image"]
    reply = await my_vision(ctx.message, [a["url"] for a in images])
    return reply
```

## Conversation history

For agents that call LLM APIs directly and need to carry context across turns:

```python
from agentproc import create_profile, AgentResult, load_history, append_history, HistoryEntry

async def handler(ctx):
    history = load_history(ctx.session_id)
    messages = [{"role": e.role, "content": e.content} for e in history]
    messages.append({"role": "user", "content": ctx.message})

    reply = await call_openai(messages)

    append_history(ctx.session_id, [
        HistoryEntry(role="user", content=ctx.message),
        HistoryEntry(role="assistant", content=reply),
    ])
    return AgentResult(response=reply, session_id=ctx.session_id)

create_profile(handler)
```

History is stored as JSONL files under `~/.agentproc/sessions/<session_id>.jsonl`.

## Using `run()` as a host / bridge

If you're building a host application that drives AgentProc profiles programmatically, use `run()` from the runner module:

```python
from agentproc.runner import run, RunOptions

result = run(
    {"command": "python3", "args": ["./bridge.py"], "timeout_secs": 60},
    RunOptions(
        message="what files are here?",
        session_id="",
        on_partial=lambda chunk: print(chunk, end="", flush=True),
        on_error=lambda msg: print(f"agent error: {msg}"),
    ),
)

print(result.reply)      # assembled reply body
print(result.session_id) # session id for the next turn
print(result.exit_code)  # 0 = success, 1 = error, 124 = timeout
print(result.usage)      # {'input_tokens': 12, 'output_tokens': 34, ...} or None
```

### `RunResult` fields

| Field | Type | Description |
|-------|------|-------------|
| `reply` | `str` | Assembled reply body (empty when streaming forwarded all chunks) |
| `session_id` | `str` | First valid session id from any event; `''` if none |
| `error` | `str` | Error message from a `{"type":"error"}` event; `''` if none |
| `exit_code` | `int` | Agent process exit code (124 = timeout) |
| `timed_out` | `bool` | Whether the run was killed by timeout |
| `usage` | `dict \| None` | Token/cost stats from the terminal event; `None` when absent |

Common `usage` keys (all optional): `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `reasoning_tokens`, `duration_ms`, `cost_usd`.

## Local testing

Test your agent through the same CLI the hub uses — the most faithful end-to-end check:

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

::: tip Don't have a profile YAML yet?
Save this as `myagent.yaml` next to your agent:

```yaml
command: python3
args: ["./agent.py"]
timeout_secs: 60
```
:::

<details>
<summary>Prefer to drive the script directly?</summary>

Write the turn object to stdin the way the bridge does:

```bash
echo '{"type":"turn","message":"hello","session_id":"","from_user":"test","protocol_version":"0.4"}' | python3 ./agent.py
```

Useful when debugging the script in isolation.
</details>
