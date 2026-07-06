#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `opencode` CLI.
 *
 * Invokes:
 *   opencode run <message> --auto --format json \
 *       [--session <session_id>] [--model <model>]
 *
 * Parses the NDJSON stream via the shared stream_utils.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'opencode';
const INSTALL_HINT = 'Install: npm install -g opencode-ai  (or: curl -fsSL https://opencode.ai/install | bash)';

function buildArgs(message, sessionId, env) {
  const args = ['opencode', 'run', message, '--auto', '--format', 'json'];
  if (sessionId) {
    args.push('--session', sessionId);
  }
  const model = (env.OPENCODE_MODEL || '').trim();
  if (model) {
    args.push('--model', model);
  }
  return args;
}

function parseEvent(event) {
  const etype = event.type;
  const sessionId = event.sessionID || null;
  const part = event.part || {};

  if (etype === 'text') {
    const text = part.text || '';
    if (text) {
      return { sessionId, partialText: text };
    }
    return sessionId ? { sessionId } : null;
  }

  if (etype === 'step_start' || etype === 'step_finish' || etype === 'tool_use') {
    return sessionId ? { sessionId } : null;
  }

  if (etype === 'error') {
    const err =
      part.message ||
      (event.error && event.error.message) ||
      'opencode reported an error';
    return { sessionId, error: err };
  }

  return null;
}

runBridge({ cliName: CLI_NAME, cliInstallHint: INSTALL_HINT, buildArgs, parseEvent }).catch(e => {
  process.stderr.write(`[opencode bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
