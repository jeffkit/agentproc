'use strict';

/**
 * Shared bridge utilities for AgentProc hub profiles (wire 0.4).
 *
 * A bridge wraps a CLI that emits NDJSON (one JSON object per line) on stdout.
 * The bridge reads the {"type":"turn",...} object from its own stdin, spawns
 * the CLI, and translates the CLI's NDJSON stream into AgentProc wire-0.4
 * output (one JSON event per line on stdout):
 *
 *   - {"type":"partial","text":...,"session_id"?}  live streaming chunk
 *      (always emitted; the runner forwards it only when the profile's
 *      streaming is true). session_id is stamped when already known.
 *   - {"type":"result","text":...,"session_id"?}   single terminal reply
 *      (emitted once at end; text may be "" if the body was already streamed)
 *   - {"type":"error","message":...,"session_id"?} error (exit 1); may
 *      carry session_id so the session survives an error-terminated turn
 *
 * A profile supplies:
 *
 *   - cliName         e.g. "claude", "codex", "gemini"
 *   - cliInstallHint  short install instruction shown on ENOENT
 *   - buildArgs(message, sessionId, env) -> string[]
 *   - parseEvent(event) -> { partialText?, finalText?, sessionId?, error?, usage? } | null
 *
 * `usage` (result/error only) is an opaque plain-object pass-through: the
 * first non-null usage captured rides on the terminal result / error event
 * so hosts can do token/cost accounting without parsing the CLI stream.
 *
 * This module handles turn parsing, subprocess lifecycle, line reading, JSON
 * decoding, exit-code mapping, and the NDJSON emission contract.
 */

const { spawn } = require('node:child_process');
const readline = require('node:readline');

// Wire-protocol version this engine implements — kept in sync with the three
// SDKs' PROTOCOL_VERSION constants (spec `Versioning`).
const PROTOCOL_VERSION = '0.4';

const KILL_GRACE_MS = 5000;

/**
 * Spawn the wrapped CLI in its own process group so a bridge-side timeout can
 * clear the CLI's whole subtree with one group signal (a grandchild that
 * survives holds the agent's credentials — issue #8). Leaving the bridge's
 * group means the runner's own group kill no longer reaches the CLI, so the
 * bridge forwards an incoming SIGTERM/SIGINT to the CLI group before exiting.
 */
function spawnCliGroup(command, args, opts = {}) {
  const detached = process.platform !== 'win32';
  const child = spawn(command, args, { ...opts, detached });
  const killTree = (signal) => {
    if (detached && child.pid) {
      try { process.kill(-child.pid, signal); return; } catch { /* group already gone */ }
    }
    try { child.kill(signal); } catch { /* already dead */ }
  };
  if (detached) {
    const forward = (signum) => () => {
      // SIGKILL, not SIGTERM: the runner's grace window is about to expire and
      // its SIGKILL step cannot reach this group once we are gone.
      killTree('SIGKILL');
      process.exit(128 + signum);
    };
    process.once('SIGTERM', forward(15));
    process.once('SIGINT', forward(2));
  }
  return { child, killTree };
}

function emitObj(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function emit(obj) {
  // Emit one NDJSON event dict. Bridges may pass a pre-built event dict; for
  // the common cases use emitPartial / emitResult / emitError.
  emitObj(obj);
}

function emitPartial(text, sessionId) {
  const obj = { type: 'partial', text };
  if (sessionId) obj.session_id = sessionId;
  emitObj(obj);
}

function emitResult(text, sessionId, usage) {
  const obj = { type: 'result', text };
  if (sessionId) obj.session_id = sessionId;
  if (validUsage(usage)) obj.usage = usage;
  emitObj(obj);
}

function emitError(text, sessionId, usage) {
  const obj = { type: 'error', message: text };
  if (sessionId) obj.session_id = sessionId;
  if (validUsage(usage)) obj.usage = usage;
  emitObj(obj);
}

function hasAnyAttachment(turn) {
  return Array.isArray(turn.attachments) && turn.attachments.length > 0;
}

/** Plain-object guard for the opaque usage pass-through. */
function validUsage(usage) {
  return usage !== null && typeof usage === 'object' && !Array.isArray(usage);
}

function readTurn() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    const onReadable = () => {
      let chunk;
      while ((chunk = process.stdin.read()) !== null) {
        data += chunk;
        const nl = data.indexOf('\n');
        if (nl >= 0) {
          const line = data.slice(0, nl);
          process.stdin.removeListener('readable', onReadable);
          try {
            const v = JSON.parse(line);
            resolve(v && typeof v === 'object' ? v : {});
          } catch {
            resolve({});
          }
          return;
        }
      }
    };
    process.stdin.once('readable', onReadable);
    process.stdin.once('end', () => resolve({}));
    process.stdin.once('error', () => resolve({}));
  });
}

