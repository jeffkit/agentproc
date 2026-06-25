'use strict';
/**
 * agentproc — AgentProc Protocol SDK (Node.js)
 *
 * Implements the AgentProc P0 protocol (spec/protocol.md, v0.1.0).
 *
 * Protocol contract:
 *   Input  — env vars: AGENT_MESSAGE, AGENT_SESSION_ID, AGENT_SESSION_NAME,
 *                      AGENT_FROM_USER, AGENT_STREAMING, AGENT_PROTOCOL_VERSION,
 *                      AGENT_IMAGE_URL, AGENT_FILE_URL, AGENT_ATTACHMENTS (draft)
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

const PROTOCOL_VERSION = '0.1';

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

/**
 * Parse AGENT_ATTACHMENTS JSON. Returns [] on parse failure.
 * @param {string} raw
 * @returns {Attachment[]}
 */
function parseAttachments(raw) {
  if (!raw) return [];
  let items;
  try {
    items = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(items)) return [];
  /** @type {Attachment[]} */
  const out = [];
  for (const it of items) {
    if (!it || typeof it !== 'object') continue;
    const t = String(it.type || '');
    const u = String(it.url || '');
    if (!t || !u) continue;
    out.push({ type: t, url: u, name: String(it.name || '') });
  }
  return out;
}

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
    attachments: parseAttachments(process.env.AGENT_ATTACHMENTS || ''),

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
      // Match Python SDK behavior: a ProtocolError-like object signals a user-facing error.
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
 * Throw this to surface a user-readable error via AGENT_ERROR.
 * @param {string} message
 * @returns {Promise<never>}
 */
async function protocolError(message) {
  const err = new Error(message || 'unknown error');
  err.isProtocolError = true;
  throw err;
}

module.exports = {
  createProfile,
  loadHistory,
  appendHistory,
  sessionFilePath,
  parseAttachments,
  protocolError,
  PROTOCOL_VERSION,
};
