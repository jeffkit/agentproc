#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `claude` CLI (Anthropic Claude Code).
 *
 * Invokes:
 *   claude -p <message> --output-format stream-json \
 *       --dangerously-skip-permissions \
 *       --disallowed-tools AskUserQuestion \
 *       [--resume <session_id>] [--model <model>]
 *
 * Re-emits the stream as AgentProc protocol output via the shared stream_utils.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'claude';
const INSTALL_HINT = 'Install: npm install -g @anthropic-ai/claude-code';

function buildArgs(message, sessionId, env) {
  const args = [
    CLI_NAME, '-p', message,
    '--output-format', 'stream-json',
    '--dangerously-skip-permissions',
  ];
  const disallow = (env.CLAUDE_DISALLOW_TOOLS || 'AskUserQuestion').trim();
  if (disallow) {
    args.push('--disallowed-tools', disallow);
  }
  const model = (env.CLAUDE_MODEL || '').trim();
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
  if (etype === 'assistant') {
    const text = (event.message?.content || [])
      .filter(b => b.type === 'text')
      .map(b => b.text)
      .join('');
    return text ? { partialText: text } : null;
  }
  if (etype === 'result') {
    const sessionId = event.session_id;
    if (event.is_error) {
      return { sessionId, error: event.result || 'claude reported an error' };
    }
    const resultText = event.result || '';
    return { sessionId, finalText: resultText || null };
  }
  return null;
}

runBridge({ cliName: CLI_NAME, cliInstallHint: INSTALL_HINT, buildArgs, parseEvent }).catch(e => {
  process.stderr.write(`[claude-code bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
