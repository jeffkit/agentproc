#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `claude` CLI (Anthropic Claude Code, wire 0.3).
 *
 * Default (unattended):
 *   claude -p <message> --output-format stream-json \
 *       --dangerously-skip-permissions --disallowed-tools AskUserQuestion
 *
 * Permission mode (turn.permission === true / profile permission: true):
 *   claude --print --input-format stream-json --output-format stream-json \
 *       --verbose --permission-prompt-tool stdio --permission-mode default
 *   Translates control_request/control_response ↔ {"type":"permission_request"}
 *   / {"type":"permission_response"} NDJSON events.
 */

const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');

const HUB_DIR = path.resolve(__dirname, '..');
const {
  runBridge,
  readTurn,
  emit,
  emitError,
  emitPartial,
  emitSession,
  emitText,
} = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'claude';
const INSTALL_HINT = 'Install: npm install -g @anthropic-ai/claude-code';

function buildArgs(message, sessionId, env) {
  const args = [
    CLI_NAME, '-p', message,
    '--output-format', 'stream-json',
    '--dangerously-skip-permissions',
  ];
  const disallow = (env.CLAUDE_DISALLOW_TOOLS || 'AskUserQuestion').trim();
  if (disallow) args.push('--disallowed-tools', disallow);
  const model = (env.CLAUDE_MODEL || '').trim();
  if (model) args.push('--model', model);
  if (sessionId) args.push('--resume', sessionId);
  return args;
}

function buildPermissionArgs(sessionId, env) {
  const args = [
    CLI_NAME, '--print',
    '--output-format', 'stream-json',
    '--input-format', 'stream-json',
    '--verbose',
    '--permission-prompt-tool', 'stdio',
    '--permission-mode', 'default',
  ];
  const disallow = (env.CLAUDE_DISALLOW_TOOLS || 'AskUserQuestion').trim();
  if (disallow) args.push('--disallowed-tools', disallow);
  const model = (env.CLAUDE_MODEL || '').trim();
  if (model) args.push('--model', model);
  if (sessionId) args.push('--resume', sessionId);
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
      return { sessionId, error: event.result || 'claude reported an error' };
    }
    const resultText = event.result || '';
    return { sessionId, finalText: resultText || null };
  }
  return null;
}

function controlToPermissionRequest(event) {
  if (event.type !== 'control_request') return null;
  const request = event.request || {};
  if (request.subtype !== 'can_use_tool') return null;
  const requestId = event.request_id;
  if (typeof requestId !== 'string' || !requestId.trim()) return null;
  const toolName = request.tool_name || request.display_name || 'tool';
  const toolInput = (request.input && typeof request.input === 'object' && !Array.isArray(request.input))
    ? request.input
    : {};
  const payload = {
    request_id: requestId,
    tool_name: String(toolName),
    input: toolInput,
  };
  if (typeof request.description === 'string' && request.description) {
    payload.description = request.description;
  }
  if (typeof request.tool_use_id === 'string' && request.tool_use_id) {
    payload.tool_use_id = request.tool_use_id;
  }
  return payload;
}

function permissionResponseToControl(resp, originalInput) {
  const requestId = String(resp.request_id || '');
  if (resp.behavior === 'allow') {
    const updated = (resp.updated_input && typeof resp.updated_input === 'object')
      ? resp.updated_input
      : originalInput;
    return {
      type: 'control_response',
      response: {
        subtype: 'success',
        request_id: requestId,
        response: { behavior: 'allow', updatedInput: updated },
      },
    };
  }
  return {
    type: 'control_response',
    response: {
      subtype: 'success',
      request_id: requestId,
      response: {
        behavior: 'deny',
        message: String(resp.message || 'denied by bridge'),
      },
    },
  };
}

module.exports = {
  // Exported for unit tests / reuse.
  controlToPermissionRequest,
  permissionResponseToControl,
  buildArgs,
  buildPermissionArgs,
  parseEvent,
};

