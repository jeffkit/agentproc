#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `gemini` CLI (Google Gemini CLI).
 *
 * Invokes:
 *   gemini -p <message> --output-format stream-json --yolo \
 *       [--resume <session_id>] [--model <model>]
 *
 * Parses the NDJSON stream via the shared stream_utils.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'gemini';
const INSTALL_HINT = 'Install: npm install -g @google/gemini-cli';

function buildArgs(message, sessionId, env) {
  const args = [
    CLI_NAME, '-p', message,
    '--output-format', 'stream-json',
    '--yolo',
  ];
  if ((env.GEMINI_SANDBOX || '').trim().toLowerCase() === 'false') {
    args.push('--sandbox', 'false');
  }
  const model = (env.GEMINI_MODEL || '').trim();
  if (model) {
    args.push('--model', model);
  }
  if (sessionId) {
    args.push('--resume', sessionId);
  }
  return args;
}

function parseEvent(event) {
  const etype = event.type;
  if (etype === 'init') {
    return { sessionId: event.session_id };
  }
  if (etype === 'message') {
    if (event.role !== 'assistant') return null;
    const text = event.content || '';
    if (!text) return null;
    // delta=true means streaming chunk; delta=false or absent means the full
    // message text. Treat delta as partial; non-delta as terminal final_text.
    if (event.delta) {
      return { partialText: text };
    }
    return { finalText: text };
  }
  if (etype === 'error') {
    if (event.severity === 'error') {
      return { error: event.message || 'gemini reported an error' };
    }
    return null;
  }
  if (etype === 'result') {
    if (event.status === 'error') {
      const err = event.error || {};
      return { error: err.message || 'gemini turn failed' };
    }
    return null;
  }
  return null;
}

runBridge({ cliName: CLI_NAME, cliInstallHint: INSTALL_HINT, buildArgs, parseEvent }).catch(e => {
  process.stderr.write(`[gemini-cli bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
