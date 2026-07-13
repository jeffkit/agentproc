'use strict';
/**
 * agentproc — AgentProc Protocol SDK (Node.js)
 *
 * Implements the AgentProc P0 protocol (spec/protocol.md, wire protocol 0.3).
 *
 * Protocol contract (wire 0.3, NDJSON both directions):
 *   Input  — stdin: one {"type":"turn",...} line (message, session_id,
 *                     session_name, from_user, attachments, permission,
 *                     protocol_version). Secrets/config stay in env.
 *   Output — stdout (one JSON object per line, discriminated by `type`):
 *              {"type":"partial","text":...}     — streaming chunk
 *              {"type":"text","text":...}        — final reply body
 *              {"type":"session","id":...}       — declare session id (last wins)
 *              {"type":"error","message":...}    — error message to forward to user
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
const { PROTOCOL_VERSION, isValidSessionId } = require('./runner.js');

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
  // Defense in depth: the bridge validates {"type":"session"} ids with
  // isValidSessionId (see runner.js), which in 0.3 accepts any JSON string
  // on the wire EXCEPT path separators / control chars / `.` / `..` (a
  // storage-safety constraint, since we store each session as <id>.jsonl).
  // A handler can call loadHistory with any string; reject anything that
  // isn't a storage-safe filename component.
  if (!isValidSessionId(sessionId)) {
    throw new Error(`sessionId is not a safe filename component: ${JSON.stringify(sessionId)}`);
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
// Turn parsing — read the {"type":"turn",...} line from stdin
// ---------------------------------------------------------------------------

/**
 * Read exactly one line from stdin (the turn object) and JSON-decode it.
 * Synchronous read of fd 0 up to the first newline. Returns the parsed
 * object, or an empty object on any failure (best-effort, fail-soft per spec).
 */
function readTurn() {
  let raw = '';
  try {
    // Read available stdin up to the first newline. fs.readFileSync(0) reads
    // until EOF when the bridge closed stdin (permission off); when stdin is
    // kept open (permission on), the first line is the turn and the rest is
    // permission_response traffic the agent-side SDK does not handle here.
    const buf = fs.readFileSync(0, null);
    raw = buf.toString('utf8');
  } catch {
    return {};
  }
  const nl = raw.indexOf('\n');
  const line = nl >= 0 ? raw.slice(0, nl) : raw;
  try {
    const v = JSON.parse(line);
    if (v && typeof v === 'object' && !Array.isArray(v)) return v;
  } catch {
    // fall through
  }
  return {};
}

function contextFromTurn() {
  const t = readTurn();
  return {
    message: typeof t.message === 'string' ? t.message : '',
    sessionId: typeof t.session_id === 'string' ? t.session_id : '',
    sessionName: typeof t.session_name === 'string' ? t.session_name : 'default',
    fromUser: typeof t.from_user === 'string' ? t.from_user : '',
    protocolVersion: typeof t.protocol_version === 'string' ? t.protocol_version : PROTOCOL_VERSION,
    attachments: Array.isArray(t.attachments) ? t.attachments : [],
    permission: t.permission === true,

    /** Send a streaming chunk to the user immediately. */
    sendPartial(text, role) {
      if (!text) return;
      const evt = { type: 'partial', text };
      if (role) evt.role = role;
      process.stdout.write(JSON.stringify(evt) + '\n');
    },

    /** Send an error message to the user. Honored regardless of streaming mode. */
    sendError(text) {
      if (!text) return;
      process.stdout.write(JSON.stringify({ type: 'error', message: text }) + '\n');
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
  const ctx = contextFromTurn();

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
        process.stdout.write(JSON.stringify({ type: 'session', id: newSessionId }) + '\n');
      }
      if (response) {
        process.stdout.write(JSON.stringify({ type: 'text', text: response }) + '\n');
      }
      process.exit(0);
    })
    .catch(err => {
      // A ProtocolError (thrown via sdk.protocolError) signals a user-facing
      // error → emit an error event. The isProtocolError marker is set on the
      // class, so legacy errors that only set the boolean still work too.
      if (err && err.isProtocolError) {
        const msg = String(err.message || 'unknown error');
        process.stdout.write(JSON.stringify({ type: 'error', message: msg }) + '\n');
        process.exit(1);
      }
      process.stderr.write(`[agentproc] handler error: ${err && err.stack || err}\n`);
      process.exit(1);
    });
}

/**
 * Error surfaced to the user as an error event. Throw an instance from
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
