#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `agy` CLI.
 *
 * agy's --print mode returns the full reply as plain text — no streaming,
 * no exposed session id. The bridge just forwards the text as the AgentProc
 * reply body.
 *
 * Env vars:
 *   AGENT_MESSAGE          User message
 *   AGENT_STREAMING        Ignored — agy doesn't stream
 *   AGY_MODEL              Optional model override
 *   AGY_DANGEROUSLY_SKIP_PERMISSIONS  "1" (default) adds the flag
 *   AGY_TIMEOUT            Optional timeout in seconds (default 300)
 */

const { spawn } = require('node:child_process');

function buildArgs(message) {
  const args = ['agy', '--print', message];
  if ((process.env.AGY_DANGEROUSLY_SKIP_PERMISSIONS || '1') === '1') {
    args.push('--dangerously-skip-permissions');
  }
  const model = (process.env.AGY_MODEL || '').trim();
  if (model) {
    args.push('--model', model);
  }
  return args;
}

function emit(line) {
  process.stdout.write(line + '\n');
}

async function main() {
  const message = process.env.AGENT_MESSAGE;
  if (!message) {
    emit(`AGENT_ERROR:${JSON.stringify('AGENT_MESSAGE env var is required')}`);
    process.exit(1);
  }

  const args = buildArgs(message);
  let child;
  try {
    child = spawn(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    emit(`AGENT_ERROR:${JSON.stringify('agy CLI not found. See the agy project for installation instructions.')}`);
    process.exit(1);
  }

  let stdout = '';
  let stderr = '';
  child.stdout.on('data', d => { stdout += d.toString(); });
  child.stderr.on('data', d => { stderr += d.toString(); });

  const timeoutSecs = parseInt(process.env.AGY_TIMEOUT || '300', 10);
  const timer = setTimeout(() => {
    child.kill('SIGTERM');
    emit(`AGENT_ERROR:${JSON.stringify('agy timed out')}`);
    process.exit(124);
  }, timeoutSecs * 1000);

  const code = await new Promise(resolve => child.on('close', resolve));
  clearTimeout(timer);

  if (code !== 0) {
    let msg = `agy exited with ${code}`;
    const s = stderr.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emit(`AGENT_ERROR:${JSON.stringify(msg)}`);
    process.exit(1);
  }

  const text = stdout.trim();
  if (!text) {
    emit(`AGENT_ERROR:${JSON.stringify('agy returned empty output')}`);
    process.exit(1);
  }
  emit(text);
  process.exit(0);
}

main().catch(e => {
  process.stderr.write(`[agy bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
