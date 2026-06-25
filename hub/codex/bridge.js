#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `codex` CLI (OpenAI Codex).
 *
 * Invokes:
 *   codex exec --json <message>
 *   codex exec resume <thread_id> <message>   // when AGENT_SESSION_ID is set
 *
 * Parses the NDJSON stream:
 *   thread.started    → captures thread_id (forwarded as AGENT_SESSION:)
 *   item.completed    → agent_message text → AGENT_PARTIAL:
 *   turn.completed    → end of turn
 *   turn.failed       → AGENT_ERROR:
 *
 * Env vars:
 *   AGENT_MESSAGE          User message
 *   AGENT_SESSION_ID       Previous thread_id (empty = new session)
 *   AGENT_STREAMING        "1" streaming mode, "0" one-shot
 *   CODEX_MODEL            Optional model override
 */

const { spawn } = require('node:child_process');
const readline = require('node:readline');

function buildArgs(message, sessionId) {
  const model = (process.env.CODEX_MODEL || '').trim();
  if (sessionId) {
    return ['codex', 'exec', 'resume', sessionId, message];
  }
  const args = ['codex', 'exec', '--json', message];
  if (model) {
    args.push('-c', `model="${model}"`);
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
  const sessionId = process.env.AGENT_SESSION_ID || '';
  const streaming = (process.env.AGENT_STREAMING || '1') !== '0';

  const args = buildArgs(message, sessionId);
  let child;
  try {
    child = spawn(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    emit(`AGENT_ERROR:${JSON.stringify('codex CLI not found. Install: npm install -g @openai/codex')}`);
    process.exit(1);
  }

  const rl = readline.createInterface({ input: child.stdout });
  let stderrBuf = '';
  child.stderr.on('data', d => { stderrBuf += d.toString(); });

  let threadId = null;
  let finalText = null;
  let errorMessage = null;

  for await (const raw of rl) {
    const line = raw.trim();
    if (!line) continue;
    let event;
    try { event = JSON.parse(line); } catch { continue; }

    const etype = event.type;
    if (etype === 'thread.started') {
      threadId = event.thread_id || threadId;
    } else if (etype === 'item.completed') {
      const item = event.item || {};
      if (item.type === 'agent_message') {
        const text = item.text || '';
        if (text) {
          if (streaming) {
            emit(`AGENT_PARTIAL:${JSON.stringify(text)}`);
          }
          finalText = text;
        }
      }
    } else if (etype === 'turn.failed') {
      errorMessage = event.error || 'codex turn failed';
    }
  }

  const code = await new Promise(resolve => child.on('close', resolve));

  if (errorMessage) {
    emit(`AGENT_ERROR:${JSON.stringify(String(errorMessage))}`);
    process.exit(1);
  }
  if (code !== 0 && !threadId) {
    let msg = `codex exited with ${code}`;
    const s = stderrBuf.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emit(`AGENT_ERROR:${JSON.stringify(msg)}`);
    process.exit(1);
  }

  if (threadId) {
    emit(`AGENT_SESSION:${threadId}`);
  }
  if (finalText && !streaming) {
    emit(finalText);
  }
  process.exit(0);
}

main().catch(e => {
  process.stderr.write(`[codex bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
