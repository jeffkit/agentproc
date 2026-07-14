#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `agy` CLI (wire 0.3).
 *
 * agy's --print mode returns the full reply as plain text — no streaming, no
 * exposed session id. The bridge forwards the text as the reply body (a single
 * {"type":"result"} event).
 *
 * Per-CLI config (read from the process env the runner injects):
 *   AGY_MODEL   Optional model override
 *   AGY_DANGEROUSLY_SKIP_PERMISSIONS  "1" (default) adds the flag
 *   AGY_TIMEOUT Optional timeout in seconds (default 300)
 */

const path = require('node:path');
const { runPlainCli } = require(path.join(__dirname, '..', '_shared', 'stream_utils.js'));

const CLI_NAME = 'agy';
const INSTALL_HINT = 'See the agy project for installation instructions.';

function buildArgs(message) {
  const args = [CLI_NAME, '--print', message];
  if ((process.env.AGY_DANGEROUSLY_SKIP_PERMISSIONS || '1') === '1') {
    args.push('--dangerously-skip-permissions');
  }
  const model = (process.env.AGY_MODEL || '').trim();
  if (model) args.push('--model', model);
  return args;
}

runPlainCli({
  cliName: CLI_NAME,
  cliInstallHint: INSTALL_HINT,
  buildArgs,
  timeoutEnv: 'AGY_TIMEOUT',
  defaultTimeout: 300,
}).catch(e => {
  process.stderr.write(`[agy bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
