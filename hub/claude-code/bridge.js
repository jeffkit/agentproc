#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `claude` CLI (Anthropic Claude Code).
 *
 * Invokes:
 *     claude -p <message> --output-format stream-json \
 *         --dangerously-skip-permissions \
 *         --disallowed-tools AskUserQuestion \
 *         [--resume <session_id>] \
 *         [--model <model>]
 *
 * Re-emits the stream as AgentProc protocol output:
 *   - assistant text blocks → AGENT_PARTIAL:<json-string>
 *   - result event's session_id → AGENT_SESSION:<id>
 *   - errors → AGENT_ERROR:<json-string>
 *
 * Env vars:
 *   AGENT_MESSAGE          User message
 *   AGENT_SESSION_ID       Previous session ID (empty = new session)
 *   AGENT_STREAMING        "1" streaming mode, "0" one-shot
 *   CLAUDE_MODEL           Optional model override
 *   CLAUDE_DISALLOW_TOOLS  Optional comma-separated disallowed tools
 */

const { spawn } = require('node:child_process');
const readline = require('node:readline');

function buildArgs(message, sessionId) {
  const args = [
    'claude', '-p', message,
    '--output-format', 'stream-json',
    '--dangerously-skip-permissions',
  ];
  const disallow = (process.env.CLAUDE_DISALLOW_TOOLS || 'AskUserQuestion').trim();
  if (disallow) {
    args.push('--disallowed-tools', disallow);
  }
  const model = (process.env.CLAUDE_MODEL || '').trim();
  if (model) {
    args.push('--model', model);
  }
  if (sessionId) {
    args.push('--resume', sessionId);
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
    emit(`AGENT_ERROR:${JSON.stringify('claude CLI not found. Install: npm install -g @anthropic-ai/claude-code')}`);
    process.exit(1);
  }

  const rl = readline.createInterface({ input: child.stdout });
  let stderrBuf = '';
  child.stderr.on('data', d => { stderrBuf += d.toString(); });

  let foundSessionId = null;
  let lastPartial = null;
  let errorMessage = null;

  for await (const raw of rl) {
    const line = raw.trim();
    if (!line) continue;
    let event;
    try { event = JSON.parse(line); } catch { continue; }

    const etype = event.type;
    if (etype === 'assistant') {
      const text = (event.message?.content || [])
        .filter(b => b.type === 'text')
        .map(b => b.text)
        .join('');
      if (text && streaming) {
        emit(`AGENT_PARTIAL:${JSON.stringify(text)}`);
        lastPartial = text;
      }
    } else if (etype === 'result') {
      foundSessionId = event.session_id || foundSessionId;
      if (event.is_error) {
        errorMessage = event.result || 'claude reported an error';
      } else {
        const resultText = event.result || '';
        if (resultText && resultText !== lastPartial) {
          if (streaming) {
            emit(`AGENT_PARTIAL:${JSON.stringify(resultText)}`);
          } else {
            if (foundSessionId) emit(`AGENT_SESSION:${foundSessionId}`);
            emit(resultText);
            process.exit(0);
          }
        }
      }
    }
  }

  const code = await new Promise(resolve => child.on('close', resolve));

  if (errorMessage) {
    emit(`AGENT_ERROR:${JSON.stringify(errorMessage)}`);
    process.exit(1);
  }
  if (code !== 0 && !foundSessionId) {
    let msg = `claude exited with ${code}`;
    const s = stderrBuf.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emit(`AGENT_ERROR:${JSON.stringify(msg)}`);
    process.exit(1);
  }

  if (foundSessionId) {
    emit(`AGENT_SESSION:${foundSessionId}`);
  }
  process.exit(0);
}

main().catch(e => {
  process.stderr.write(`[claude-code bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
