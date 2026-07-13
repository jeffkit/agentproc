# 裸脚本（无 SDK）

不需要 SDK。任何从 stdin 读取 `{"type":"turn",...}` 对象、向 stdout 写 NDJSON 事件的脚本都是合法的 AgentProc agent。

## Echo agent

::: code-group

```bash [bash]
#!/usr/bin/env bash
# Wire 0.3：turn 以一行 NDJSON 到达 stdin。
read -r turn
message=$(printf '%s' "$turn" | python3 -c 'import json,sys; t=json.loads(sys.stdin.read() or "{}"); print(t.get("message","") if isinstance(t.get("message"),str) else "")')
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"type":"text","text":"你说：'"$message"'"},separators=(",",":"))+"\n")'
```

```python [python]
#!/usr/bin/env python3
import json, sys
turn = json.loads(sys.stdin.readline() or "{}")
msg = turn.get("message", "") if isinstance(turn.get("message"), str) else ""
sys.stdout.write(json.dumps({"type": "text", "text": f"你说：{msg}"}, separators=(",", ":")) + "\n")
```

```js [node]
#!/usr/bin/env node
const fs = require('node:fs');
const raw = fs.readFileSync(0, 'utf8');
const turn = JSON.parse(raw.split('\n')[0] || '{}');
process.stdout.write(JSON.stringify({ type: 'text', text: `你说：${turn.message || ''}` }) + '\n');
```

:::

## 带流式输出

分片到达时发 `{"type":"partial"}` 事件。当 profile 的 `streaming: true` 时，runner 会实时转发。

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
```

如果全部内容已通过 `partial` 事件发出，就不再发 `{"type":"text"}` 事件——runner 把空回复视为「已流式送达」。

## 带会话续接

发一个 `{"type":"session"}` 事件声明 id；bridge 在下一轮把它作为 `session_id` 传回。

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

emit({"type": "session", "id": session_id})
emit({"type": "text", "text": f"[会话 {session_id[:8]}] 你说：{message}"})
```

## Profile YAML

```yaml
command: python3                     # 或 bash、node 等
args: ["./my_agent.py"]
timeout_secs: 30
```
