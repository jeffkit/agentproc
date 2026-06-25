# Connect claude CLI

This example shows how to wrap the `claude` CLI as an AgentProc-compliant agent with full session continuity and streaming support.

## How it works

```
Bridge
  │  AGENT_MESSAGE, AGENT_SESSION_ID
  ▼
claude_bridge.py / .js
  │  claude --resume <session_id> -p <message> --output-format stream-json
  ▼
claude CLI
  │  stream-json events on stdout
  ▼
claude_bridge.py / .js
  │  AGENT_SESSION:<uuid>
  │  AGENT_PARTIAL:"chunk..."
  ▼
Bridge → user
```

## Python

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

Profile YAML:

```yaml
command: python3 ./claude_bridge.py
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

const message = process.env.AGENT_MESSAGE || '';
const sessionId = process.env.AGENT_SESSION_ID || '';
const streaming = (process.env.AGENT_STREAMING || '1') !== '0';

const args = [
  '--output-format', 'stream-json',
  '--dangerously-skip-permissions',
  '--disallowed-tools', 'AskUserQuestion',
  '-p', message,
];
if (sessionId) args.push('--resume', sessionId);

const child = spawn('claude', args, { stdio: ['ignore', 'pipe', 'pipe'] });
const rl = readline.createInterface({ input: child.stdout });

let foundSessionId = null;
let lastPartial = null;

rl.on('line', line => {
  let event;
  try { event = JSON.parse(line.trim()); } catch { return; }

  if (event.type === 'assistant') {
    const text = (event.message?.content || [])
      .filter(b => b.type === 'text').map(b => b.text).join('');
    if (text.trim() && streaming) {
      process.stdout.write(`AGENT_PARTIAL:${JSON.stringify(text)}\n`);
      lastPartial = text;
    }
  } else if (event.type === 'result') {
    foundSessionId = event.session_id;
    const resultText = event.result || '';
    if (resultText.trim() && resultText !== lastPartial) {
      if (streaming) process.stdout.write(`AGENT_PARTIAL:${JSON.stringify(resultText)}\n`);
      else {
        if (foundSessionId) process.stdout.write(`AGENT_SESSION:${foundSessionId}\n`);
        process.stdout.write(resultText + '\n');
        process.exit(0);
      }
    }
  }
});

child.on('close', () => {
  if (foundSessionId) process.stdout.write(`AGENT_SESSION:${foundSessionId}\n`);
  process.exit(0);
});
```

Profile YAML:

```yaml
command: node ./claude_bridge.js
cwd: /path/to/your/project
timeout_secs: 600
streaming: true
```

## Local testing

```bash
AGENT_MESSAGE="hello" AGENT_SESSION_ID="" AGENT_STREAMING="1" \
python3 ./claude_bridge.py
```
