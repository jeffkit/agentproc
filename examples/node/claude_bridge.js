#!/usr/bin/env node
'use strict';
/**
 * Example: connect claude CLI to an AgentProc bridge (Node.js, wire 0.4).
 *
 * The bridge spawns this script, writes a {"type":"turn",...} object to its
 * stdin, and reads NDJSON events from its stdout.
 *
 * Profile YAML:
 *   command: node
 *   args: ["./claude_bridge.js"]
 *   cwd: /path/to/your/project
 *   timeout_secs: 600
 *   streaming: true
 */

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

  const args = [
    '--output-format', 'stream-json',
    '--dangerously-skip-permissions',
    '--disallowed-tools', 'AskUserQuestion',
    '-p', message,
  ];
  if (sessionId) args.push('--resume', sessionId);

  const child = spawn('claude', args, { stdio: ['ignore', 'pipe', 'pipe'] });

  let foundSessionId = null;
  let lastFinal = null;
  let lastPartial = null;
  let errorMessage = null;
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
        .filter(b => b.type === 'text')
        .map(b => b.text)
        .join('');
      if (text) {
        const partial = { type: 'partial', text };
        if (foundSessionId) partial.session_id = foundSessionId;
        emit(partial);
        lastPartial = text;
      }
    } else if (event.type === 'result') {
      if (event.session_id) foundSessionId = event.session_id;
      if (event.is_error) {
        errorMessage = event.result || 'claude reported an error';
      } else {
        const resultText = event.result || '';
        if (resultText) lastFinal = resultText;
      }
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
      const s = stderrBuf.trim();
      emit({ type: 'error', message: `claude exited with ${code}: ${s.slice(0, 500)}` });
      process.exit(1);
    }
    const reply = (lastFinal !== null) ? lastFinal : lastPartial;
    const out = { type: 'result', text: reply || '' };
    if (foundSessionId) out.session_id = foundSessionId;
    emit(out);
    process.exit(0);
  });
})();
