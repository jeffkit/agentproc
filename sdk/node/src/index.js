'use strict';
/**
 * agentproc — AgentProc Protocol SDK (Node.js)
 *
 * Implements the AgentProc P0 protocol (spec/protocol.md, wire protocol 0.4).
 *
 * Protocol contract (wire 0.4, NDJSON both directions):
 *   Input  — stdin: one {"type":"turn",...} line (message, session_id,
 *                     session_name, attachments, permission,
 *                     protocol_version). Secrets/config stay in env.
 *   Output — stdout (one JSON object per line, discriminated by `type`):
 *              {"type":"partial","text":...,"session_id"?}  — streaming chunk
 *              {"type":"result","text":...,"session_id"?}   — terminal success body
 *              {"type":"error","message":...,"session_id"?} — error to forward
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
const { PROTOCOL_VERSION, isValidSessionId, executorNames, EXECUTORS } = require('./runner.js');

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
  // Defense in depth: the bridge validates `session_id` fields with
  // isValidSessionId (see runner.js), which in 0.4 accepts any JSON string
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
 *
 * Reads byte-by-byte from fd 0 until the first newline (or EOF). This is
 * deliberate: `fs.readFileSync(0)` reads until EOF, which deadlocks the
 * agent process when the bridge keeps stdin open for permission traffic
 * (`profile.permission: true`). A byte-at-a-time loop is fast enough on
 * the turn payload (a few KB at most) and lets stdin stay open for the
 * handler to read subsequent permission_response frames itself.
 *
 * Returns the parsed object, or an empty object on any failure
 * (best-effort, fail-soft per spec).
 */
function readTurn() {
  let line = '';
  try {
    const buf = Buffer.alloc(1);
    while (true) {
      // fs.readSync returns 0 at EOF, >0 on a byte read. EAGAIN/EWOULDBLOCK
      // would manifest as a thrown error on fd 0 in practice (the bridge
      // either wrote data or closed), so a synchronous loop is safe here.
      const n = fs.readSync(0, buf, 0, 1);
      if (n === 0) break;            // EOF
      const ch = buf[0];
      if (ch === 0x0a) break;        // \n
      line += buf.toString('utf8');
    }
  } catch {
    // Best-effort: an error reading stdin yields an empty turn. The handler
    // will then see a default-empty context, which is the spec's fail-soft.
    return {};
  }
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
  const permissionEnabled = t.permission === true;
  return {
    message: typeof t.message === 'string' ? t.message : '',
    sessionId: typeof t.session_id === 'string' ? t.session_id : '',
    sessionName: typeof t.session_name === 'string' ? t.session_name : 'default',
    protocolVersion: typeof t.protocol_version === 'string' ? t.protocol_version : PROTOCOL_VERSION,
    attachments: Array.isArray(t.attachments) ? t.attachments : [],
    permission: permissionEnabled,

    /** Send a streaming chunk to the user immediately. */
    sendPartial(text, role) {
      if (!text) return;
      const evt = { type: 'partial', text };
      if (role) evt.role = role;
      process.stdout.write(JSON.stringify(evt) + '\n');
    },

    /**
     * Send an error message to the user. Honored regardless of streaming mode.
     * Non-terminal: the handler may continue and return a body after this call.
     * If a result follows an error on stdout, the bridge discards the result.
     * For a truly fatal path, throw `await sdk.protocolError(msg)` instead.
     */
    sendError(text) {
      if (!text) return;
      process.stdout.write(JSON.stringify({ type: 'error', message: text }) + '\n');
    },

    /**
     * Send a tool-permission request to the bridge (only valid when
     * `ctx.permission === true`). The bridge surfaces it to the user; the
     * matching decision arrives on stdin as a
     * `{"type":"permission_response",...}` line, which `readPermissionResponse`
     * decodes.
     *
     * `request_id` MUST be unique within the turn. The bridge echoes it in
     * the response.
     */
    sendPermissionRequest(req) {
      if (!permissionEnabled) {
        throw new Error(
          'ctx.sendPermissionRequest() requires profile.permission: true — the bridge would otherwise not honor the request'
        );
      }
      if (!req || typeof req !== 'object') {
        throw new Error('sendPermissionRequest(req): req must be an object');
      }
      if (typeof req.request_id !== 'string' || !req.request_id) {
        throw new Error('sendPermissionRequest: req.request_id is required');
      }
      const payload = { type: 'permission_request' };
      for (const k of ['request_id', 'tool_name', 'input']) {
        payload[k] = req[k];
      }
      // Optional fields the agent MAY include.
      for (const k of ['description', 'tool_use_id']) {
        if (req[k] !== undefined) payload[k] = req[k];
      }
      process.stdout.write(JSON.stringify(payload) + '\n');
    },

    /**
     * Read the next `{"type":"permission_response",...}` frame from stdin.
     * Blocks until a frame arrives (or EOF). Returns the parsed object
     * (containing `request_id`, `behavior`, optional `updated_input` /
     * `message`), or `null` on EOF.
     *
     * Only valid when `ctx.permission === true`. The bridge MUST have
     * kept stdin open for permission traffic; in `permission: false` mode
     * stdin is closed after the turn line, so this would return `null`
     * immediately.
     */
    readPermissionResponse() {
      return readNextStdinLine();
    },
  };
}

/**
 * Read one NDJSON line from stdin (used by `ctx.readPermissionResponse`).
 * Returns the parsed object, or null at EOF / on JSON error.
 */
function readNextStdinLine() {
  let line = '';
  try {
    const buf = Buffer.alloc(1);
    while (true) {
      const n = fs.readSync(0, buf, 0, 1);
      if (n === 0) return null;          // EOF
      const ch = buf[0];
      if (ch === 0x0a) break;            // \n
      line += buf.toString('utf8');
    }
  } catch {
    return null;
  }
  try {
    const v = JSON.parse(line);
    if (v && typeof v === 'object' && !Array.isArray(v)) return v;
  } catch {
    // fall through
  }
  return null;
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

      // Wire 0.4: one {"type":"result"} with optional session_id on the event.
      if (response || newSessionId) {
        const evt = { type: 'result', text: response };
        if (newSessionId) evt.session_id = newSessionId;
        process.stdout.write(JSON.stringify(evt) + '\n');
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
  executorNames,
  EXECUTORS,
};
