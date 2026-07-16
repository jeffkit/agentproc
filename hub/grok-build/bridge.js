#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `grok` CLI (xAI Grok Build).
 *
 * Invokes:
 *   grok -p <message> --output-format streaming-json \
 *       --always-approve --no-auto-update \
 *       [-r <session_id>] [-m <model>]
 *
 * Schema (verified against grok 0.2.101):
 *   text     → coalesced into block-sized partial (not per-token)
 *   thought  → ignored
 *   end      → sessionId + accumulated finalText
 *   error    → error
 *
 * Grok emits near token-sized ``text`` events; Claude Code emits larger
 * assistant content blocks. We coalesce here to match that block shape.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'grok';
const INSTALL_HINT = 'Install: curl -fsSL https://x.ai/cli/install.sh | bash';

const SOFT_CHARS = 40;
const HARD_CHARS = 80;
const BOUNDARY = new Set(['\n', '。', '！', '？', '；', '.', '!', '?', ';']);

function shouldFlush(buf) {
  if (!buf) return false;
  if (buf.length >= HARD_CHARS) return true;
  const last = buf[buf.length - 1];
  if (BOUNDARY.has(last) && buf.length >= SOFT_CHARS) return true;
  if (last === '\n') return true;
  return false;
}

function buildArgs(message, sessionId, env) {
  const args = [
    CLI_NAME, '-p', message,
    '--output-format', 'streaming-json',
    '--always-approve',
    '--no-auto-update',
  ];
  const model = (env.GROK_MODEL || '').trim();
  if (model) {
    args.push('-m', model);
  }
  if (sessionId) {
    args.push('-r', sessionId);
  }
  return args;
}

function makeParseEvent() {
  const full = [];
  let pending = '';

  function flushPending() {
    if (!pending) return null;
    const chunk = pending;
    pending = '';
    return chunk;
  }

  return function parseEvent(event) {
    const etype = event.type;
    if (etype === 'text') {
      const data = event.data || '';
      if (!data) return null;
      full.push(data);
      pending += data;
      if (shouldFlush(pending)) {
        return { partialText: flushPending() };
      }
      return null;
    }
    if (etype === 'thought') {
      return null;
    }
    if (etype === 'end') {
      const sid = event.sessionId;
      const leftover = flushPending();
      return {
        sessionId: (typeof sid === 'string' && sid) ? sid : undefined,
        partialText: leftover || undefined,
        finalText: full.join(''),
      };
    }
    if (etype === 'error') {
      const sid = event.sessionId;
      pending = '';
      return {
        sessionId: (typeof sid === 'string' && sid) ? sid : undefined,
        error: event.message || 'grok reported an error',
      };
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
  process.stderr.write(`[grok-build bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
