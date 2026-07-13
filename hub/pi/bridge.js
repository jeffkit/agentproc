#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `pi` coding agent CLI (wire 0.3).
 *
 * pi's --print mode returns the full reply as plain text — no streaming, no
 * exposed session id. The bridge forwards the text as the reply body (a single
 * {"type":"text"} event).
 *
 * Per-CLI config (read from the process env the runner injects):
 *   PI_MODEL         Optional model override (e.g. "anthropic/claude-opus-4-5")
 *   PI_NO_EXTENSIONS "1" (default) adds --no-extensions to prevent hanging
 *   PI_TIMEOUT       Process timeout in seconds (default 600)
 */

const path = require('node:path');
const { runPlainCli } = require(path.join(__dirname, '..', '_shared', 'stream_utils.js'));

const CLI_NAME = 'pi';
const INSTALL_HINT = 'Install: npm install -g @earendil-works/pi-coding-agent';

function buildArgs(message) {
  const args = [CLI_NAME, '-p', message, '--approve'];
  if ((process.env.PI_NO_EXTENSIONS || '1') !== '0') args.push('--no-extensions');
  const model = (process.env.PI_MODEL || '').trim();
  if (model) args.push('--model', model);
  return args;
}

runPlainCli({
  cliName: CLI_NAME,
  cliInstallHint: INSTALL_HINT,
  buildArgs,
  timeoutEnv: 'PI_TIMEOUT',
  defaultTimeout: 600,
}).catch(e => {
  process.stderr.write(`[pi bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
