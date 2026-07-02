'use strict';

/**
 * Shared bridge utilities for AgentProc hub profiles.
 *
 * A bridge wraps a CLI that emits NDJSON (one JSON object per line) on stdout.
 * The bridge reads the stream line by line, extracts three things per event,
 * and emits AgentProc protocol output:
 *
 *   - partial text   → AGENT_PARTIAL:<json-string>   (streaming only)
 *   - session id     → AGENT_SESSION:<opaque-id>     (last wins)
 *   - error message  → AGENT_ERROR:<json-string>     (always honored)
 *
 * A profile supplies:
 *
 *   - `cliName`         e.g. "claude", "codex", "gemini"
 *   - `cliInstallHint`  short install instruction shown on ENOENT
 *   - `buildArgs(message, sessionId, env) -> string[]`
 *   - `parseEvent(event) -> { partialText?, sessionId?, error? } | null`
 *
 * This module handles subprocess lifecycle, line reading, JSON decoding,
 * non-streaming fallback (emit final text at end), exit-code mapping, and
 * the AGENT_* emission contract. Each bridge stays under ~30 lines.
 */

const { spawn } = require('node:child_process');
const readline = require('node:readline');

function emit(line) {
  process.stdout.write(line + '\n');
}

function emitError(text) {
  emit(`AGENT_ERROR:${JSON.stringify(text)}`);
}

function emitPartial(text) {
  emit(`AGENT_PARTIAL:${JSON.stringify(text)}`);
}

function emitSession(sessionId) {
  emit(`AGENT_SESSION:${sessionId}`);
}

function hasAnyAttachment(env) {
  if ((env.AGENT_IMAGE_URL || '').trim()) return true;
  if ((env.AGENT_FILE_URL || '').trim()) return true;
  return false;
}

async function runBridge({ cliName, cliInstallHint, buildArgs, parseEvent }) {
  const env = process.env;
  const message = env.AGENT_MESSAGE || '';
  const sessionId = env.AGENT_SESSION_ID || '';
  const streaming = (env.AGENT_STREAMING || '1') !== '0';

  // Per spec: AGENT_MESSAGE may be empty when the turn carries attachments
  // (e.g. an image-only message). Only reject when there is truly nothing
  // to do — no text AND no attachment of any kind.
  if (!message && !hasAnyAttachment(env)) {
    emitError('AGENT_MESSAGE env var is required (or set AGENT_IMAGE_URL / AGENT_FILE_URL)');
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
  let sawAnyPartial = false;
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
      if (streaming) {
        emitPartial(result.partialText);
        sawAnyPartial = true;
      }
    }
    if (result.finalText) {
      // Only used as fallback in non-streaming mode (or when no partials
      // were actually emitted). Streaming mode prefers the live partials.
      if (!streaming || !sawAnyPartial) {
        lastFinalText = result.finalText;
      }
    }
  }

  const code = await new Promise(resolve => child.on('close', resolve));

  if (errorMessage) {
    // Per spec: a CLI's terminal event often carries both session_id and an
    // error indication. Persist the session for the next turn BEFORE emitting
    // the error — the error terminates this turn but does not invalidate the
    // session.
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
  if (lastFinalText && !streaming) emit(lastFinalText);
  process.exit(0);
}

module.exports = {
  runBridge,
  emit,
  emitError,
  emitPartial,
  emitSession,
};
