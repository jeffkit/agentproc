#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `codex` CLI (OpenAI Codex).
 *
 * Default:
 *   codex exec --json <message>
 *   codex exec resume --json <thread_id> <message>
 *
 * Permission mode (AGENT_PERMISSION=1 / profile permission: true):
 *   Same argv + --dangerously-bypass-hook-trust + approval_policy=on-request,
 *   with a one-shot CODEX_HOME that installs a PermissionRequest hook.
 *   The hook relays approvals over a Unix socket ↔ AGENT_PERMISSION_*.
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { randomUUID } = require('node:crypto');

const HUB_DIR = path.resolve(__dirname, '..');
const {
  runBridge,
  emit,
  emitError,
  emitPartial,
  emitSession,
} = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const {
  permissionEnabled,
  buildArgs,
  buildPermissionArgs,
  parseEvent,
  buildHooksJson,
} = require(path.join(__dirname, 'permission_map.js'));

const CLI_NAME = 'codex';
const INSTALL_HINT = 'Install: npm install -g @openai/codex';
const HOOK_SCRIPT = path.join(__dirname, 'permission_hook.py');

module.exports = {
  permissionEnabled,
  buildArgs,
  buildPermissionArgs,
  parseEvent,
  buildHooksJson,
};

function realCodexHome() {
  const fromEnv = (process.env.CODEX_HOME || '').trim();
  if (fromEnv) return fromEnv;
  return path.join(os.homedir(), '.codex');
}

function preparePermissionHome() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'agentproc-codex-'));
  const sockPath = path.join(tmp, 'perm.sock');
  const hooks = buildHooksJson(HOOK_SCRIPT);
  fs.writeFileSync(path.join(tmp, 'hooks.json'), JSON.stringify(hooks, null, 2));

  // Preserve auth (and optional config) from the real Codex home so the
  // one-shot CODEX_HOME still authenticates.
  const realHome = realCodexHome();
  for (const name of ['auth.json', 'config.toml']) {
    const src = path.join(realHome, name);
    const dst = path.join(tmp, name);
    try {
      if (fs.existsSync(src)) fs.copyFileSync(src, dst);
    } catch {
      // ignore — auth may live elsewhere / login session
    }
  }
  return { tmp, sockPath };
}

function startPermissionServer(sockPath, waiters) {
  const server = net.createServer((conn) => {
    let buf = '';
    conn.on('data', (chunk) => {
      buf += chunk.toString();
      const nl = buf.indexOf('\n');
      if (nl < 0) return;
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      let req;
      try { req = JSON.parse(line); } catch { conn.end(); return; }
      if (!req || typeof req !== 'object') { conn.end(); return; }
      const rid = req.request_id || randomUUID();
      req.request_id = rid;
      emit(`AGENT_PERMISSION_REQUEST:${JSON.stringify(req)}`);
      const done = (resp) => {
        try {
          conn.write(JSON.stringify(resp) + '\n');
        } catch { /* ignore */ }
        try { conn.end(); } catch { /* ignore */ }
      };
      waiters.set(rid, { resolve: done });
    });
    conn.on('error', () => { /* ignore */ });
  });
  server.listen(sockPath);
  return server;
}

async function runPermissionMode(env) {
  const message = env.AGENT_MESSAGE || '';
  const sessionId = env.AGENT_SESSION_ID || '';
  const streaming = (env.AGENT_STREAMING || '1') !== '0';

  if (!message && !(env.AGENT_IMAGE_URL || '').trim() && !(env.AGENT_FILE_URL || '').trim()) {
    emitError('AGENT_MESSAGE env var is required (or set AGENT_IMAGE_URL / AGENT_FILE_URL)');
    process.exit(1);
  }

  const { tmp, sockPath } = preparePermissionHome();
  const waiters = new Map();
  let server;
  try {
    server = startPermissionServer(sockPath, waiters);
  } catch (e) {
    emitError(`failed to start permission socket: ${e && e.message ? e.message : e}`);
    process.exit(1);
  }

  const bridgeRl = readline.createInterface({ input: process.stdin });
  bridgeRl.on('line', (raw) => {
    const line = String(raw);
    if (!line.startsWith('AGENT_PERMISSION_RESPONSE:')) return;
    let payload;
    try {
      payload = JSON.parse(line.slice('AGENT_PERMISSION_RESPONSE:'.length));
    } catch { return; }
    if (!payload || typeof payload !== 'object') return;
    const rid = payload.request_id;
    if (typeof rid !== 'string' || !rid) return;
    const waiter = waiters.get(rid);
    if (waiter) {
      waiters.delete(rid);
      waiter.resolve(payload);
    }
  });

  const childEnv = {
    ...process.env,
    CODEX_HOME: tmp,
    AGENTPROC_CODEX_PERM_SOCK: sockPath,
  };
  const args = buildPermissionArgs(message, sessionId, env);
  let child;
  try {
    child = spawn(args[0], args.slice(1), {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: childEnv,
    });
  } catch {
    cleanup();
    emitError(`${CLI_NAME} CLI not found. ${INSTALL_HINT}`);
    process.exit(1);
  }
  child.on('error', () => {
    cleanup();
    emitError(`${CLI_NAME} CLI not found. ${INSTALL_HINT}`);
    process.exit(1);
  });

  let stderrBuf = '';
  child.stderr.on('data', d => { stderrBuf += d.toString(); });

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
  cleanup();

  function cleanup() {
    try { bridgeRl.close(); } catch { /* ignore */ }
    for (const [rid, waiter] of waiters) {
      waiters.delete(rid);
      waiter.resolve({
        request_id: rid,
        behavior: 'deny',
        message: 'no permission response (process ending)',
      });
    }
    try { server && server.close(); } catch { /* ignore */ }
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* ignore */ }
  }

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
    process.stderr.write(`[codex bridge] unhandled error: ${e && (e.stack || e)}\n`);
    process.exit(1);
  });
}
