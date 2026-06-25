#!/usr/bin/env node
/**
 * Example: connect claude CLI to AgentProc bridge (Node.js)
 *
 * Profile YAML:
 *   command: node ./claude_bridge.js
 *   cwd: /path/to/your/project
 *   timeout_secs: 600
 *   streaming: true
 */

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
  line = line.trim();
  if (!line) return;
  let event;
  try { event = JSON.parse(line); } catch { return; }

  if (event.type === 'assistant') {
    const text = (event.message?.content || [])
      .filter(b => b.type === 'text')
      .map(b => b.text)
      .join('');
    if (text.trim() && streaming) {
      process.stdout.write(`AGENT_PARTIAL:${JSON.stringify(text)}\n`);
      lastPartial = text;
    }
  } else if (event.type === 'result') {
    foundSessionId = event.session_id;
    const resultText = event.result || '';
    if (resultText.trim() && resultText !== lastPartial) {
      if (streaming) {
        process.stdout.write(`AGENT_PARTIAL:${JSON.stringify(resultText)}\n`);
      } else {
        if (foundSessionId) process.stdout.write(`AGENT_SESSION:${foundSessionId}\n`);
        process.stdout.write(resultText + '\n');
        process.exit(0);
      }
    }
  }
});

child.on('close', code => {
  if (code !== 0 && !foundSessionId) {
    process.stderr.write(`claude exited with ${code}\n`);
    process.exit(1);
  }
  if (foundSessionId) {
    process.stdout.write(`AGENT_SESSION:${foundSessionId}\n`);
  }
  process.exit(0);
});
