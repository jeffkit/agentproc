#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the DeepSeek TUI CLI (wire 0.3).
 *
 *   deepseek exec -p <message> [--model <model>]
 *
 * deepseek exec returns plain text — no streaming, no session continuity. The
 * bridge forwards the text as the reply body (a single {"type":"result"} event).
 *
 * Per-CLI config (read from the process env the runner injects):
 *   DEEPSEEK_MODEL   Optional model override (default: deepseek-v4-pro)
 *   DEEPSEEK_API_KEY Optional API key (alternative to `deepseek login`)
 *   DEEPSEEK_TIMEOUT Process timeout in seconds (default: 300)
 */

const path = require('node:path');
const { runPlainCli } = require(path.join(__dirname, '..', '_shared', 'stream_utils.js'));

const CLI_NAME = 'deepseek';
const INSTALL_HINT = 'Install from https://deepseek.com/downloads or: brew install deepseek';

function buildArgs(message) {
  const args = [CLI_NAME, 'exec', '-p', message];
  const model = (process.env.DEEPSEEK_MODEL || '').trim();
  if (model) args.push('--model', model);
  return args;
}

runPlainCli({
  cliName: CLI_NAME,
  cliInstallHint: INSTALL_HINT,
  buildArgs,
  timeoutEnv: 'DEEPSEEK_TIMEOUT',
  defaultTimeout: 300,
}).catch(e => {
  process.stderr.write(`[deepseek bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
