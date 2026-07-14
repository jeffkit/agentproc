# 快速开始

5 分钟跑起一个 AgentProc 兼容的 agent。

::: tip 两条路径，选一条
- **只是想用主流 AI CLI（claude、codex、codebuddy 等）？** 不用看本页。直接去[首页](/zh/)用 `agentproc hub run <name>`——零配置。
- **想从零写一个自己的 agent 脚本？** 本页就是写给你的。你会写一个小脚本、一个 2 行 profile YAML，然后用同一个 `agentproc` CLI 跑起来。
:::

## 第一步：写 agent 脚本

最简单的 agent 从 stdin 读取 `{"type":"turn",...}` 对象，向 stdout 写一个 NDJSON 事件。

::: code-group

```bash [bash]
#!/usr/bin/env bash
# echo_agent.sh —— 从 stdin 读 turn，把消息原样回复
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"result","text":"你说：'"$message"'"},separators=(",",":"))+"\n")'
```

```python [python]
#!/usr/bin/env python3
# echo_agent.py
import json, sys
turn = json.loads(sys.stdin.readline() or "{}")
msg = turn.get("message", "") if isinstance(turn.get("message"), str) else ""
sys.stdout.write(json.dumps({"type": "result", "text": f"你说：{msg}"}, separators=(",", ":")) + "\n")
```

```js [node]
#!/usr/bin/env node
// echo_agent.js
const fs = require('node:fs');
const raw = fs.readFileSync(0, 'utf8');
const turn = JSON.parse(raw.split('\n')[0] || '{}');
process.stdout.write(JSON.stringify({ type: 'result', text: `你说：${turn.message || ''}` }) + '\n');
```

:::

## 第二步：创建 profile YAML

```yaml
# myagent.yaml
command: bash
args: ["./echo_agent.sh"]
timeout_secs: 10
```

## 第三步：用 agentproc CLI 本地测试

用 hub 用的同一个 CLI 跑你的 profile——这是对真实 bridge 行为最忠实的测试：

```bash
agentproc --profile ./myagent.yaml --prompt "你好"
# → 你说：你好
```

NDJSON 事件（`{"type":"partial"}`、`{"type":"result"}`、`{"type":"error"}`；可选 `session_id`）出现在 stderr，回复正文（来自 `{"type":"result"}` / 流式 `partial`）出现在 stdout。CLI 的退出码和 bridge 看到的一致：`0` 成功、`1` 错误、`124` 超时。

<details>
<summary>不想用 CLI 测试？</summary>

也可以直接把 turn 对象 pipe 给脚本。CLI 内部就是这么做的：

```bash
echo '{"type":"turn","message":"你好","session_id":"","from_user":"test","protocol_version":"0.4"}' | bash ./echo_agent.sh
```

调试脚本本身时有用；但端到端行为请优先用上面的 `agentproc --profile ...`。
</details>

## 第四步：接入 bridge

将 profile YAML 的路径告诉 bridge。具体步骤取决于你用的 bridge 实现，请参考对应 bridge 的文档。[Node SDK 的 `run()` 函数](/zh/sdk/node)是 bridge 行为的标准参考。

---

## 错误处理

当 agent 遇到需要让用户看见的错误（上游 API 失效、限流、参数不合法等），输出一个 `{"type":"error"}` 事件——bridge 会把消息原样转发给用户，并把这次进程视为失败（即使后续退出码为 0），并丢弃并发的回复正文。

::: code-group

```bash [bash]
#!/usr/bin/env bash
# error_agent.sh
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
if [ -z "$message" ]; then
  echo '{"type":"error","message":"消息不能为空。"}'
  exit 1
fi
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"result","text":"你说：'"$message"'"},separators=(",",":"))+"\n")'
```

```python [python]
#!/usr/bin/env python3
# error_agent.py
from agentproc import create_profile, ProtocolError

async def handler(ctx):
    if not ctx.message.strip():
        raise ProtocolError("消息不能为空。")
    return f"你说：{ctx.message}"

create_profile(handler)
```

```js [node]
#!/usr/bin/env node
// error_agent.js
const { createProfile, protocolError } = require('agentproc');

createProfile(async (ctx) => {
  if (!ctx.message.trim()) {
    throw protocolError('消息不能为空。');
  }
  return { response: `你说：${ctx.message}` };
});
```

:::

SDK 形式（抛 `ProtocolError` / `protocolError(...)`）和裸 `echo '{"type":"error","message":"..."}'` 形式产生的线上输出完全一致。无论哪种，输出事件后都要以非零码退出。

---

## 下一步

- [阅读完整协议规范](/zh/spec/) 了解所有特性
- [使用 SDK](/zh/sdk/) 省去样板代码
- [查看示例](/zh/examples/claude) 了解如何接入 claude 等真实 AI agent

::: tip 卡住了？
看 [故障排除](/zh/guide/troubleshooting)，覆盖了最常见的几类错误（限流、`spawn ENOENT`、模型不存在、超时等），每类都给出确切修法。
:::
