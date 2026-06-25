'use strict';
/**
 * agentproc — AgentProc Protocol SDK (Node.js)
 *
 * Implements the AgentProc P0 protocol so you can write a single async handler
 * instead of manually reading env vars and formatting stdout.
 *
 * Protocol contract:
 *   Input  — env vars: AGENT_MESSAGE, AGENT_SESSION_ID, AGENT_SESSION_NAME,
 *                      AGENT_FROM_USER, AGENT_STREAMING
 *   Output — stdout:
 *              optional first line  "AGENT_SESSION:<uuid>"
 *              optional lines       "AGENT_PARTIAL:<json-string>"
 *              remaining lines      = final reply text
 *   Exit   — 0 = success, non-zero = error
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

// ---------------------------------------------------------------------------
// History helpers (optional — for handlers calling LLM APIs directly)
// ---------------------------------------------------------------------------

function defaultSessionDir() {
  return path.join(os.homedir(), '.agentproc', 'sessions');
}

function sessionFilePath(sessionId, sessionDir) {
  return path.join(sessionDir || defaultSessionDir(), `${sessionId}.jsonl`);
}

/**
 * Load conversation history for a session from its JSONL file.
 * Returns [] if the file does not exist.
 *
 * @param {string} sessionId
 * @param {string} [sessionDir]
 * @returns {{ role: string, content: string, ts: string }[]}
 */
function loadHistory(sessionId, sessionDir) {
  if (!sessionId) return [];
  const file = sessionFilePath(sessionId, sessionDir);
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map(line => { try { return JSON.parse(line); } catch { return null; } })
    .filter(Boolean);
}

/**
 * Append entries to a session's JSONL history file.
 *
 * @param {string} sessionId
 * @param {{ role: string, content: string, ts?: string }[]} entries
 * @param {string} [sessionDir]
 */
function appendHistory(sessionId, entries, sessionDir) {
  if (!sessionId || !entries.length) return;
  const file = sessionFilePath(sessionId, sessionDir);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const lines = entries.map(e =>
    JSON.stringify({ role: e.role, content: e.content, ts: e.ts || new Date().toISOString() })
  );
  fs.appendFileSync(file, lines.join('\n') + '\n', 'utf8');
}

// ---------------------------------------------------------------------------
// createProfile — main entrypoint
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} AgentContext
 * @property {string}   message     - User message text (AGENT_MESSAGE)
 * @property {string}   sessionId   - Session UUID from previous turn (AGENT_SESSION_ID)
 * @property {string}   sessionName - Human-readable session name (AGENT_SESSION_NAME)
 * @property {string}   fromUser    - Sender identifier (AGENT_FROM_USER)
 * @property {boolean}  streaming   - Whether bridge expects streaming output
 * @property {string}   imageUrl    - Image attachment URL (AGENT_IMAGE_URL)
 * @property {string}   fileUrl     - File attachment URL (AGENT_FILE_URL)
 * @property {function} sendPartial - Send a streaming chunk immediately
 */

/**
 * @typedef {Object} AgentResult
 * @property {string}           response  - Final reply text
 * @property {string|undefined} sessionId - CLI session UUID to persist
 */

/**
 * Run a handler as an AgentProc-compliant process.
 *
 * @param {(ctx: AgentContext) => Promise<AgentResult | string>} handler
 */
function createProfile(handler) {
  const ctx = {
    message: process.env.AGENT_MESSAGE || '',
    sessionId: process.env.AGENT_SESSION_ID || '',
    sessionName: process.env.AGENT_SESSION_NAME || 'default',
    fromUser: process.env.AGENT_FROM_USER || '',
    streaming: (process.env.AGENT_STREAMING || '1') !== '0',
    imageUrl: process.env.AGENT_IMAGE_URL || '',
    fileUrl: process.env.AGENT_FILE_URL || '',

    /** Send a streaming chunk to the user immediately. */
    sendPartial(text) {
      if (!text) return;
      process.stdout.write(`AGENT_PARTIAL:${JSON.stringify(text)}\n`);
    },
  };

  Promise.resolve()
    .then(() => handler(ctx))
    .then(result => {
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
      process.stderr.write(`[agentproc] handler error: ${err?.stack || err}\n`);
      process.exit(1);
    });
}

module.exports = { createProfile, loadHistory, appendHistory, sessionFilePath };
