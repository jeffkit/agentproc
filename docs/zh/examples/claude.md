# 接入 claude CLI

本示例展示如何将 `claude` CLI 封装为符合 AgentProc 协议的 agent，支持多会话续接和流式输出。（规范版本位于仓库的 `examples/python/claude_bridge.py` 和 `examples/node/claude_bridge.js`。）

## 工作原理

```
Bridge
  │  stdin 写入 {"type":"turn","message":"...","session_id":"..."}
  ▼
claude_bridge.py / .js
  │  claude --resume <session_id> -p <message> --output-format stream-json
  ▼
claude CLI
  │  stream-json 事件
  ▼
claude_bridge.py / .js
  │  {"type":"partial","text":"分块内容..."}
  │  {"type":"session","id":"<uuid>"}
  │  {"type":"text","text":"<最终回复>"}
  ▼
Bridge → 用户
```

## Python 版本

```python
#!/usr/bin/env python3
import json, subprocess, sys

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

turn = json.loads(sys.stdin.readline().rstrip("\r\n") or "{}")
message = turn.get("message") if isinstance(turn.get("message"), str) else ""
session_id = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""

args = ["claude", "--output-format", "stream-json",
        "--dangerously-skip-permissions", "--disallowed-tools", "AskUserQuestion",
        "-p", message]
if session_id:
    args += ["--resume", session_id]

proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

found_session_id = None
last_final = None
last_partial = None
error_message = None

for line in proc.stdout:
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue

    if event.get("type") == "assistant":
        text = "".join(b.get("text", "") for b in (event.get("message") or {}).get("content", [])
                       if b.get("type") == "text")
        if text:
            emit({"type": "partial", "text": text})
            last_partial = text
    elif event.get("type") == "result":
        found_session_id = event.get("session_id")
        if event.get("is_error"):
            error_message = event.get("result", "claude reported an error")
        else:
            result_text = event.get("result", "")
            if result_text:
                last_final = result_text

proc.wait()

if error_message:
    if found_session_id:
        emit({"type": "session", "id": found_session_id})
    emit({"type": "error", "message": error_message})
    sys.exit(1)

if proc.returncode != 0 and not found_session_id:
    stderr = (proc.stderr.read() if proc.stderr else "").strip()
    emit({"type": "error", "message": f"claude exited with {proc.returncode}: {stderr[:500]}"})
    sys.exit(1)

if found_session_id:
    emit({"type": "session", "id": found_session_id})
reply = last_final if last_final is not None else last_partial
if reply:
    emit({"type": "text", "text": reply})
sys.exit(0)
```

Profile YAML：

```yaml
command: python3
args: ["./claude_bridge.py"]
cwd: /path/to/your/project
timeout_secs: 600
streaming: true
```

## Node.js 版本

```js
#!/usr/bin/env node
'use strict';
const { spawn } = require('child_process');
const readline = require('readline');

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

(async function main() {
  let turn = {};
  await new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin });
    rl.on('line', (line) => {
      try { turn = JSON.parse(line); } catch { /* empty turn */ }
      rl.close();
      resolve();
    });
    rl.on('close', () => resolve());
  });

  const message = (typeof turn.message === 'string') ? turn.message : '';
  const sessionId = (typeof turn.session_id === 'string') ? turn.session_id : '';

  const args = ['--output-format', 'stream-json',
                '--dangerously-skip-permissions', '--disallowed-tools', 'AskUserQuestion',
                '-p', message];
  if (sessionId) args.push('--resume', sessionId);

  const child = spawn('claude', args, { stdio: ['ignore', 'pipe', 'pipe'] });

  let foundSessionId = null, lastFinal = null, lastPartial = null, errorMessage = null;
  let stderrBuf = '';
  child.stderr.on('data', d => { stderrBuf += d.toString(); });

  const rl = readline.createInterface({ input: child.stdout });
  rl.on('line', line => {
    line = line.trim();
    if (!line) return;
    let event;
    try { event = JSON.parse(line); } catch { return; }

    if (event.type === 'assistant') {
      const text = (event.message?.content || [])
        .filter(b => b.type === 'text').map(b => b.text).join('');
      if (text) { emit({ type: 'partial', text }); lastPartial = text; }
    } else if (event.type === 'result') {
      foundSessionId = event.session_id;
      if (event.is_error) errorMessage = event.result || 'claude reported an error';
      else { const rt = event.result || ''; if (rt) lastFinal = rt; }
    }
  });

  child.on('close', code => {
    if (errorMessage) {
      if (foundSessionId) emit({ type: 'session', id: foundSessionId });
      emit({ type: 'error', message: errorMessage });
      process.exit(1);
    }
    if (code !== 0 && !foundSessionId) {
      emit({ type: 'error', message: `claude exited with ${code}: ${stderrBuf.trim().slice(0, 500)}` });
      process.exit(1);
    }
    if (foundSessionId) emit({ type: 'session', id: foundSessionId });
    const reply = (lastFinal !== null) ? lastFinal : lastPartial;
    if (reply) emit({ type: 'text', text: reply });
    process.exit(0);
  });
})();
```

Profile YAML：

```yaml
command: node
args: ["./claude_bridge.js"]
cwd: /path/to/your/project
timeout_secs: 600
streaming: true
```

## 本地测试

像 runner 那样驱动 bridge——把 turn 对象 pipe 给 stdin：

```bash
echo '{"type":"turn","message":"你好","session_id":"","from_user":"test","protocol_version":"0.3"}' \
  | python3 ./claude_bridge.py
```
