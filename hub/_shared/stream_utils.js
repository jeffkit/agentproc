'use strict';

/**
 * Shared bridge utilities for AgentProc hub profiles (wire 0.3).
 *
 * A bridge wraps a CLI that emits NDJSON (one JSON object per line) on stdout.
 * The bridge reads the {"type":"turn",...} object from its own stdin, spawns
 * the CLI, and translates the CLI's NDJSON stream into AgentProc wire-0.3
 * output (one JSON event per line on stdout):
 *
 *   - {"type":"partial","text":...}   live streaming chunk (always emitted;
 *                                      the runner forwards it only when the
 *                                      profile's streaming is true)
 *   - {"type":"session","id":...}     declare session id (last wins)
 *   - {"type":"error","message":...}  error message (always honored, exit 1)
 *   - {"type":"text","text":...}      final reply body (emitted once at end)
 *
 * A profile supplies:
 *
 *   - `cliName`         e.g. "claude", "codex", "gemini"
 *   - `cliInstallHint`  short install instruction shown on ENOENT
 *   - `buildArgs(message, sessionId, env) -> string[]`
 *   - `parseEvent(event) -> { partialText?, finalText?, sessionId?, error? } | null`
 *
 * This module handles turn parsing, subprocess lifecycle, line reading, JSON
 * decoding, exit-code mapping, and the NDJSON emission contract.
 */

const { spawn } = require('node:child_process');
const readline = require('node:readline');

function emitObj(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function emit(obj) {
  // Emit one NDJSON event dict. Bridges may pass a pre-built event dict; for
  // the common cases use emitPartial / emitText / emitSession / emitError.
  emitObj(obj);
}

function emitPartial(text) {
  emitObj({ type: 'partial', text });
}

function emitText(text) {
  emitObj({ type: 'text', text });
}

function emitError(text) {
  emitObj({ type: 'error', message: text });
}

function emitSession(sessionId) {
  emitObj({ type: 'session', id: sessionId });
}

function hasAnyAttachment(turn) {
  return Array.isArray(turn.attachments) && turn.attachments.length > 0;
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

  for await (const raw of rl) {
    const line = String(raw).trim();
    if (!line) continue;
    let event;
    try { event = JSON.parse(line); } catch { continue; }

    const result = parseEvent(event);
    if (!result) continue;

    if (result.sessionId) foundSessionId = result.sessionId;
    if (result.error) errorMessage = result.error;
    if (result.partialText) {
      // Always emit partials; the runner forwards them only when the profile's
      // streaming is true (and drops them otherwise).
      emitPartial(result.partialText);
      lastPartialText = result.partialText;
    }
    if (result.finalText) {
      lastFinalText = result.finalText;
    }
  }

  const code = await new Promise(resolve => child.on('close', resolve));

  if (errorMessage) {
    if (foundSessionId) emitSession(foundSessionId);
    emitError(errorMessage);
    process.exit(1);
  }
  if (code !== 0 && !foundSessionId) {
    let msg = `${cliName} exited with ${code}`;
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

async function runPlainCli({ cliName, cliInstallHint, buildArgs, timeoutEnv = 'CLI_TIMEOUT', defaultTimeout = 600 }) {
  // Drive a one-shot CLI that returns the full reply as plain stdout text
  // (no streaming, no session id). Reads the turn from stdin, runs the CLI
  // with a timeout, and emits the trimmed stdout as a single {"type":"text"}
  // event (or {"type":"error"} on failure). buildArgs(message) builds the
  // argv; per-CLI config is read from process.env inside buildArgs.
  const turn = await readTurn();
  const message = (typeof turn.message === 'string') ? turn.message : '';
  if (!message && !hasAnyAttachment(turn)) {
    emitError(`${cliName}: turn.message is required (or include turn.attachments)`);
    process.exit(1);
  }

  const args = buildArgs(message);
  const child = spawn(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  let spawnError = null;
  child.on('error', err => { spawnError = err; });
  child.stdout.on('data', d => { stdout += d.toString(); });
  child.stderr.on('data', d => { stderr += d.toString(); });

  const timeoutSecs = parseInt(process.env[timeoutEnv] || String(defaultTimeout), 10);
  const timer = setTimeout(() => {
    child.kill('SIGTERM');
    emitError(`${cliName} timed out`);
    process.exit(124);
  }, timeoutSecs * 1000);

  const code = await new Promise(resolve => child.on('close', resolve));
  clearTimeout(timer);

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
  emitText(text);
  process.exit(0);
}

module.exports = {
  runBridge,
  runPlainCli,
  readTurn,
  emit,
  emitPartial,
  emitText,
  emitError,
  emitSession,
};
