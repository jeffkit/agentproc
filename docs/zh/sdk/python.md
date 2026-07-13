# Python SDK

## 安装

```bash
pip install agentproc
```

## 基础用法

```python
from agentproc import create_profile

async def handler(ctx):
    # ctx.message           — 用户消息文本
    # ctx.session_id        — 上一轮返回的 session ID（空 = 新会话）
    # ctx.session_name      — 会话可读名称
    # ctx.from_user         — 发送者标识符
    # ctx.protocol_version  — bridge 实现的协议版本（如 "0.3"）
    # ctx.attachments       — 附件列表，元素为 {kind, url, ...} 字典（空 = 无）
    # ctx.permission        — bridge 是否开启了权限通道
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

SDK 从 stdin 读取 `{"type":"turn",...}` 对象，调用你的 handler，再把 NDJSON 事件写到 stdout。保存为 `agent.py`，在 profile YAML 中：

```yaml
command: python3
args: ["./agent.py"]
timeout_secs: 60
```

## 返回 session ID

返回 `AgentResult` 以持久化 session ID，实现多轮续接：

```python
from agentproc import create_profile, AgentResult

async def handler(ctx):
    reply, new_session_id = await my_cli(ctx.message, ctx.session_id)
    return AgentResult(response=reply, session_id=new_session_id)

create_profile(handler)
```

SDK 会输出 `{"type":"session","id":...}` 事件（随后是 `{"type":"text"}` 回复事件）。bridge 在下一轮把它作为 `session_id` 传回。

## 流式输出

用 `ctx.send_partial()` 立即发送分片，不必等整段回复。当 profile 的 `streaming: true` 时，bridge 会实时转发：

```python
from agentproc import create_profile, AgentResult

async def handler(ctx):
    session_id = ctx.session_id
    async for chunk, session_id in stream_llm(ctx.message, ctx.session_id):
        await ctx.send_partial(chunk)
    # response="" —— 内容已通过 send_partial 全部流式发出。
    return AgentResult(response="", session_id=session_id)

create_profile(handler)
```

`send_partial` 可选 `role` 参数（`"output"` | `"thinking"`）：

```python
await ctx.send_partial("推理中...", role="thinking")
```

## 错误透出

当 agent 遇到需要告诉用户的错误，用 `{"type":"error"}` 事件让 bridge 转发。两种等价形式：

```python
from agentproc import create_profile, AgentResult, ProtocolError

async def handler(ctx):
    # 方式一：调用 send_error，然后返回 / 抛出
    if rate_limited():
        await ctx.send_error("被限流，60 秒后重试。")
        return                       # bridge 会丢弃并发的回复正文

    # 方式二：抛 ProtocolError —— SDK 输出 {"type":"error"} 并以退出码 1 退出
    if bad_input(ctx.message):
        raise ProtocolError("消息不能为空")

    return AgentResult(response=await my_llm(ctx.message))

create_profile(handler)
```

`ProtocolError` 是异常形式；SDK 把它的消息序列化为 `{"type":"error"}` 事件并以非零码退出。其他未捕获异常会打到 stderr，以退出码 1 退出但不输出 `error` 事件。

## 附件

读 `ctx.attachments` —— 一个 `{kind, url, ...}` 字典列表：

```python
async def handler(ctx):
    images = [a for a in ctx.attachments if a.get("kind") == "image"]
    reply = await my_vision(ctx.message, [a["url"] for a in images])
    return reply
```

## 会话历史

对于直接调 LLM API、需要跨轮携带上下文的 agent：

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

用 hub 同款 CLI 跑你的 agent —— 最忠实的端到端验证：

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

::: tip 还没有 profile YAML？
在你 agent 旁边存一个 `myagent.yaml`：

```yaml
command: python3
args: ["./agent.py"]
timeout_secs: 60
```
:::

<details>
<summary>想直接驱动脚本？</summary>

像 bridge 那样把 turn 对象写进 stdin：

```bash
echo '{"type":"turn","message":"hello","session_id":"","from_user":"test","protocol_version":"0.3"}' | python3 ./agent.py
```

单独调试脚本时很有用。
</details>
