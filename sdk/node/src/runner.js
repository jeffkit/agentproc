'use strict';
/**
 * AgentProc runner — the core engine that turns a profile + message into a
 * protocol-compliant agent invocation.
 *
 * This module is the canonical implementation of the AgentProc bridge-side
 * contract (spec/protocol.md). The CLI (cli.js) is a thin wrapper around it.
 *
 * Responsibilities:
 *   - Parse and validate a profile object
 *   - Substitute {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}} placeholders
 *   - Inject AGENT_* env vars + profile env block
 *   - Spawn the agent command (no shell)
 *   - Read stdout line by line, classify protocol lines vs reply body
 *   - Forward AGENT_PARTIAL: in real time (via onPartial callback)
 *   - Capture the last AGENT_SESSION: line (last-wins rule)
 *   - Honor AGENT_ERROR: lines
 *   - Enforce timeout_secs with SIGTERM → kill_grace_secs → SIGKILL
 *   - Write message to stdin and close (when profile.stdin === 'message')
 *   - Return { reply, sessionId, error, exitCode }
 *
 * Exports:
 *   run(profile, options) -> Promise<RunResult>
 *   parseProfileYaml(yamlString) -> Profile
 *   classifyLine(line) -> { kind: 'session'|'partial'|'error'|'body', value }
 */

const { spawn } = require('node:child_process');
const path = require('node:path');
const os = require('node:os');

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PROTOCOL_VERSION = '0.1';

const DEFAULT_TIMEOUT_SECS = 1800;
const DEFAULT_KILL_GRACE_SECS = 5;
const DEFAULT_MAX_REPLY_CHARS = 8000;

const PREFIX_SESSION = 'AGENT_SESSION:';
const PREFIX_PARTIAL = 'AGENT_PARTIAL:';
const PREFIX_ERROR = 'AGENT_ERROR:';

// Exit codes per spec
const EXIT_SUCCESS = 0;
const EXIT_ERROR = 1;
const EXIT_TIMEOUT = 124;
const EXIT_SIGINT = 130;
const EXIT_SIGTERM = 143;

// ---------------------------------------------------------------------------
// Profile parsing & validation
// ---------------------------------------------------------------------------

/**
 * Validate and normalize a profile object (the `agentproc:` block from YAML,
 * already extracted by the caller — or a top-level profile for spec compatibility).
 *
 * @param {object} raw - Profile object.
 * @returns {object} Normalized profile.
 * @throws {Error} if required fields are missing or invalid.
 */
function normalizeProfile(raw) {
  if (!raw || typeof raw !== 'object') {
    throw new Error('profile must be an object');
  }
  // Accept either a top-level profile (spec form: command, args, ...)
  // or a hub form ({ agentproc: { command, ... } }).
  const p = raw.agentproc ? { ...raw.agentproc } : { ...raw };

  if (typeof p.command !== 'string' || p.command.trim() === '') {
    throw new Error('profile.command must be a non-empty string');
  }

  // Split command into argv on whitespace, no shell (per spec).
  const argv = p.command.trim().split(/\s+/);
  if (argv.length === 0) {
    throw new Error('profile.command produced empty argv');
  }

  return {
    argv,
    args: Array.isArray(p.args) ? p.args.map(String) : [],
    cwd: p.cwd ? expandPath(String(p.cwd)) : undefined,
    env: p.env && typeof p.env === 'object' ? p.env : {},
    stdin: p.stdin === 'message' ? 'message' : 'none',
    timeout_secs: Number.isFinite(p.timeout_secs) ? p.timeout_secs : DEFAULT_TIMEOUT_SECS,
    kill_grace_secs: Number.isFinite(p.kill_grace_secs) ? p.kill_grace_secs : DEFAULT_KILL_GRACE_SECS,
    max_reply_chars: Number.isFinite(p.max_reply_chars) ? p.max_reply_chars : DEFAULT_MAX_REPLY_CHARS,
    streaming: p.streaming !== false,
  };
}

function expandPath(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/')) return path.join(os.homedir(), p.slice(2));
  return p;
}

/**
 * Substitute {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}} placeholders
 * in a string value. Per spec, no shell is involved.
 */
function substitute(value, ctx) {
  return String(value)
    .replace(/\{\{MESSAGE\}\}/g, ctx.message || '')
    .replace(/\{\{SESSION_ID\}\}/g, ctx.sessionId || '')
    .replace(/\{\{SESSION_NAME\}\}/g, ctx.sessionName || '');
}

/**
 * Expand ${VAR} references against process.env, like a typical shell would.
 * Unknown variables expand to empty string (POSIX sh behavior).
 */
function expandEnvRef(value, env) {
  return String(value).replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_, name) => {
    const v = env[name];
    return v !== undefined ? v : '';
  });
}

// ---------------------------------------------------------------------------
// Line classification (per spec)
// ---------------------------------------------------------------------------

