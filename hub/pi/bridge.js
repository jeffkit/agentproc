#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `pi` coding agent CLI.
 *
 * pi's --print mode returns the full reply as plain text — no streaming,
 * no exposed session id. The bridge forwards the text as the AgentProc
 * reply body.
 *
 * Env vars:
 *   AGENT_MESSAGE          User message
 *   AGENT_STREAMING        Ignored — pi -p doesn't stream
 *   PI_MODEL               Optional model override (e.g. "anthropic/claude-opus-4-5")
 *   PI_NO_EXTENSIONS       "1" (default) adds --no-extensions to prevent hanging
 *   PI_TIMEOUT             Process timeout in seconds (default 600)
 */

const { spawn } = require('node:child_process');

function buildArgs(message) {
  const args = ['pi', '-p', message, '--approve'];
  if ((process.env.PI_NO_EXTENSIONS || '1') !== '0') {
    args.push('--no-extensions');
  }
  const model = (process.env.PI_MODEL || '').trim();
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
  const child = spawn(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });

  let stdout = '';
  let stderr = '';
  let spawnError = null;

  child.on('error', err => {
    // spawn-side ENOENT surfaces here, not as a synchronous throw.
    spawnError = err;
  });
  child.stdout.on('data', d => { stdout += d.toString(); });
  child.stderr.on('data', d => { stderr += d.toString(); });

  const timeoutSecs = parseInt(process.env.PI_TIMEOUT || '600', 10);
  const timer = setTimeout(() => {
    child.kill('SIGTERM');
    emit(`AGENT_ERROR:${JSON.stringify('pi timed out')}`);
    process.exit(124);
  }, timeoutSecs * 1000);

  const code = await new Promise(resolve => child.on('close', resolve));
  clearTimeout(timer);

  if (spawnError) {
    const notFound = spawnError.code === 'ENOENT';
    const msg = notFound
      ? 'pi CLI not found. Install: npm install -g @earendil-works/pi-coding-agent'
      : spawnError.message;
    emit(`AGENT_ERROR:${JSON.stringify(msg)}`);
    process.exit(1);
  }

  if (code !== 0) {
    let msg = `pi exited with ${code}`;
    const s = stderr.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emit(`AGENT_ERROR:${JSON.stringify(msg)}`);
    process.exit(1);
  }

  const text = stdout.trim();
  if (!text) {
    emit(`AGENT_ERROR:${JSON.stringify('pi returned empty output')}`);
    process.exit(1);
  }
  emit(text);
  process.exit(0);
}

main().catch(e => {
  process.stderr.write(`[pi bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
