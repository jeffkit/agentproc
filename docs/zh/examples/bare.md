# 裸脚本（无 SDK）

你不需要 SDK。任何从 stdin 读取 `{"type":"turn",...}` 对象、并向 stdout 写入 NDJSON 事件的脚本，都是合法的 AgentProc agent。

## Echo agent

::: code-group

```bash [bash]
#!/usr/bin/env bash
# Wire 0.4：turn 以一行 NDJSON 到达 stdin。
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"result","text":"你说：'"$message"'"},separators=(",",":"))+"\n")'
```

```python [python]
#!/usr/bin/env python3
import json, sys
turn = json.loads(sys.stdin.readline() or "{}")
msg = turn.get("message", "") if isinstance(turn.get("message"), str) else ""
sys.stdout.write(json.dumps({"type": "result", "text": f"你说：{msg}"}, separators=(",", ":")) + "\n")
```

```js [node]
#!/usr/bin/env node
const fs = require('node:fs');
const raw = fs.readFileSync(0, 'utf8');
const turn = JSON.parse(raw.split('\n')[0] || '{}');
process.stdout.write(JSON.stringify({ type: 'result', text: `你说：${turn.message || ''}` }) + '\n');
```

:::

## 流式输出

分块到达时发出 `{"type":"partial"}` 事件。当 profile 的 `streaming: true` 时，runner 会实时转发。

```python
#!/usr/bin/env python3
import json, sys, time

turn = json.loads(sys.stdin.readline() or "{}")
message = turn.get("message", "") if isinstance(turn.get("message"), str) else ""

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for i, word in enumerate(message.split()):
    chunk = word + (" " if i < len(message.split()) - 1 else "")
    emit({"type": "partial", "text": chunk})
    time.sleep(0.05)

# 空 result —— 正文已通过 partial 送达。可选的 usage 放在这里。
emit({"type": "result", "text": ""})
```

如果全部内容已通过 `partial` 事件发出，就发 `{"type":"result","text":""}`（若没有 `usage` 要发布也可以省略 `result`）——runner 把已转发的 partial 视为用户可见正文。

## 会话连续性

在事件上打上 `session_id`（持久化第一个非空值；早期可省略；一旦已知 SHOULD 带上）。无状态 agent 完全省略该字段。

```python
#!/usr/bin/env python3
import json, sys, uuid

turn = json.loads(sys.stdin.readline() or "{}")
message = turn.get("message", "") if isinstance(turn.get("message"), str) else ""
session_id = turn.get("session_id", "") if isinstance(turn.get("session_id"), str) else ""

if not session_id:
    session_id = str(uuid.uuid4())

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

emit({"type": "result", "text": f"[会话 {session_id[:8]}] 你说：{message}", "session_id": session_id})
```

## Profile YAML

```yaml
command: python3                     # 或 bash、node、…
args: ["./my_agent.py"]
timeout_secs: 30
```
