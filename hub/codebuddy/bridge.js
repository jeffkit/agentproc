#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `codebuddy` CLI (Tencent CodeBuddy).
 *
 * CodeBuddy's stream-json output schema is compatible with claude's.
 * Differences: command name `codebuddy`, resume flag `-r`, env prefix CODEBUDDY_*.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '..');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'codebuddy';
const INSTALL_HINT = 'See your internal CodeBuddy installation docs.';

function buildArgs(message, sessionId, env) {
  const args = [
    CLI_NAME, '-p', message,
    '--output-format', 'stream-json',
    '--dangerously-skip-permissions',
  ];
  const disallow = (env.CODEBUDDY_DISALLOW_TOOLS || 'AskUserQuestion').trim();
  if (disallow) {
    args.push('--disallowedTools', disallow);
  }
  const model = (env.CODEBUDDY_MODEL || '').trim();
  if (model) {
    args.push('--model', model);
  }
  if (sessionId) {
    args.push('-r', sessionId);
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
      return { sessionId, error: event.result || 'codebuddy reported an error' };
    }
    const resultText = event.result || '';
    return { sessionId, finalText: resultText || null };
  }
  return null;
}

runBridge({ cliName: CLI_NAME, cliInstallHint: INSTALL_HINT, buildArgs, parseEvent }).catch(e => {
  process.stderr.write(`[codebuddy bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
