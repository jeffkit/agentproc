# 裸脚本（无 SDK）

不需要 SDK。任何读取 `AGENT_*` 环境变量并写入 stdout 的脚本都是合法的 AgentProc agent。

## Echo agent

::: code-group

```bash [bash]
#!/usr/bin/env bash
echo "你说：$AGENT_MESSAGE"
```

```python [python]
#!/usr/bin/env python3
import os
print(f"你说：{os.environ['AGENT_MESSAGE']}")
```

```js [node]
#!/usr/bin/env node
console.log(`你说：${process.env.AGENT_MESSAGE}`);
```

:::

## 带流式输出

```python
#!/usr/bin/env python3
import json, os, sys, time

message = os.environ["AGENT_MESSAGE"]

for i, word in enumerate(message.split()):
    chunk = word + (" " if i < len(message.split()) - 1 else "")
    print(f"AGENT_PARTIAL:{json.dumps(chunk, ensure_ascii=False)}", flush=True)
    time.sleep(0.05)
```

## 带会话续接

```python
#!/usr/bin/env python3
import os, uuid

message = os.environ["AGENT_MESSAGE"]
session_id = os.environ.get("AGENT_SESSION_ID", "") or str(uuid.uuid4())

print(f"AGENT_SESSION:{session_id}", flush=True)
print(f"[会话 {session_id[:8]}] 你说：{message}")
```

## Profile YAML

```yaml
command: python3 ./my_agent.py
timeout_secs: 30
```