/**
 * Try to JSON-decode a value after a prefix.
 * Lenient mode (default per spec): on failure, return the raw text.
 */
function decodeJsonValue(raw) {
  const text = raw.trim();
  if (text === '') return '';
  try {
    const v = JSON.parse(text);
    return typeof v === 'string' ? v : String(v);
  } catch {
    // Lenient: treat as plain string.
    return text;
  }
}

/**
 * Classify one stdout line.
 * @param {string} line - Raw line, without trailing newline.
 * @returns {{ kind: string, value: string }}
 *   kind is 'session' | 'partial' | 'error' | 'body'.
 */
function classifyLine(line) {
  // Per spec: bridges MAY match against the stripped line to be tolerant
  // of leading whitespace from heredocs. We match the raw line — agents
  // that want their text to NOT be a protocol line should prefix with space.
  if (line.startsWith(PREFIX_SESSION)) {
    return { kind: 'session', value: line.slice(PREFIX_SESSION.length).trim() };
  }
  if (line.startsWith(PREFIX_PARTIAL)) {
    return { kind: 'partial', value: decodeJsonValue(line.slice(PREFIX_PARTIAL.length)) };
  }
  if (line.startsWith(PREFIX_ERROR)) {
    return { kind: 'error', value: decodeJsonValue(line.slice(PREFIX_ERROR.length)) };
  }
  return { kind: 'body', value: line };
}

// ---------------------------------------------------------------------------
// run() — the main entry point
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} RunOptions
 * @property {string} message - User message (required).
 * @property {string} [sessionId] - Session id from the previous turn (empty = new).
 * @property {string} [sessionName] - Human-readable session name.
 * @property {string} [fromUser] - Sender identifier.
 * @property {boolean} [streaming] - Override profile.streaming.
 * @property {Object<string, string>} [extraEnv] - Additional env vars (CLI --env).
 * @property {string} [cwd] - Override profile.cwd (CLI --cwd).
 * @property {number} [timeoutSecs] - Override profile.timeout_secs (CLI --timeout).
 * @property {function(string): void} [onPartial] - Streaming callback.
 * @property {function(string): void} [onSession] - Called when session id captured.
 * @property {function(string): void} [onError] - Called on AGENT_ERROR:.
 * @property {function(string): void} [onProtocolLine] - Raw protocol line (verbose/debug).
 * @property {function(string): void} [onStderr] - Agent's stderr line.
 * @property {boolean} [forwardStdin] - If true, write message to stdin (override profile.stdin).
 */

/**
 * @typedef {Object} RunResult
 * @property {string} reply - Concatenated reply body (non-protocol lines).
 * @property {string} sessionId - Final session id (last AGENT_SESSION: wins; '' if none).
 * @property {string} error - Error message from AGENT_ERROR:, or '' if none.
 * @property {number} exitCode - Process exit code (124 = timeout, etc.).
 * @property {boolean} timedOut - Whether the run was killed by timeout.
 */

/**
 * Run an agent process per the AgentProc spec.
 *
 * @param {object} profileRaw - The profile (top-level form or hub `agentproc:` form).
 * @param {RunOptions} options
 * @returns {Promise<RunResult>}
 */
