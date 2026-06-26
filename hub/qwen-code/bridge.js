#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `qwen` CLI (Alibaba Qwen Code).
 *
 * Qwen Code is a fork of gemini-cli; its stream-json schema matches gemini's.
 * Thin variant: command `qwen`, env prefix QWEN_*.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'qwen';
const INSTALL_HINT = 'Install: npm install -g @qwen-code/qwen-code';

function buildArgs(message, sessionId, env) {
  const args = [
    CLI_NAME, '-p', message,
    '--output-format', 'stream-json',
    '--yolo',
  ];
  if ((env.QWEN_SANDBOX || '').trim().toLowerCase() === 'false') {
    args.push('--sandbox', 'false');
  }
  const model = (env.QWEN_MODEL || '').trim();
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
    if (event.delta) {
      return { partialText: text };
    }
    return { finalText: text };
  }
  if (etype === 'error') {
    if (event.severity === 'error') {
      return { error: event.message || 'qwen reported an error' };
    }
    return null;
  }
  if (etype === 'result') {
    if (event.status === 'error') {
      const err = event.error || {};
      return { error: err.message || 'qwen turn failed' };
    }
    return null;
  }
  return null;
}

runBridge({ cliName: CLI_NAME, cliInstallHint: INSTALL_HINT, buildArgs, parseEvent }).catch(e => {
  process.stderr.write(`[qwen-code bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
