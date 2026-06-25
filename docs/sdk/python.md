# Python SDK

## Install

```bash
pip install agentproc
```

## Basic usage

```python
from agentproc import create_profile

async def handler(ctx):
    # ctx.message      — user message text
    # ctx.session_id   — previous CLI session UUID (empty = new session)
    # ctx.session_name — human-readable session name
    # ctx.from_user    — sender identifier
    # ctx.streaming    — True if bridge expects streaming output
    # ctx.image_url    — image attachment URL (empty if none)
    # ctx.file_url     — file attachment URL (empty if none)
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

Return an `AgentResult` to persist a CLI session UUID for multi-turn continuity:

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

Test your agent without a running bridge:

```bash
AGENT_MESSAGE="hello" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
python3 ./agent.py
```
