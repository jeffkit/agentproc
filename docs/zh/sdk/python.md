# Python SDK

## 安装

```bash
pip install agentproc
```

## 基础用法

```python
from agentproc import create_profile

async def handler(ctx):
    # ctx.message      — 用户消息文本
    # ctx.session_id   — 上一轮 CLI session UUID（空 = 新会话）
    # ctx.session_name — 会话可读名称
    # ctx.from_user    — 发送者标识符
    # ctx.streaming    — 是否流式模式
    # ctx.image_url    — 图片附件 URL（无则为空）
    # ctx.file_url     — 文件附件 URL（无则为空）
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

保存为 `agent.py`，在 profile YAML 中：

```yaml
command: python3 ./agent.py
timeout_secs: 60
```

## 返回 session ID

```python
from agentproc import create_profile, AgentResult

async def handler(ctx):
    reply, new_session_id = await my_cli(ctx.message, ctx.session_id)
    return AgentResult(response=reply, session_id=new_session_id)

create_profile(handler)
```

## 流式输出

```python
from agentproc import create_profile, AgentResult

async def handler(ctx):
    session_id = ctx.session_id
    async for chunk, session_id in stream_llm(ctx.message, ctx.session_id):
        await ctx.send_partial(chunk)
    return AgentResult(response="", session_id=session_id)

create_profile(handler)
```

## 会话历史

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

历史记录存储在 `~/.agentproc/sessions/<session_id>.jsonl`。

## 本地测试

```bash
AGENT_MESSAGE="你好" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
python3 ./agent.py
```
