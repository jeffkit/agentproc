# Connect claude CLI

This example shows how to wrap the `claude` CLI as an AgentProc-compliant agent with full session continuity and streaming support. (The canonical version lives at `examples/python/claude_bridge.py` and `examples/node/claude_bridge.js`.)

## How it works

```
Bridge
  │  {"type":"turn","message":"...","session_id":"..."} on stdin
  ▼
claude_bridge.py / .js
  │  claude --resume <session_id> -p <message> --output-format stream-json
  ▼
claude CLI
  │  stream-json events on stdout
  ▼
claude_bridge.py / .js
  │  {"type":"partial","text":"chunk...","session_id":"<uuid>"}
  │  {"type":"result","text":"<final reply>","session_id":"<uuid>"}
  ▼
Bridge → user
```

## Python

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

    if event.get("type") == "system" and event.get("subtype") == "init":
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            found_session_id = sid
    elif event.get("type") == "assistant":
        text = "".join(b.get("text", "") for b in (event.get("message") or {}).get("content", [])
                       if b.get("type") == "text")
        if text:
            partial = {"type": "partial", "text": text}
            if found_session_id:
                partial["session_id"] = found_session_id
            emit(partial)
            last_partial = text
    elif event.get("type") == "result":
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            found_session_id = sid
        if event.get("is_error"):
            error_message = event.get("result", "claude reported an error")
        else:
            result_text = event.get("result", "")
            if result_text:
                last_final = result_text

proc.wait()

if error_message:
    err = {"type": "error", "message": error_message}
    if found_session_id:
        err["session_id"] = found_session_id
    emit(err)
    sys.exit(1)

if proc.returncode != 0 and not found_session_id:
    stderr = (proc.stderr.read() if proc.stderr else "").strip()
    emit({"type": "error", "message": f"claude exited with {proc.returncode}: {stderr[:500]}"})
    sys.exit(1)

reply = last_final if last_final is not None else last_partial
out = {"type": "result", "text": reply or ""}
if found_session_id:
    out["session_id"] = found_session_id
emit(out)
sys.exit(0)
```

Profile YAML:

```yaml
command: python3
args: ["./claude_bridge.py"]
cwd: /path/to/your/project
timeout_secs: 600
streaming: true
```

## Node.js

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

    if (event.type === 'system' && event.subtype === 'init' && event.session_id) {
      foundSessionId = event.session_id;
    } else if (event.type === 'assistant') {
      const text = (event.message?.content || [])
        .filter(b => b.type === 'text').map(b => b.text).join('');
      if (text) {
        const partial = { type: 'partial', text };
        if (foundSessionId) partial.session_id = foundSessionId;
        emit(partial);
        lastPartial = text;
      }
    } else if (event.type === 'result') {
      if (event.session_id) foundSessionId = event.session_id;
      if (event.is_error) errorMessage = event.result || 'claude reported an error';
      else { const rt = event.result || ''; if (rt) lastFinal = rt; }
    }
  });

  child.on('close', code => {
    if (errorMessage) {
      const err = { type: 'error', message: errorMessage };
      if (foundSessionId) err.session_id = foundSessionId;
      emit(err);
      process.exit(1);
    }
    if (code !== 0 && !foundSessionId) {
      emit({ type: 'error', message: `claude exited with ${code}: ${stderrBuf.trim().slice(0, 500)}` });
      process.exit(1);
    }
    const reply = (lastFinal !== null) ? lastFinal : lastPartial;
    const out = { type: 'result', text: reply || '' };
    if (foundSessionId) out.session_id = foundSessionId;
    emit(out);
    process.exit(0);
  });
})();
```

Profile YAML:

```yaml
command: node
args: ["./claude_bridge.js"]
cwd: /path/to/your/project
timeout_secs: 600
streaming: true
```

## Local testing

Drive the bridge the same way the runner does — pipe a turn object to stdin:

```bash
echo '{"type":"turn","message":"hello","session_id":"","from_user":"test","protocol_version":"0.4"}' \
  | python3 ./claude_bridge.py
```
