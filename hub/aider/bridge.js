#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `aider` AI coding assistant (wire 0.3).
 *
 *   aider --message <message> --yes-always --no-show-release-notes --no-stream
 *         [--model <model>]
 *
 * aider modifies files in cwd and may make git commits. Its stdout (a
 * human-readable summary) is forwarded as the reply body (a single
 * {"type":"text"} event). No session id is emitted.
 *
 * Per-CLI config (read from the process env the runner injects):
 *   AIDER_MODEL   Optional model override (e.g. "claude-opus-4-5")
 *   AIDER_TIMEOUT Process timeout in seconds (default 600)
 */

const path = require('node:path');
const { runPlainCli } = require(path.join(__dirname, '..', '_shared', 'stream_utils.js'));

const CLI_NAME = 'aider';
const INSTALL_HINT = 'Install: pip install aider-chat';

function buildArgs(message) {
  const args = [
    CLI_NAME,
    '--message', message,
    '--yes-always',
    '--no-show-release-notes',
    '--no-stream',
  ];
  const model = (process.env.AIDER_MODEL || '').trim();
  if (model) args.push('--model', model);
  return args;
}

runPlainCli({
  cliName: CLI_NAME,
  cliInstallHint: INSTALL_HINT,
  buildArgs,
  timeoutEnv: 'AIDER_TIMEOUT',
  defaultTimeout: 600,
}).catch(e => {
  process.stderr.write(`[aider bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
