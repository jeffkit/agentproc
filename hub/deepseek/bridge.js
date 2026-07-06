#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the DeepSeek TUI CLI.
 *
 * Uses `deepseek exec -p <message> [--model <model>]` for non-interactive output.
 * deepseek exec returns plain text. No streaming, no session continuity across
 * separate invocations.
 *
 * Env vars:
 *   AGENT_MESSAGE       User message
 *   AGENT_STREAMING     Ignored — deepseek exec returns full text only
 *   DEEPSEEK_MODEL      Optional model override (default: deepseek-v4-pro)
 *   DEEPSEEK_API_KEY    Optional API key (alternative to `deepseek login`)
 *   DEEPSEEK_TIMEOUT    Process timeout in seconds (default: 300)
 */

const { spawn } = require('node:child_process');

function buildArgs(message) {
  const args = ['deepseek', 'exec', '-p', message];
  const model = (process.env.DEEPSEEK_MODEL || '').trim();
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
  const childEnv = Object.assign({}, process.env);

  const child = spawn(args[0], args.slice(1), {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: childEnv,
  });

  let stdout = '';
  let stderr = '';
  let spawnError = null;

  child.on('error', err => { spawnError = err; });
  child.stdout.on('data', d => { stdout += d.toString(); });
  child.stderr.on('data', d => { stderr += d.toString(); });

  const timeoutSecs = parseInt(process.env.DEEPSEEK_TIMEOUT || '300', 10);
  const timer = setTimeout(() => {
    child.kill('SIGTERM');
    emit(`AGENT_ERROR:${JSON.stringify('deepseek timed out')}`);
    process.exit(124);
  }, timeoutSecs * 1000);

  const code = await new Promise(resolve => child.on('close', resolve));
  clearTimeout(timer);

  if (spawnError) {
    const notFound = spawnError.code === 'ENOENT';
    const msg = notFound
      ? 'deepseek CLI not found. Install from https://deepseek.com/downloads or: brew install deepseek'
      : spawnError.message;
    emit(`AGENT_ERROR:${JSON.stringify(msg)}`);
    process.exit(1);
  }

  if (code !== 0) {
    let msg = `deepseek exited with ${code}`;
    const s = stderr.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emit(`AGENT_ERROR:${JSON.stringify(msg)}`);
    process.exit(1);
  }

  const text = stdout.trim();
  if (!text) {
    emit(`AGENT_ERROR:${JSON.stringify('deepseek returned empty output')}`);
    process.exit(1);
  }
  emit(text);
  process.exit(0);
}

main().catch(e => {
  process.stderr.write(`[deepseek bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