async function runPermissionMode(turn, env) {
  const message = (typeof turn.message === 'string') ? turn.message : '';
  const sessionId = (typeof turn.session_id === 'string') ? turn.session_id : '';
  const attachments = Array.isArray(turn.attachments) ? turn.attachments : [];

  if (!message && attachments.length === 0) {
    emitError('turn.message is required (or include turn.attachments)');
    process.exit(1);
  }

  const args = buildPermissionArgs(sessionId, env);
  let child;
  try {
    child = spawn(args[0], args.slice(1), { stdio: ['pipe', 'pipe', 'pipe'] });
  } catch {
    emitError(`${CLI_NAME} CLI not found. ${INSTALL_HINT}`);
    process.exit(1);
  }
  child.on('error', () => {
    emitError(`${CLI_NAME} CLI not found. ${INSTALL_HINT}`);
    process.exit(1);
  });

  function writeClaude(obj) {
    try {
      child.stdin.write(JSON.stringify(obj) + '\n');
    } catch {
      // ignore
    }
  }

  writeClaude({
    type: 'user',
    message: { role: 'user', content: message },
  });

  const pendingInputs = new Map();
  const waiters = new Map(); // request_id -> { resolve }
  let childClosed = false;
  let stderrBuf = '';
  child.stderr.on('data', d => { stderrBuf += d.toString(); });
  child.on('close', () => {
    childClosed = true;
    for (const [rid, waiter] of waiters) {
      waiters.delete(rid);
      waiter.resolve({
        request_id: rid,
        behavior: 'deny',
        message: 'no permission response (process ending)',
      });
    }
  });

  // Bridge stdin ← AgentProc runner ({"type":"permission_response",...} NDJSON)
  const bridgeRl = readline.createInterface({ input: process.stdin });
  bridgeRl.on('line', (raw) => {
    const line = String(raw).trim();
    if (!line) return;
    let payload;
    try { payload = JSON.parse(line); } catch { return; }
    if (!payload || typeof payload !== 'object' || payload.type !== 'permission_response') return;
    const rid = payload.request_id;
    if (typeof rid !== 'string' || !rid) return;
    const waiter = waiters.get(rid);
    if (waiter) {
      waiters.delete(rid);
      waiter.resolve(payload);
    }
  });

  function waitForResponse(rid) {
    if (childClosed) {
      return Promise.resolve({
        request_id: rid,
        behavior: 'deny',
        message: 'no permission response (process ending)',
      });
    }
    return new Promise((resolve) => {
      waiters.set(rid, { resolve });
    });
  }

  let foundSessionId = null;
  let lastFinalText = null;
  let lastPartialText = null;
  let errorMessage = null;

  const outRl = readline.createInterface({ input: child.stdout });
  for await (const raw of outRl) {
    const line = String(raw).trim();
    if (!line) continue;
    let event;
    try { event = JSON.parse(line); } catch { continue; }

    const permReq = controlToPermissionRequest(event);
    if (permReq) {
      const rid = permReq.request_id;
      pendingInputs.set(rid, permReq.input || {});
      emit(Object.assign({ type: 'permission_request' }, permReq));
      const resp = await waitForResponse(rid);
      const original = pendingInputs.get(rid) || {};
      pendingInputs.delete(rid);
      writeClaude(permissionResponseToControl(resp, original));
      continue;
    }

    if (event.type === 'control_request' || event.type === 'control_response' || event.type === 'sdk_control_request') {
      continue;
    }

    const result = parseEvent(event);
    if (!result) continue;
    if (result.sessionId) foundSessionId = result.sessionId;
    if (result.error) errorMessage = result.error;
    if (result.partialText) {
      emitPartial(result.partialText);
      lastPartialText = result.partialText;
    }
    if (result.finalText) {
      lastFinalText = result.finalText;
    }
  }

  const code = await new Promise(resolve => child.on('close', resolve));
  try { child.stdin.end(); } catch { /* ignore */ }
  bridgeRl.close();

  if (errorMessage) {
    if (foundSessionId) emitSession(foundSessionId);
    emitError(errorMessage);
    process.exit(1);
  }
  if (code !== 0 && !foundSessionId) {
    let msg = `${CLI_NAME} exited with ${code}`;
    const s = stderrBuf.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emitError(msg);
    process.exit(1);
  }

  if (foundSessionId) emitSession(foundSessionId);
  const replyText = (lastFinalText !== null) ? lastFinalText : lastPartialText;
  if (replyText) emitText(replyText);
  process.exit(0);
}

async function main() {
  const env = process.env;
  const turn = await readTurn();
  if (turn.permission === true) {
    await runPermissionMode(turn, env);
    return;
  }
  await runBridge({
    cliName: CLI_NAME,
    cliInstallHint: INSTALL_HINT,
    buildArgs,
    parseEvent,
    turn,
  });
}

if (require.main === module) {
  main().catch(e => {
    process.stderr.write(`[claude-code bridge] unhandled error: ${e && (e.stack || e)}\n`);
    process.exit(1);
  });
}