async function runBridge({ cliName, cliInstallHint, buildArgs, parseEvent, turn = null }) {
  if (turn === null) turn = await readTurn();
  const env = process.env;
  const turnVersion = turn.protocol_version;
  if (typeof turnVersion === 'string' && turnVersion && turnVersion !== PROTOCOL_VERSION) {
    // Diagnostic only (spec `Versioning`): the turn is processed unchanged.
    process.stderr.write(
      `[agentproc hub] protocol_version "${turnVersion}" does not match this hub bridge's ` +
        `"${PROTOCOL_VERSION}"; continuing best-effort (fail-soft)\n`,
    );
  }
  const message = (typeof turn.message === 'string') ? turn.message : '';
  const sessionId = (typeof turn.session_id === 'string') ? turn.session_id : '';

  if (!message && !hasAnyAttachment(turn)) {
    emitError('turn.message is required (or include turn.attachments)');
    process.exit(1);
  }

  const args = buildArgs(message, sessionId, env);
  let child;
  try {
    child = spawn(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    emitError(`${cliName} CLI not found. ${cliInstallHint}`);
    process.exit(1);
  }
  child.on('error', () => {
    emitError(`${cliName} CLI not found. ${cliInstallHint}`);
    process.exit(1);
  });

  const rl = readline.createInterface({ input: child.stdout });
  let stderrBuf = '';
  child.stderr.on('data', d => { stderrBuf += d.toString(); });

  let foundSessionId = null;
  let lastFinalText = null;
  let lastPartialText = null;
  let errorMessage = null;
  let usage = null;

  for await (const raw of rl) {
    const line = String(raw).trim();
    if (!line) continue;
    let event;
    try { event = JSON.parse(line); } catch { continue; }

    const result = parseEvent(event);
    if (!result) continue;

    // Capture sessionId before emitting partials so same-event session
    // stamps the partial (runner first-non-empty wins if it arrives later).
    if (result.sessionId) foundSessionId = result.sessionId;
    if (result.error) errorMessage = result.error;
    if (!usage && validUsage(result.usage)) usage = result.usage;
    if (result.partialText) {
      // Always emit partials; the runner forwards them only when the profile's
      // streaming is true (and drops them otherwise).
      emitPartial(result.partialText, foundSessionId);
      lastPartialText = result.partialText;
    }
    if (result.finalText !== undefined && result.finalText !== null) {
      lastFinalText = result.finalText;
    }
  }

  const code = await new Promise(resolve => child.on('close', resolve));

  if (errorMessage) {
    emitError(errorMessage, foundSessionId, usage);
    process.exit(1);
  }

  const replyText = (lastFinalText !== null) ? lastFinalText : lastPartialText;

  // Surface a non-zero exit as an error only when the CLI produced no reply
  // content. If the CLI crashed after emitting a result event, treat the run
  // as successful — many CLIs exit non-zero for internal reasons while still
  // returning valid output. Include foundSessionId (if any) so the session
  // survives the failed turn.
  if (code !== 0 && !replyText) {
    let msg = `${cliName} exited with ${code}`;
    const s = stderrBuf.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emitError(msg, foundSessionId, usage);
    process.exit(1);
  }

  emitResult(replyText || '', foundSessionId, usage);
  process.exit(0);
}

async function runPlainCli({ cliName, cliInstallHint, buildArgs, timeoutEnv = 'CLI_TIMEOUT', defaultTimeout = 600 }) {
  // Drive a one-shot CLI that returns the full reply as plain stdout text
  // (no streaming, no session id). Reads the turn from stdin, runs the CLI
  // with a timeout, and emits the trimmed stdout as a single {"type":"result"}
  // event (or {"type":"error"} on failure). buildArgs(message) builds the
  // argv; per-CLI config is read from process.env inside buildArgs.
  const turn = await readTurn();
  const message = (typeof turn.message === 'string') ? turn.message : '';
  if (!message && !hasAnyAttachment(turn)) {
    emitError(`${cliName}: turn.message is required (or include turn.attachments)`);
    process.exit(1);
  }

  const args = buildArgs(message);
  const { child, killTree } = spawnCliGroup(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  let spawnError = null;
  child.on('error', err => { spawnError = err; });
  child.stdout.on('data', d => { stdout += d.toString(); });
  child.stderr.on('data', d => { stderr += d.toString(); });

  const timeoutSecs = parseInt(process.env[timeoutEnv] || String(defaultTimeout), 10);
  let timedOut = false;
  let killer = null;
  const timer = setTimeout(() => {
    timedOut = true;
    // Two-step on the CLI's own process group: the direct child alone would
    // leave the CLI's grandchildren running with the credentials.
    killTree('SIGTERM');
    killer = setTimeout(() => killTree('SIGKILL'), KILL_GRACE_MS);
  }, timeoutSecs * 1000);

  const code = await new Promise(resolve => child.on('close', resolve));
  clearTimeout(timer);
  if (killer) clearTimeout(killer);

  if (timedOut) {
    emitError(`${cliName} timed out`);
    process.exit(124);
  }
  if (spawnError) {
    const notFound = spawnError.code === 'ENOENT';
    const msg = notFound ? `${cliName} CLI not found. ${cliInstallHint}` : spawnError.message;
    emitError(msg);
    process.exit(1);
  }
  if (code !== 0) {
    let msg = `${cliName} exited with ${code}`;
    const s = stderr.trim();
    if (s) msg += `: ${s.slice(0, 500)}`;
    emitError(msg);
    process.exit(1);
  }

  const text = stdout.trim();
  if (!text) {
    emitError(`${cliName} returned empty output`);
    process.exit(1);
  }
  emitResult(text);
  process.exit(0);
}

module.exports = {
  runBridge,
  runPlainCli,
  spawnCliGroup,
  readTurn,
  emit,
  emitPartial,
  emitResult,
  emitError,
};