async function run(profileRaw, options) {
  if (!options || typeof options.message !== 'string') {
    throw new Error('options.message is required');
  }

  const profile = normalizeProfile(profileRaw);
  const sessionId = options.sessionId || '';
  const sessionName = options.sessionName || 'default';
  const streaming = options.streaming !== undefined ? !!options.streaming : profile.streaming;
  const timeoutSecs = options.timeoutSecs !== undefined ? options.timeoutSecs : profile.timeout_secs;
  const cwd = options.cwd || profile.cwd;

  // Build the substitution context for {{MESSAGE}} etc.
  const substCtx = {
    message: options.message,
    sessionId,
    sessionName,
  };

  // Build argv: command + args (with placeholders substituted).
  const argv = [...profile.argv];
  for (const a of profile.args) {
    argv.push(substitute(a, substCtx));
  }

  // Build env: start with process.env (so PATH etc. work), add profile.env
  // (with ${VAR} refs expanded against process.env), then add AGENT_* vars.
  const env = { ...process.env };
  for (const [k, v] of Object.entries(profile.env)) {
    env[k] = expandEnvRef(substitute(v, substCtx), process.env);
  }
  if (options.extraEnv) {
    for (const [k, v] of Object.entries(options.extraEnv)) {
      env[k] = String(v);
    }
  }

  // Inject AGENT_* vars per spec.
  env.AGENT_MESSAGE = options.message;
  env.AGENT_SESSION_ID = sessionId;
  env.AGENT_SESSION_NAME = sessionName;
  env.AGENT_FROM_USER = options.fromUser || '';
  env.AGENT_STREAMING = streaming ? '1' : '0';
  env.AGENT_PROTOCOL_VERSION = PROTOCOL_VERSION;

  // Spawn — no shell. Cwd optional.
  const child = spawn(argv[0], argv.slice(1), {
    cwd,
    env,
    stdio: [
      profile.stdin === 'message' || options.forwardStdin ? 'pipe' : 'ignore',
      'pipe',
      'pipe',
    ],
  });

  /** @type {RunResult} */
  const result = {
    reply: '',
    sessionId: '',
    error: '',
    exitCode: 0,
    timedOut: false,
  };

  const bodyLines = [];
  let killed = false;

  // ---- stdout: line-by-line classification ----
  let stdoutBuf = '';
  child.stdout.on('data', chunk => {
    stdoutBuf += chunk.toString();
    let nl;
    while ((nl = stdoutBuf.indexOf('\n')) >= 0) {
      const rawLine = stdoutBuf.slice(0, nl);
      stdoutBuf = stdoutBuf.slice(nl + 1);
      handleLine(rawLine);
    }
  });

  function handleLine(rawLine) {
    // Strip a trailing \r (CRLF tolerance) but otherwise treat raw.
    const line = rawLine.replace(/\r$/, '');
    const c = classifyLine(line);
    if (c.kind === 'session') {
      result.sessionId = c.value; // last wins
      if (options.onSession) options.onSession(c.value);
      if (options.onProtocolLine) options.onProtocolLine(line);
    } else if (c.kind === 'partial') {
      if (streaming && options.onPartial) options.onPartial(c.value);
      if (options.onProtocolLine) options.onProtocolLine(line);
    } else if (c.kind === 'error') {
      result.error = c.value;
      if (options.onError) options.onError(c.value);
      if (options.onProtocolLine) options.onProtocolLine(line);
    } else {
      bodyLines.push(line);
    }
  }

  // ---- stderr: forward as debug ----
  let stderrBuf = '';
  child.stderr.on('data', chunk => {
    stderrBuf += chunk.toString();
    let nl;
    while ((nl = stderrBuf.indexOf('\n')) >= 0) {
      const line = stderrBuf.slice(0, nl);
      stderrBuf = stderrBuf.slice(nl + 1);
      if (options.onStderr) options.onStderr(line.replace(/\r$/, ''));
    }
  });

  // ---- stdin (if requested) ----
  if (profile.stdin === 'message' || options.forwardStdin) {
    child.stdin.write(options.message);
    child.stdin.end();
  }

  // ---- timeout handling per spec: SIGTERM → grace → SIGKILL ----
  let timer = null;
  if (timeoutSecs > 0) {
    timer = setTimeout(() => {
      killed = true;
      result.timedOut = true;
      try { child.kill('SIGTERM'); } catch {}
      setTimeout(() => {
        try {
          if (!child.exitCode && child.signalCode === null) {
            child.kill('SIGKILL');
          }
        } catch {}
      }, (profile.kill_grace_secs || DEFAULT_KILL_GRACE_SECS) * 1000);
    }, timeoutSecs * 1000);
  }

  // ---- wait for exit ----
  const exitCode = await new Promise(resolve => {
    child.on('close', code => resolve(code));
    child.on('error', err => {
      // spawn error (ENOENT etc.)
      if (options.onStderr) options.onStderr(`[agentproc runner] spawn error: ${err.message}`);
      resolve(EXIT_ERROR);
    });
  });

  if (timer) clearTimeout(timer);

  // Flush any remaining stdout buffer as a final line (without trailing \n).
  if (stdoutBuf.length > 0) {
    handleLine(stdoutBuf.replace(/\r$/, ''));
  }

  // Compose reply body.
  result.reply = bodyLines.join('\n');
  if (result.reply.length > profile.max_reply_chars) {
    const suffix = profile.max_reply_chars === DEFAULT_MAX_REPLY_CHARS
      ? '\n\n…(truncated)'
      : '';
    result.reply = result.reply.slice(0, profile.max_reply_chars) + suffix;
  }

  // Exit code per spec.
  if (killed) {
    result.exitCode = EXIT_TIMEOUT;
  } else if (result.error) {
    // AGENT_ERROR was emitted — treat as failure even if exit was 0.
    result.exitCode = exitCode === 0 ? EXIT_ERROR : exitCode;
  } else {
    result.exitCode = exitCode;
  }

  return result;
}

module.exports = {
  run,
  normalizeProfile,
  classifyLine,
  decodeJsonValue,
  substitute,
  expandEnvRef,
  expandPath,
  PROTOCOL_VERSION,
  DEFAULT_TIMEOUT_SECS,
  DEFAULT_KILL_GRACE_SECS,
  DEFAULT_MAX_REPLY_CHARS,
  EXIT_SUCCESS,
  EXIT_ERROR,
  EXIT_TIMEOUT,
  EXIT_SIGINT,
  EXIT_SIGTERM,
};
