# Python SDK

## 安装

```bash
pip install agentproc
```

SDK 版本与协议版本对齐（当前 **0.1.0**），支持 Python 3.8+。

## 基础用法

```python
from agentproc import create_profile

async def handler(ctx):
    # ctx.message           — 用户消息文本
    # ctx.session_id        — 上一轮返回的 session ID（空 = 新会话）
    # ctx.session_name      — 会话可读名称
    # ctx.from_user         — 发送者标识符
    # ctx.streaming         — 是否流式模式
    # ctx.protocol_version  — bridge 实现的协议版本（如 "0.1"）
    # ctx.image_url         — 图片附件 URL（无则为空）
    # ctx.file_url          — 文件附件 URL（无则为空）
    # ctx.attachments       — 多附件列表（草案，见下文）
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

SDK 会把 `AGENT_SESSION:` 行输出到 stdout 最后（spec 规定 session 行「最后一行生效」，所以在末尾输出是正确做法）。

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

`ctx.send_partial(text)` 写入一行 `AGENT_PARTIAL:<json>` 并 flush。空字符串会被忽略。`streaming: false` 时 bridge 会忽略这些行，调用仍是安全的。

## 错误透出

当 agent 遇到需要告诉用户的错误（如限流、上游 API 失效），用 `AGENT_ERROR:` 让 bridge 转发一条有意义的消息——而不是 bridge 的通用模板。

### 方式一：`ctx.send_error`

适合需要在流程中途透出错误、之后还想自己收尾的场景。bridge 看到 `AGENT_ERROR:` 后即视为失败，即使退出码为 0。

```python
from agentproc import create_profile

async def handler(ctx):
    try:
        reply = await my_llm(ctx.message)
        return reply
    except RateLimitError:
        await ctx.send_error("被限流，60 秒后重试。")
        return  # 立即返回；并发的回复正文会被 bridge 丢弃

create_profile(handler)
```

### 方式二：`raise ProtocolError`

异常形式更符合控制流风格，且会以退出码 1 退出（更贴近 spec 的建议）。SDK 会捕获 `ProtocolError`，输出 `AGENT_ERROR:` 行。

```python
from agentproc import create_profile, ProtocolError

async def handler(ctx):
    if not ctx.message.strip():
        raise ProtocolError("bad input: 消息不能为空")
    ...

create_profile(handler)
```

两种方式都会让 bridge 视本次进程为失败（即使后续退出码为 0 也会被覆盖）。

## 多附件（草案）

如果 bridge 注入了 `AGENT_ATTACHMENTS`（一个 JSON 数组），SDK 会解析为 `Attachment` 列表：

```python
from agentproc import create_profile

async def handler(ctx):
    for att in ctx.attachments:
        # att.type — "image" | "file" | "audio" | "video"
        # att.url  — bridge 提供的可获取 URL
        # att.name — 可选的文件名或显示名
        print(f"收到 {att.type}: {att.url}", file=sys.stderr)

    if ctx.attachments:
        first = ctx.attachments[0]
        return f"收到 {first.type}：{first.name or first.url}"
    return "（无附件）"

create_profile(handler)
```

::: warning 草案状态
`AGENT_ATTACHMENTS` 当前是草案。bridge 可能同时设置单附件变量（`AGENT_IMAGE_URL` / `AGENT_FILE_URL`）和此项。agent 应优先使用 `ctx.attachments`，为空时再回退到 `ctx.image_url` / `ctx.file_url`。
:::

## 协议版本

`ctx.protocol_version` 反映 bridge 实现的协议版本（来自 `AGENT_PROTOCOL_VERSION`）。当 bridge 未注入时，SDK 回退到自身的 `PROTOCOL_VERSION`（当前 `"0.1"`）。

```python
from agentproc import PROTOCOL_VERSION  # "0.1"

async def handler(ctx):
    if ctx.protocol_version != PROTOCOL_VERSION:
        print(f"warn: bridge={ctx.protocol_version}, sdk={PROTOCOL_VERSION}", file=sys.stderr)
    ...
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

不需要启动 bridge，手动设置环境变量即可测试：

```bash
AGENT_MESSAGE="你好" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
AGENT_PROTOCOL_VERSION="0.1" \
python3 ./agent.py
```

要模拟多附件场景：

```bash
AGENT_MESSAGE="看看这张图" \
AGENT_ATTACHMENTS='[{"type":"image","url":"https://example.com/a.png","name":"a.png"}]' \
python3 ./agent.py
```
