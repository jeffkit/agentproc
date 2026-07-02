'use strict';
/**
 * agentproc — AgentProc Protocol SDK (Node.js)
 *
 * Implements the AgentProc P0 protocol (spec/protocol.md, wire protocol 0.1).
 *
 * Protocol contract:
 *   Input  — env vars: AGENT_MESSAGE, AGENT_SESSION_ID, AGENT_SESSION_NAME,
 *                      AGENT_FROM_USER, AGENT_STREAMING, AGENT_PROTOCOL_VERSION,
 *                      AGENT_IMAGE_URL, AGENT_FILE_URL
 *   Output — stdout (sentinel-prefixed lines):
 *              AGENT_SESSION:<opaque-id>     — declare session id (last wins)
 *              AGENT_PARTIAL:<json-string>   — streaming chunk
 *              AGENT_ERROR:<json-string>     — error message to forward to user
 *              everything else               = final reply body
 *   Exit   — 0 success, 1 error, 124 timeout, 130 SIGINT, 143 SIGTERM
 *
 * @example
 * const { createProfile } = require('agentproc');
 *
 * createProfile(async ({ message, sessionId }) => {
 *   const reply = await myAI(message);
 *   return { response: reply, sessionId: newSessionId };
 * });
 */

const path = require('path');
const os = require('os');
const fs = require('fs');

// Single source of truth: the wire-protocol version lives in runner.js
// (the canonical bridge-side engine). The SDK entry point re-exports it so
// `agentproc.PROTOCOL_VERSION` stays in lockstep without copy-pasted literals.
const { PROTOCOL_VERSION } = require('./runner.js');

// ---------------------------------------------------------------------------
// History helpers (optional — for handlers calling LLM APIs directly)
// ---------------------------------------------------------------------------

function defaultSessionDir() {
  return path.join(os.homedir(), '.agentproc', 'sessions');
}

/**
 * Resolve the JSONL history file path for a session.
 * @param {string} sessionId
 * @param {string} [sessionDir]
 * @returns {string}
 * @throws {Error} when sessionId is empty
 */
function sessionFilePath(sessionId, sessionDir) {
  if (!sessionId) {
    throw new Error('sessionId must be non-empty');
  }
  return path.join(sessionDir || defaultSessionDir(), `${sessionId}.jsonl`);
}

/**
 * Load conversation history for a session from its JSONL file.
 * Returns [] if sessionId is empty or the file does not exist.
 *
 * @param {string} sessionId
 * @param {string} [sessionDir]
 * @returns {HistoryEntry[]}
 */
function loadHistory(sessionId, sessionDir) {
  if (!sessionId) return [];
  let file;
  try {
    file = sessionFilePath(sessionId, sessionDir);
  } catch {
    return [];
  }
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map(line => { try { return JSON.parse(line); } catch { return null; } })
    .filter(Boolean)
    .map(d => ({
      role: String(d.role || ''),
      content: String(d.content || ''),
      timestamp: String(d.timestamp || ''),
    }));
}

/**
 * Append entries to a session's JSONL history file. No-op if sessionId is empty.
 *
 * @param {string} sessionId
 * @param {Array<{ role: string, content: string, ts?: string }>} entries
 * @param {string} [sessionDir]
 */
function appendHistory(sessionId, entries, sessionDir) {
  if (!sessionId || !entries || !entries.length) return;
  const file = sessionFilePath(sessionId, sessionDir);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const lines = entries.map(e =>
    JSON.stringify({
      role: e.role,
      content: e.content,
      timestamp: e.ts || new Date().toISOString(),
    })
  );
  fs.appendFileSync(file, lines.join('\n') + '\n', 'utf8');
}

// ---------------------------------------------------------------------------
// Env parsing helpers
// ---------------------------------------------------------------------------

function contextFromEnv() {
  return {
    message: process.env.AGENT_MESSAGE || '',
    sessionId: process.env.AGENT_SESSION_ID || '',
    sessionName: process.env.AGENT_SESSION_NAME || 'default',
    fromUser: process.env.AGENT_FROM_USER || '',
    streaming: (process.env.AGENT_STREAMING || '1') !== '0',
    protocolVersion: process.env.AGENT_PROTOCOL_VERSION || PROTOCOL_VERSION,
    imageUrl: process.env.AGENT_IMAGE_URL || '',
    fileUrl: process.env.AGENT_FILE_URL || '',

    /** Send a streaming chunk to the user immediately. */
    sendPartial(text) {
      if (!text) return;
      process.stdout.write(`AGENT_PARTIAL:${JSON.stringify(text)}\n`);
    },

    /** Send an error message to the user. Honored regardless of streaming mode. */
    sendError(text) {
      if (!text) return;
      process.stdout.write(`AGENT_ERROR:${JSON.stringify(text)}\n`);
    },
  };
}

// ---------------------------------------------------------------------------
// createProfile — main entrypoint
// ---------------------------------------------------------------------------

/**
 * Run a handler as an AgentProc-compliant process.
 *
 * @param {(ctx: AgentContext) => Promise<AgentResult | string | void>} handler
 */
function createProfile(handler) {
  const ctx = contextFromEnv();

  Promise.resolve()
    .then(() => handler(ctx))
    .then(result => {
      if (result == null) {
        // Handler signalled everything via sendPartial / sendError itself.
        process.exit(0);
      }
      const response = typeof result === 'string' ? result : (result.response || '');
      const newSessionId = typeof result === 'string' ? undefined : result.sessionId;

      if (newSessionId) {
        process.stdout.write(`AGENT_SESSION:${newSessionId}\n`);
      }
      if (response) {
        process.stdout.write(response);
        if (!response.endsWith('\n')) process.stdout.write('\n');
      }
      process.exit(0);
    })
    .catch(err => {
      // A ProtocolError (thrown via sdk.protocolError) signals a user-facing
      // error → emit AGENT_ERROR. The isProtocolError marker is set on the
      // class, so legacy errors that only set the boolean still work too.
      if (err && err.isProtocolError) {
        const msg = String(err.message || 'unknown error');
        process.stdout.write(`AGENT_ERROR:${JSON.stringify(msg)}\n`);
        process.exit(1);
      }
      process.stderr.write(`[agentproc] handler error: ${err && err.stack || err}\n`);
      process.exit(1);
    });
}

/**
 * Error surfaced to the user as an AGENT_ERROR: line. Throw an instance from
 * a handler to report a user-readable error:
 *
 *   throw sdk.protocolError('API key expired');
 *
 * `await protocolError(...)` from older callers still works: awaiting a
 * non-thenable Error instance returns the instance itself, so the legacy
 * `throw await sdk.protocolError(msg)` form keeps functioning.
 *
 * Mirrors Python's `ProtocolError` exception class.
 */
class ProtocolError extends Error {
  constructor(message) {
    super(message || 'unknown error');
    this.isProtocolError = true;
  }
}

function protocolError(message) {
  return new ProtocolError(message);
}

module.exports = {
  createProfile,
  ProtocolError,
  loadHistory,
  appendHistory,
  sessionFilePath,
  protocolError,
  PROTOCOL_VERSION,
};
