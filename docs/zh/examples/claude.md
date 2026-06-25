# 接入 claude CLI

本示例展示如何将 `claude` CLI 封装为符合 AgentProc 协议的 agent，支持多会话续接和流式输出。

## 工作原理

```
Bridge
  │  AGENT_MESSAGE, AGENT_SESSION_ID
  ▼
claude_bridge.py / .js
  │  claude --resume <session_id> -p <message> --output-format stream-json
  ▼
claude CLI
  │  stream-json 事件
  ▼
claude_bridge.py / .js
  │  AGENT_SESSION:<uuid>
  │  AGENT_PARTIAL:"分块内容..."
  ▼
Bridge → 用户
```

## Python 版本

```python
#!/usr/bin/env python3
import json, os, subprocess, sys

message = os.environ["AGENT_MESSAGE"]
session_id = os.environ.get("AGENT_SESSION_ID", "")
streaming = os.environ.get("AGENT_STREAMING", "1") != "0"

args = [
    "claude", "--output-format", "stream-json",
    "--dangerously-skip-permissions",
    "--disallowed-tools", "AskUserQuestion",
    "-p", message,
]
if session_id:
    args += ["--resume", session_id]

proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
found_session_id = None
last_partial = None

for line in proc.stdout:
    try:
        event = json.loads(line.strip())
    except json.JSONDecodeError:
        continue

    if event.get("type") == "assistant":
        text = "".join(
            b.get("text", "") for b in (event.get("message") or {}).get("content", [])
            if b.get("type") == "text"
        )
        if text.strip() and streaming:
            print(f"AGENT_PARTIAL:{json.dumps(text, ensure_ascii=False)}", flush=True)
            last_partial = text

    elif event.get("type") == "result":
        found_session_id = event.get("session_id")
        result_text = event.get("result", "")
        if result_text.strip() and result_text != last_partial:
            if streaming:
                print(f"AGENT_PARTIAL:{json.dumps(result_text, ensure_ascii=False)}", flush=True)
            else:
                if found_session_id:
                    print(f"AGENT_SESSION:{found_session_id}", flush=True)
                print(result_text, flush=True)
                sys.exit(0)

proc.wait()
if found_session_id:
    print(f"AGENT_SESSION:{found_session_id}", flush=True)
```

Profile YAML：

```yaml
command: python3 ./claude_bridge.py
cwd: /path/to/your/project
timeout_secs: 600
streaming: true
```

## 本地测试

```bash
AGENT_MESSAGE="你好" AGENT_SESSION_ID="" AGENT_STREAMING="1" \
python3 ./claude_bridge.py
```
