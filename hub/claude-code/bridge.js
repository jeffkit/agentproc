#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `claude` CLI (Anthropic Claude Code).
 *
 * Default (unattended):
 *   claude -p <message> --output-format stream-json \
 *       --dangerously-skip-permissions --disallowed-tools AskUserQuestion
 *
 * Permission mode (AGENT_PERMISSION=1 / profile permission: true):
 *   claude --print --input-format stream-json --output-format stream-json \
 *       --verbose --permission-prompt-tool stdio --permission-mode default
 *   Translates control_request/control_response ↔ AGENT_PERMISSION_*.
 */

const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');

const HUB_DIR = path.resolve(__dirname, '..');
const {
  runBridge,
  emit,
  emitError,
  emitPartial,
  emitSession,
} = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'claude';
const INSTALL_HINT = 'Install: npm install -g @anthropic-ai/claude-code';

function permissionEnabled(env) {
  return (env.AGENT_PERMISSION || '').trim() === '1';
}

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
  permissionEnabled,
};

async function runPermissionMode(env) {
  const message = env.AGENT_MESSAGE || '';
  const sessionId = env.AGENT_SESSION_ID || '';
  const streaming = (env.AGENT_STREAMING || '1') !== '0';

  if (!message && !(env.AGENT_IMAGE_URL || '').trim() && !(env.AGENT_FILE_URL || '').trim()) {
    emitError('AGENT_MESSAGE env var is required (or set AGENT_IMAGE_URL / AGENT_FILE_URL)');
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

  // Bridge stdin ← AgentProc runner (AGENT_PERMISSION_RESPONSE:)
  const bridgeRl = readline.createInterface({ input: process.stdin });
  bridgeRl.on('line', (raw) => {
    const line = String(raw);
    if (!line.startsWith('AGENT_PERMISSION_RESPONSE:')) return;
    let payload;
    try {
      payload = JSON.parse(line.slice('AGENT_PERMISSION_RESPONSE:'.length));
    } catch {
      return;
    }
    if (!payload || typeof payload !== 'object') return;
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
  let sawAnyPartial = false;
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
      emit(`AGENT_PERMISSION_REQUEST:${JSON.stringify(permReq)}`);
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
      if (streaming) {
        emitPartial(result.partialText);
        sawAnyPartial = true;
      }
    }
    if (result.finalText) {
      if (!streaming || !sawAnyPartial) lastFinalText = result.finalText;
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
  if (lastFinalText && !streaming) emit(lastFinalText);
  process.exit(0);
}

async function main() {
  const env = process.env;
  if (permissionEnabled(env)) {
    await runPermissionMode(env);
    return;
  }
  await runBridge({
    cliName: CLI_NAME,
    cliInstallHint: INSTALL_HINT,
    buildArgs,
    parseEvent,
  });
}

if (require.main === module) {
  main().catch(e => {
    process.stderr.write(`[claude-code bridge] unhandled error: ${e && (e.stack || e)}\n`);
    process.exit(1);
  });
}