#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the Cursor Agent CLI (`agent`).
 *
 * The Cursor Agent binary is named `agent` (NOT `cursor`).
 *
 * Invokes:
 *   agent -p <message> --output-format stream-json \
 *       --stream-partial-output --yolo \
 *       [--resume <session_id>] [--model <model>]
 *
 * Schema matches claude-code (content blocks, result with session_id).
 * Cursor emits a duplicate full-text assistant event at the end of a streamed
 * turn — the bridge tracks accumulated text and suppresses it.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'agent';
const INSTALL_HINT = 'Install: brew install cursor-agent  (then run `agent login`)';

function buildArgs(message, sessionId, env) {
  const args = [
    CLI_NAME, '-p', message,
    '--output-format', 'stream-json',
    '--stream-partial-output',
  ];
  if ((env.CURSOR_FORCE || '1') === '1') {
    args.push('--yolo');
  }
  const model = (env.CURSOR_MODEL || '').trim();
  if (model) {
    args.push('--model', model);
  }
  if (sessionId) {
    args.push('--resume', sessionId);
  }
  return args;
}

function makeParseEvent() {
  const accumulated = [];
  return function parseEvent(event) {
    const etype = event.type;
    if (etype === 'system' && event.subtype === 'init') {
      return { sessionId: event.session_id };
    }
    if (etype === 'assistant') {
      const msg = event.message || {};
      const text = (msg.content || [])
        .filter(b => b.type === 'text')
        .map(b => b.text)
        .join('');
      if (!text) return null;
      // If text equals what we've already streamed, this is Cursor's
      // duplicate "full assembled" event — drop it.
      if (text === accumulated.join('')) return null;
      accumulated.push(text);
      return { partialText: text };
    }
    if (etype === 'result') {
      const sessionId = event.session_id;
      if (event.is_error || event.subtype === 'error') {
        return { sessionId, error: event.result || 'cursor agent reported an error' };
      }
      return { sessionId, finalText: event.result || null };
    }
    return null;
  };
}

runBridge({
  cliName: CLI_NAME,
  cliInstallHint: INSTALL_HINT,
  buildArgs,
  parseEvent: makeParseEvent(),
}).catch(e => {
  process.stderr.write(`[cursor bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
