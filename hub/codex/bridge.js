#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `codex` CLI (OpenAI Codex).
 *
 * Invokes:
 *   codex exec --json <message>
 *   codex exec resume --json <thread_id> <message>   // when AGENT_SESSION_ID is set
 *
 * Parses the NDJSON stream via the shared stream_utils.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'codex';
const INSTALL_HINT = 'Install: npm install -g @openai/codex';

function buildArgs(message, sessionId, env) {
  const model = (env.CODEX_MODEL || '').trim();
  if (sessionId) {
    // --json MUST be present on the resume path too, otherwise codex emits
    // non-NDJSON output that the bridge cannot parse.
    const args = [CLI_NAME, 'exec', 'resume', '--json', sessionId, message];
    if (model) args.push('-c', `model="${model}"`);
    return args;
  }
  const args = [CLI_NAME, 'exec', '--json', message];
  if (model) {
    args.push('-c', `model="${model}"`);
  }
  return args;
}

function parseEvent(event) {
  const etype = event.type;
  if (etype === 'thread.started') {
    return { sessionId: event.thread_id };
  }
  if (etype === 'item.completed') {
    const item = event.item || {};
    if (item.type === 'agent_message') {
      const text = item.text || '';
      return text ? { partialText: text } : null;
    }
    return null;
  }
  if (etype === 'turn.failed') {
    return { error: String(event.error || 'codex turn failed') };
  }
  return null;
}

runBridge({ cliName: CLI_NAME, cliInstallHint: INSTALL_HINT, buildArgs, parseEvent }).catch(e => {
  process.stderr.write(`[codex bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
