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
    # ctx.streaming         — True if bridge expects streaming output
    # ctx.protocol_version  — protocol version string (e.g. "0.1")
    # ctx.image_url         — image attachment URL (empty if none)
    # ctx.file_url          — file attachment URL (empty if none)
    # ctx.attachments       — parsed AGENT_ATTACHMENTS (draft, [] if unset)
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

Save as `agent.py`, then in your profile YAML:

```yaml
command: python3 ./agent.py
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

## Streaming

Use `ctx.send_partial()` to send chunks immediately without waiting for the full response:

```python
from agentproc import create_profile, AgentResult

async def handler(ctx):
    session_id = ctx.session_id
    async for chunk, session_id in stream_llm(ctx.message, ctx.session_id):
        await ctx.send_partial(chunk)
    return AgentResult(response="", session_id=session_id)

create_profile(handler)
```

## Error handling

Surface a user-readable error via `AGENT_ERROR:`. Two equivalent forms:

```python
from agentproc import create_profile, AgentResult, ProtocolError

async def handler(ctx):
    # Form 1: call send_error, then return / raise
    if rate_limited():
        await ctx.send_error("rate limited; retry in 60s")
        return                       # bridge discards any reply body

    # Form 2: raise ProtocolError — SDK emits AGENT_ERROR: and exits 1
    if bad_input(ctx.message):
        raise ProtocolError("bad input")

    return AgentResult(response=await my_llm(ctx.message))

create_profile(handler)
```

`ProtocolError` is the exception form; the SDK serializes its message as an `AGENT_ERROR:` line and exits non-zero. Any other uncaught exception is logged to stderr and exits 1 without an `AGENT_ERROR:` line.

## Multi-attachment input (draft)

When the bridge sets `AGENT_ATTACHMENTS`, the SDK parses it into `ctx.attachments`:

```python
from agentproc import create_profile, Attachment

async def handler(ctx):
    # Prefer the multi-attachment list when present, fall back to single vars
    images = [a for a in ctx.attachments if a.type == "image"] or (
        [Attachment(type="image", url=ctx.image_url)] if ctx.image_url else []
    )
    reply = await my_vision_llm(ctx.message, [a.url for a in images])
    return reply

create_profile(handler)
```

`ctx.attachments` is empty when `AGENT_ATTACHMENTS` is unset or unparseable. The single-attachment vars (`ctx.image_url`, `ctx.file_url`) remain the P0 path.

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

## Local testing

Test your agent through the same CLI the hub uses — the most faithful end-to-end check:

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

::: tip Don't have a profile YAML yet?
Save this as `myagent.yaml` next to your agent:

```yaml
command: python3 ./agent.py
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
python3 ./agent.py
```

Useful when debugging the script in isolation.
</details>
