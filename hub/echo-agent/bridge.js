#!/usr/bin/env node
'use strict';
/**
 * Minimal AgentProc echo agent (Node.js, wire 0.3).
 *
 * Reads the {"type":"turn",...} object from stdin and writes the message back
 * as a single {"type":"text"} event. No external dependencies, no AI calls.
 * Use this to verify your messaging bridge speaks the protocol correctly.
 */

const readline = require('node:readline');

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  let turn = {};
  try { turn = JSON.parse(line); } catch { /* empty turn */ }
  const message = (typeof turn.message === 'string') ? turn.message : '';
  process.stdout.write(JSON.stringify({ type: 'text', text: `You said: ${message}` }) + '\n');
  process.exit(0);
});
rl.on('close', () => {
  // No turn line at all — echo an empty message.
  process.stdout.write(JSON.stringify({ type: 'text', text: 'You said: ' }) + '\n');
  process.exit(0);
});
