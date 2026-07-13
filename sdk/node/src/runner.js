'use strict';
/**
 * AgentProc runner — the core engine that turns a profile + message into a
 * protocol-compliant agent invocation.
 *
 * This module is the canonical implementation of the AgentProc bridge-side
 * contract (spec/protocol.md, wire protocol 0.3). The CLI (cli.js) is a thin wrapper around it.
 *
 * Wire 0.3 is NDJSON in both directions:
 *   - stdin:  one {"type":"turn",...} line, then optional
 *             {"type":"permission_response",...} lines when permission is on.
 *   - stdout: one JSON object per line, discriminated by `type`:
 *             partial | text | session | error | permission_request.
 *
 * Responsibilities:
 *   - Parse and validate a profile object
 *   - Substitute {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}}, {{PROFILE_DIR}} placeholders
 *   - Build the child env (infra set + profile env block + CLI --env extras)
 *   - Spawn the agent command (no shell); command is always argv[0], never split
 *   - Write the turn object to the agent's stdin (and keep stdin open when
 *     profile.permission is true, for permission_response traffic)
 *   - Read stdout line by line, parse each line as a JSON event
 *   - Forward {"type":"partial"} in real time (via onPartial callback)
 *   - Capture the last {"type":"session"} event (last-wins rule)
 *   - Honor {"type":"error"} events
 *   - Optional tool permission: honor permission_request / write permission_response
 *   - Enforce timeout_secs with SIGTERM → kill_grace_secs → SIGKILL
 *   - Return { reply, sessionId, error, exitCode }
 *
 * Exports:
 *   run(profile, options) -> Promise<RunResult>
 *   parseProfileYaml(yamlString) -> Profile   (re-exported from yaml.js where used)
 *   classifyLine(line) -> { kind: 'session'|'partial'|'text'|'error'|'permission_request'|'malformed', value }
 */

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PROTOCOL_VERSION = '0.3';

const DEFAULT_TIMEOUT_SECS = 1800;
const DEFAULT_KILL_GRACE_SECS = 5;
const DEFAULT_MAX_REPLY_CHARS = 8000;
const DEFAULT_TRUNCATION_SUFFIX = '\n\n…(truncated)';

// Exit codes per spec
const EXIT_SUCCESS = 0;
const EXIT_ERROR = 1;
const EXIT_TIMEOUT = 124;
const EXIT_SIGINT = 130;
const EXIT_SIGTERM = 143;

// ---------------------------------------------------------------------------
// Environment composition policy (wire 0.3)
// ---------------------------------------------------------------------------
//
// The child env is built from exactly three layers (later overrides earlier):
//   (1) this minimal INFRA set (copied from process.env when present),
//   (2) the profile `env` block (${VAR} expanded; optionally allowlist-filtered),
//   (3) extraEnv from the CLI --env flag.
// The per-turn request does NOT travel in env (it travels on stdin as the
// turn object), so there are no AGENT_* injections. There is no
// `env_inherit: all` escape hatch in 0.3 — the infra set is always the base.
const ENV_INFRA_VARS = [
  'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE',
  'LC_MESSAGES', 'TERM', 'TMPDIR', 'TZ', 'PWD',
  // Windows infra
  'SystemRoot', 'TEMP', 'TMP', 'USERPROFILE', 'USERNAME', 'PATHEXT',
  'COMSPEC', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'NUMBER_OF_PROCESSORS',
  'PROCESSOR_ARCHITECTURE', 'OS',
];

function buildBaseEnv() {
  const base = {};
  for (const name of ENV_INFRA_VARS) {
    if (process.env[name] !== undefined) base[name] = process.env[name];
  }
  return base;
}

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
  const src = raw.agentproc && typeof raw.agentproc === 'object' ? raw.agentproc : raw;

  if (typeof src.command !== 'string' || src.command.trim() === '') {
    throw new Error('profile.command must be a non-empty string');
  }

  // Wire 0.3: `command` is always argv[0], a single token, NEVER split —
  // even if it contains whitespace. `args` is argv[1..], a YAML list of
  // tokens, defaulting to []. The 0.2 "args absent ⇒ split command on
  // whitespace" shorthand is removed. Paths with whitespace are carried whole
  // by YAML quoting and passed to execve as one token.
  const command = src.command.trim();
  const argsValue = Array.isArray(src.args) ? src.args.map(String) : [];
  const argv = [command];

  // env_allowlist (optional): when present, ${VAR} references in the env
  // block whose name is NOT in the list expand to empty + a stderr warning.
  // Absent ⇒ expand against the full bridge environment.
  let envAllowlist = null;
  if (src.env_allowlist !== undefined && src.env_allowlist !== null) {
    if (!Array.isArray(src.env_allowlist)) {
      throw new Error('profile.env_allowlist must be a list');
    }
    envAllowlist = new Set(src.env_allowlist.map(String));
  }

  return {
    command,
    argv,
    args: argsValue,
    cwd: src.cwd ? expandPath(String(src.cwd)) : undefined,
    env: src.env && typeof src.env === 'object' ? src.env : {},
    env_allowlist: envAllowlist,
    // Opt-in tool-authorization channel (wire 0.3). Default false — profiles
    // without mid-turn approval keep using CLI auto-approve / skip-permissions.
    permission: src.permission === true,
    timeout_secs: Number.isFinite(src.timeout_secs) ? src.timeout_secs : DEFAULT_TIMEOUT_SECS,
    kill_grace_secs: Number.isFinite(src.kill_grace_secs) ? src.kill_grace_secs : DEFAULT_KILL_GRACE_SECS,
    max_reply_chars: Number.isFinite(src.max_reply_chars) ? src.max_reply_chars : DEFAULT_MAX_REPLY_CHARS,
    // Spec profile YAML: optional truncation notice appended when the reply is
    // capped. Defaults to "\n\n…(truncated)". An empty string disables the
    // notice entirely (the cap still applies, just no visible marker).
    truncation_suffix: typeof src.truncation_suffix === 'string'
      ? src.truncation_suffix
      : DEFAULT_TRUNCATION_SUFFIX,
    // Bridge-side hint: when false, the runner ignores {"type":"partial"} events
    // and assembles the reply from {"type":"text"} events only. Not a wire field.
    streaming: src.streaming !== false,
  };
}

function expandPath(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/')) return path.join(os.homedir(), p.slice(2));
  return p;
}

// Shared (pattern, hint) table for post-mortem stderr diagnosis. This is the
// runtime-embedded copy of spec/conformance/diagnostics.json — the single
// source of truth. The conformance test asserts the two stay in sync. Rules
// are evaluated in order; first match wins. A `{n}` token in the hint is
// replaced by capture group n; `{{PROFILE_DIR}}` is a literal, not a format
// token (the `\{(\d+)\}` replacer only touches numeric tokens).
const STDERR_DIAGNOSTICS = [
  {
    id: 'python-open-file',
    pattern: "(?:can'?t|cannot) open file '([^']+)': \\[Errno 2\\] No such file or directory",
    hint: 'agent script not found: {1}. Check the profile\'s command path (likely a {{PROFILE_DIR}} issue or a typo).',
  },
  {
    id: 'node-cannot-find-module',
    pattern: "Cannot find module '([^']+)'",
    hint: 'agent script not found: {1}. Check the profile\'s command path (likely a {{PROFILE_DIR}} issue or a typo).',
  },
  {
    id: 'bash-line-no-such-file',
    pattern: '(?:^|\\n)[^:]+: line \\d+: ([^:]+): No such file or directory',
    hint: 'agent script not found: {1}. Check the profile\'s command path.',
  },
  {
    id: 'generic-enoent',
    pattern: 'errno 2|enoent|no such file or directory',
    flags: 'i',
    hint: 'agent reported a missing file. Check the profile\'s command and cwd.',
  },
];

/**
 * Best-effort pattern check against the agent's accumulated stderr to spot
 * common "bridge file not found" / "module not found" failures that the
 * wrapped interpreter writes to its own stderr before exiting non-zero.
 * Returns a human-friendly hint, or '' if nothing recognizable.
 *
 * Data-driven by STDERR_DIAGNOSTICS (the embedded mirror of
 * spec/conformance/diagnostics.json). Intentionally narrow — we only flag
 * high-confidence patterns to avoid mis-diagnosing genuine agent errors.
 */
function diagnoseStderrFailure(stderrText) {
  if (!stderrText) return '';
  for (const rule of STDERR_DIAGNOSTICS) {
    const re = rule.flags ? new RegExp(rule.pattern, rule.flags) : new RegExp(rule.pattern);
    const m = stderrText.match(re);
    if (m) {
      return rule.hint.replace(/\{(\d+)\}/g, (_, n) => m[Number(n)] || '');
    }
  }
  return '';
}

/**
 * Produce a human-friendly hint for a spawn ENOENT-style error.
 *
 * Node's spawn attributes the error to argv[0] regardless of whether it was
 * the command itself or a referenced file (e.g. `./bridge.py`) that wasn't
 * found, which is very confusing. We inspect cwd + argv to give a better
 * diagnosis. Returns '' when nothing useful can be said.
 */
function diagnoseSpawnError(err, { argv, cwd, env }) {
  const code = err && err.code;
  const message = (err && err.message) || '';
  if (code !== 'ENOENT' && !/ENOENT/.test(message)) return '';

  // (a) cwd doesn't exist or isn't a directory
  if (cwd) {
    try {
      const stat = fs.statSync(cwd);
      if (!stat.isDirectory()) {
        return `profile.cwd is not a directory: ${cwd}`;
      }
    } catch (e) {
      if (e && (e.code === 'EACCES' || e.code === 'EPERM')) {
        return `profile.cwd is not accessible (permission denied): ${cwd}`;
      }
      return `profile.cwd does not exist: ${cwd}. Pass --cwd <path> to point at a real directory.`;
    }
  }

  // (b) the command (argv[0]) is not on PATH
  const cmd = argv[0];
  const isPathed = /[\\/]/.test(cmd);
  if (!isPathed) {
    // Bare command like 'python3' or 'claude' — check PATH ourselves.
    const PATH = (env && env.PATH) || '';
    if (PATH) {
      const found = PATH.split(path.delimiter).some(d => {
        try {
          const p = path.join(d, cmd);
          fs.accessSync(p, fs.constants.X_OK);
          return true;
        } catch { return false; }
      });
      if (!found) {
        return `'${cmd}' not found on PATH. Install it, or if it's installed, make sure PATH is set correctly when the bridge spawns the agent.`;
      }
    }
    return `'${cmd}' could not be executed. Verify it is installed and on PATH.`;
  }

  // (c) argv[0] looks like a path — check whether the file itself exists
  try {
    fs.accessSync(cmd, fs.constants.X_OK);
  } catch {
    return `command path does not exist or is not executable: ${cmd}`;
  }

  // (d) Command exists; suspect an argv file argument (e.g. python3 ./bridge.py).
  for (let i = 1; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('-') && (a.includes('/') || a.includes('\\'))) {
      // Resolve relative to cwd (mirrors spawn's resolution)
      const resolved = path.isAbsolute(a) ? a : (cwd ? path.resolve(cwd, a) : path.resolve(a));
      try {
        fs.accessSync(resolved, fs.constants.R_OK);
      } catch {
        return `argument file not found: ${a} (resolved to ${resolved}). The profile likely needs --cwd or the bundled script path is wrong.`;
      }
    }
  }

  return '';
}

/**
 * Substitute {{MESSAGE}}, {{SESSION_ID}}, {{SESSION_NAME}}, {{PROFILE_DIR}}
 * placeholders in a string value. Per spec, no shell is involved.
 */
function substitute(value, ctx) {
  return String(value)
    .replace(/\{\{MESSAGE\}\}/g, ctx.message || '')
    .replace(/\{\{SESSION_ID\}\}/g, ctx.sessionId || '')
    .replace(/\{\{SESSION_NAME\}\}/g, ctx.sessionName || '')
    .replace(/\{\{PROFILE_DIR\}\}/g, ctx.profileDir || '');
}

/**
 * Expand ${VAR} references against `env`, like a typical shell would.
 * Unknown variables expand to empty string (POSIX sh behavior).
 *
 * When `allowlist` is a Set of names, references to names NOT in the set
 * expand to empty and `onBlocked` (if given) is called with each blocked
 * name. When `allowlist` is null, all references expand normally.
 */
function expandEnvRef(value, env, allowlist = null, onBlocked = null) {
  return String(value).replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_, name) => {
    if (allowlist && !allowlist.has(name)) {
      if (onBlocked) onBlocked(name);
      return '';
    }
    const v = env[name];
    return v !== undefined ? v : '';
  });
}

// ---------------------------------------------------------------------------
// Event parsing (wire 0.3 — every stdout line is a JSON object)
// ---------------------------------------------------------------------------

/**
 * Parse one stdout line as a JSON object. Returns the object, or null on
 * failure (not valid JSON, not an object).
 */
function parseJsonLine(line) {
  const text = line.trim();
  if (text === '') return null;
  try {
    const v = JSON.parse(text);
    if (v && typeof v === 'object' && !Array.isArray(v)) return v;
  } catch {
    // fall through
  }
  return null;
}

/**
 * Classify one stdout line into a typed event.
 * @param {string} line - Raw line, without trailing newline.
 * @returns {{ kind: string, value: any, role?: string }}
 *   kind is 'session' | 'partial' | 'text' | 'error' | 'permission_request' | 'malformed'.
 *   - session: value is the id string.
 *   - partial: value is the text string; role is the optional role string.
 *   - text:    value is the text string.
 *   - error:   value is the message string.
 *   - permission_request: value is the parsed object (or null if malformed).
 *   - malformed: the line was not a recognised event; the bridge logs + ignores.
 */
function classifyLine(line) {
  const obj = parseJsonLine(line);
  if (!obj || typeof obj.type !== 'string') {
    return { kind: 'malformed', value: line };
  }
  switch (obj.type) {
    case 'session':
      return { kind: 'session', value: typeof obj.id === 'string' ? obj.id : '' };
    case 'partial': {
      const o = { kind: 'partial', value: typeof obj.text === 'string' ? obj.text : '' };
      if (typeof obj.role === 'string') o.role = obj.role;
      return o;
    }
    case 'text':
      return { kind: 'text', value: typeof obj.text === 'string' ? obj.text : '' };
    case 'error':
      return { kind: 'error', value: typeof obj.message === 'string' ? obj.message : '' };
    case 'permission_request':
      return { kind: 'permission_request', value: obj };
    default:
      return { kind: 'malformed', value: line };
  }
}

/**
 * Format a {"type":"permission_response",...} line for stdin.
 * @param {{ request_id: string, behavior: 'allow'|'deny', updated_input?: object, message?: string }} decision
 */
function formatPermissionResponse(decision) {
  const payload = {
    type: 'permission_response',
    request_id: String(decision.request_id),
    behavior: decision.behavior === 'allow' ? 'allow' : 'deny',
  };
  if (decision.updated_input != null && typeof decision.updated_input === 'object') {
    payload.updated_input = decision.updated_input;
  }
  if (decision.message != null && decision.message !== '') {
    payload.message = String(decision.message);
  }
  return JSON.stringify(payload);
}

function isValidPermissionRequest(obj) {
  if (!obj || typeof obj !== 'object') return false;
  if (typeof obj.request_id !== 'string' || obj.request_id.trim() === '') return false;
  if (/[\s\r\n\x00-\x1f]/.test(obj.request_id)) return false;
  if (typeof obj.tool_name !== 'string' || obj.tool_name === '') return false;
  if (obj.input == null || typeof obj.input !== 'object' || Array.isArray(obj.input)) return false;
  return true;
}

// Wire 0.3: the session id is an arbitrary JSON string on the wire (no
// colon/whitespace restriction — that was an artifact of the 0.2
// colon-delimited prefix). The only remaining constraint is STORAGE safety:
// the SDK history helpers store each session as <id>.jsonl, so an id
// containing a path separator (/ or \), a NUL / control char, or equal to
// `.` / `..` would path-traverse out of the sessions directory. The runner
// rejects such ids (preserving the previously captured id + a stderr warning)
// so they do not round-trip. Colons, spaces, `+`, and unicode are all fine.
function isValidSessionId(value) {
  if (typeof value !== 'string' || value.length === 0) return false;
  if (value === '.' || value === '..') return false;
  // Reject path separators and control chars (incl. NUL and newline).
  if (/[\/\\\x00-\x1f]/.test(value)) return false;
  return true;
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
 * @property {boolean} [streaming] - Override profile.streaming (bridge-side hint).
 * @property {Object<string, string>} [extraEnv] - Additional env vars (CLI --env).
 * @property {Array<{kind:string,url:string}>} [attachments] - Attachments for the turn.
 * @property {string} [cwd] - Override profile.cwd (CLI --cwd).
 * @property {number} [timeoutSecs] - Override profile.timeout_secs (CLI --timeout).
 * @property {function(string, string=): void} [onPartial] - Streaming callback (text, role?).
 * @property {function(string): void} [onSession] - Called when session id captured.
 * @property {function(string): void} [onError] - Called on {"type":"error"}.
 * @property {function(object): (object|Promise<object>|void)} [onPermission] -
 *   Called on {"type":"permission_request"} when profile.permission is true.
 *   Return (or resolve to) { behavior: 'allow'|'deny', updated_input?, message? }
 *   to write a permission_response; omit / return nothing to leave the
 *   agent blocked until timeout.
 * @property {function(string): void} [onProtocolLine] - Raw protocol line (verbose/debug).
 * @property {function(string): void} [onStderr] - Agent's stderr line.
 */

/**
 * @typedef {Object} RunResult
 * @property {string} reply - Concatenated reply body (from {"type":"text"} events).
 * @property {string} sessionId - Final session id (last {"type":"session"} wins; '' if none).
 * @property {string} error - Error message from {"type":"error"}, or '' if none.
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
  // `!= null` (not `!== undefined`) so CLI can pass `null` to mean "defer to
  // profile" without silently forcing streaming off — matches Python's
  // `is not None`. See cli.js `streaming` option.
  const streaming = options.streaming != null ? !!options.streaming : profile.streaming;
  const timeoutSecs = options.timeoutSecs != null ? options.timeoutSecs : profile.timeout_secs;
  let cwd = options.cwd || profile.cwd;
  // Resolve relative cwd against the profile's directory (if known) so that
  // profiles written as `cwd: .` work no matter where the user invokes from.
  // Absolute paths and `~`-prefixed paths are already absolute post-expand.
  if (cwd && !path.isAbsolute(cwd) && options.profileDir) {
    cwd = path.resolve(options.profileDir, cwd);
  }

  // Build the substitution context for {{MESSAGE}} etc.
  const substCtx = {
    message: options.message,
    sessionId,
    sessionName,
    profileDir: options.profileDir || '',
  };

  // Build argv: command + args (with placeholders substituted).
  const argv = profile.argv.map(a => substitute(a, substCtx));
  for (const a of profile.args) {
    argv.push(substitute(a, substCtx));
  }

  // Build env per the composition policy (infra set + profile env + --env).
  // No AGENT_* injections in 0.3 — the per-turn request travels on stdin.
  const allowlist = profile.env_allowlist;
  const env = buildBaseEnv();
  for (const [k, v] of Object.entries(profile.env)) {
    env[k] = expandEnvRef(substitute(v, substCtx), process.env, allowlist, (name) => {
      if (options.onStderr) {
        options.onStderr(`[agentproc runner] env_allowlist blocked \${${name}} (not in allowlist); expanded to empty`);
      }
    });
  }
  if (options.extraEnv) {
    for (const [k, v] of Object.entries(options.extraEnv)) {
      env[k] = String(v);
    }
  }

  // Build the turn object (wire 0.3 stdin payload).
  const turn = {
    type: 'turn',
    message: options.message,
    session_id: sessionId,
    session_name: sessionName,
    from_user: options.fromUser || '',
    protocol_version: PROTOCOL_VERSION,
  };
  // attachments: include the key when the caller provided an attachments
  // array (presence-as-feature); omit otherwise.
  if (Array.isArray(options.attachments)) {
    turn.attachments = options.attachments;
  }
  if (profile.permission) turn.permission = true;

  // stdin is always a pipe: we always write the turn line. When permission is
  // on, we keep stdin open afterwards for permission_response traffic.
  const needStdinPipe = true;

  // Spawn — no shell. Cwd optional.
  const child = spawn(argv[0], argv.slice(1), {
    cwd,
    env,
    stdio: [
      needStdinPipe ? 'pipe' : 'ignore',
      'pipe',
      'pipe',
    ],
  });

  // The agent may exit (or close its stdin) before/while we write the turn
  // line — e.g. a one-shot CLI that reads nothing from stdin. In that case
  // writes to child.stdin fail with EPIPE, which Node emits asynchronously
  // as an 'error' event on the stream. Without a listener it becomes an
  // uncaughtException and crashes the bridge. An early-exit agent is a
  // legitimate condition (the run() result is derived from stdout/exit
  // code), so swallow stream errors here.
  if (child.stdin) {
    child.stdin.on('error', (err) => {
      if (err && err.code === 'EPIPE') return;
      // Re-throw genuinely unexpected stream errors.
      throw err;
    });
  }

  /** @type {RunResult} */
  const result = {
    reply: '',
    sessionId: '',
    error: '',
    exitCode: 0,
    timedOut: false,
  };

  const textChunks = [];
  let killed = false;
  // Spec 5.4: once an error event arrives, subsequent partial/text events
  // MUST be discarded (they cannot contribute to a failed turn's reply).
  // `session` is exempt — last-wins still applies so the id for the next
  // turn can be captured even after an error.
  let errorSeen = false;
  /** @type {Set<string>} */
  const pendingPermissionIds = new Set();
  let stdinClosed = false;

  function writePermissionResponse(decision) {
    if (stdinClosed || !child.stdin || child.stdin.destroyed) return false;
    const line = formatPermissionResponse(decision);
    try {
      child.stdin.write(line + '\n');
      if (decision && decision.request_id != null) {
        pendingPermissionIds.delete(String(decision.request_id));
      }
      return true;
    } catch {
      return false;
    }
  }

  function closeStdin() {
    if (stdinClosed || !child.stdin) return;
    stdinClosed = true;
    try { child.stdin.end(); } catch {}
  }

  // ---- streaming partial truncation tracking ----
  // max_reply_chars is applied to the assembled text body in non-streaming
  // mode and to the cumulative partial length in streaming mode.  Without
  // this second check, streaming mode silently bypasses the cap.  The two
  // paths now agree: once the combined partial text exceeds max_reply_chars,
  // we emit a truncation notice and drop further partials.
  let cumulativePartialChars = 0;
  let partialsTruncated = false;
  const maxChars = profile.max_reply_chars;
  const truncSuffix = profile.truncation_suffix;

  // ---- stdout: line-by-line event parsing ----
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
      if (!isValidSessionId(c.value)) {
        if (options.onStderr) {
          options.onStderr(`[agentproc runner] ignoring invalid session id ${JSON.stringify(c.value)} (must be non-empty, no path separators or control chars); previous session id preserved`);
        }
        if (options.onProtocolLine) options.onProtocolLine(line);
      } else {
        result.sessionId = c.value; // last wins
        if (options.onSession) options.onSession(c.value);
        if (options.onProtocolLine) options.onProtocolLine(line);
      }
    } else if (c.kind === 'partial') {
      // Spec 5.4: post-error partials are discarded (not forwarded, not
      // appended). `onProtocolLine` still fires so debug traces stay complete.
      if (!errorSeen && streaming && options.onPartial && !partialsTruncated) {
        const remaining = maxChars - cumulativePartialChars;
        if (c.value.length >= remaining) {
          // Emit whatever fits, then the truncation notice, then stop.
          if (remaining > 0) options.onPartial(c.value.slice(0, remaining), c.role);
          if (truncSuffix) options.onPartial(truncSuffix);
          partialsTruncated = true;
        } else {
          options.onPartial(c.value, c.role);
          cumulativePartialChars += c.value.length;
        }
      }
      if (options.onProtocolLine) options.onProtocolLine(line);
    } else if (c.kind === 'text') {
      // Spec 5.4: post-error text is discarded.
      if (!errorSeen) textChunks.push(c.value);
      if (options.onProtocolLine) options.onProtocolLine(line);
    } else if (c.kind === 'error') {
      result.error = c.value;
      errorSeen = true;
      if (options.onError) options.onError(c.value);
      if (options.onProtocolLine) options.onProtocolLine(line);
      // Pending permission requests become moot; stop waiting for UI approval.
      pendingPermissionIds.clear();
    } else if (c.kind === 'permission_request') {
      if (!profile.permission) {
        if (options.onStderr) {
          options.onStderr(
            '[agentproc runner] ignoring {"type":"permission_request"} (profile.permission is not true)'
          );
        }
        if (options.onProtocolLine) options.onProtocolLine(line);
      } else if (!isValidPermissionRequest(c.value)) {
        if (options.onStderr) {
          options.onStderr(
            `[agentproc runner] malformed permission_request: ${JSON.stringify(line.slice(0, 200))}`
          );
        }
        // Spec: if we can still parse a request_id, SHOULD deny; else ignore.
        const rid = c.value && typeof c.value.request_id === 'string' ? c.value.request_id.trim() : '';
        if (rid && !/[\s\r\n\x00-\x1f]/.test(rid)) {
          writePermissionResponse({
            request_id: rid,
            behavior: 'deny',
            message: 'malformed permission request',
          });
        }
        if (options.onProtocolLine) options.onProtocolLine(line);
      } else {
        const req = c.value;
        pendingPermissionIds.add(req.request_id);
        if (options.onProtocolLine) options.onProtocolLine(line);
        if (typeof options.onPermission === 'function') {
          Promise.resolve()
            .then(() => options.onPermission(req))
            .then(decision => {
              if (!decision || typeof decision !== 'object') return;
              // Spec: when the bridge omits updated_input, the response MUST
              // omit it too — the agent (or wrapped CLI) is responsible for
              // falling back to the request's original input. Don't auto-fill
              // req.input here: that would erase the distinction between
              // "user accepted unchanged" and "user never touched it" for
              // downstream CLIs (e.g. Claude Code's updatedInput semantics).
              const out = {
                request_id: req.request_id,
                behavior: decision.behavior === 'allow' ? 'allow' : 'deny',
                message: decision.message,
              };
              if (decision.updated_input !== undefined) {
                out.updated_input = decision.updated_input;
              } else if (decision.updatedInput !== undefined) {
                out.updated_input = decision.updatedInput;
              }
              writePermissionResponse(out);
            })
            .catch(err => {
              if (options.onStderr) {
                options.onStderr(
                  `[agentproc runner] onPermission failed: ${err && err.message ? err.message : err}`
                );
              }
              writePermissionResponse({
                request_id: req.request_id,
                behavior: 'deny',
                message: 'permission handler error',
              });
            });
        }
        // No onPermission: leave the agent blocked until turn timeout (spec).
      }
    } else {
      // malformed: log + ignore (not forwarded as body in 0.3).
      if (options.onStderr) {
        options.onStderr(`[agentproc runner] ignoring malformed stdout line: ${JSON.stringify(line.slice(0, 200))}`);
      }
      if (options.onProtocolLine) options.onProtocolLine(line);
    }
  }

  // ---- stderr: forward as debug ----
  let stderrBuf = '';
  // Two views on stderr:
  //   - stderrWindow: bounded sliding window (8 KB) — reserved for future
  //     UI/display use so a noisy agent cannot exhaust memory.
  //   - stderrFull:   bounded head capture (1 MB) used for post-mortem pattern
  //     diagnosis. The diagnostic patterns target interpreter-startup errors
  //     (file/module not found) which appear in the first bytes of stderr, so
  //     a head cap preserves the high-value signal without unbounded growth.
  //     Beyond the cap the tail is dropped with a one-shot marker.
  let stderrWindow = '';
  let stderrFull = '';
  let stderrFullTruncated = false;
  const STDERR_CAP = 8192;
  const STDERR_FULL_CAP = 1 << 20; // 1 MB
  child.stderr.on('data', chunk => {
    const text = chunk.toString();
    stderrBuf += text;
    stderrWindow += text;
    if (stderrWindow.length > STDERR_CAP) {
      stderrWindow = stderrWindow.slice(stderrWindow.length - STDERR_CAP);
    }
    if (stderrFull.length < STDERR_FULL_CAP) {
      const room = STDERR_FULL_CAP - stderrFull.length;
      stderrFull += text.slice(0, room);
    } else if (!stderrFullTruncated) {
      stderrFull += '\n[agentproc runner] stderr capped at 1 MB; trailing output dropped\n';
      stderrFullTruncated = true;
    }
    let nl;
    while ((nl = stderrBuf.indexOf('\n')) >= 0) {
      const line = stderrBuf.slice(0, nl);
      stderrBuf = stderrBuf.slice(nl + 1);
      if (options.onStderr) options.onStderr(line.replace(/\r$/, ''));
    }
  });

  // ---- stdin: write the turn line; keep open only when permission is on ----
  try { child.stdin.write(JSON.stringify(turn) + '\n'); } catch {}
  if (!profile.permission) {
    closeStdin();
  }

  // ---- timeout handling per spec: SIGTERM → grace → SIGKILL ----
  // On POSIX, child.kill('SIGTERM') is a real signal the agent can trap and
  // flush; on Windows, Node translates any signal name to TerminateProcess,
  // so the grace period is effectively a no-op there. The two-step shape is
  // preserved so POSIX behaviour is correct; Windows callers get a hard kill
  // at the deadline (acceptable per the spec's Windows caveat).
  //
  // killTimer is the SIGKILL follow-up fired after the grace period. It is
  // declared here (outer scope) so the close-handler below can clearTimeout
  // it when the process exits on its own during the grace period — without
  // this, the timer fires against an already-dead process. The try/catch in
  // the callback prevents a crash but the timer itself is a wasted resource
  // and leaves a dangling handle that can prevent Node from exiting cleanly
  // in test/script contexts.
  let timer = null;
  let killTimer = null;
  if (timeoutSecs > 0) {
    timer = setTimeout(() => {
      killed = true;
      result.timedOut = true;
      // Spec: when timing out with a pending permission request, prefer deny
      // with a timeout message if stdin is still writable, then kill.
      if (pendingPermissionIds.size > 0) {
        for (const rid of [...pendingPermissionIds]) {
          writePermissionResponse({
            request_id: rid,
            behavior: 'deny',
            message: 'permission timed out',
          });
        }
      }
      closeStdin();
      try { child.kill('SIGTERM'); } catch {}
      killTimer = setTimeout(() => {
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
      // spawn error — usually ENOENT. Node attributes it to argv[0]
      // regardless of whether it was the command or a referenced file that
      // wasn't found, so disambiguate for the user.
      const tip = diagnoseSpawnError(err, { argv, cwd, env });
      if (options.onStderr) {
        options.onStderr(`[agentproc runner] spawn error: ${err.message}`);
        if (tip) options.onStderr(`[agentproc runner] hint: ${tip}`);
      }
      // Surface as an error event so the user sees it on the bridge too.
      if (options.onError) {
        const msg = tip || err.message;
        options.onError(`failed to start agent: ${msg}`);
      }
      if (!result.error) result.error = tip || err.message;
      resolve(EXIT_ERROR);
    });
  });

  if (timer) clearTimeout(timer);
  if (killTimer) clearTimeout(killTimer);
  closeStdin();

  // Flush any remaining stdout buffer as a final line (without trailing \n).
  if (stdoutBuf.length > 0) {
    handleLine(stdoutBuf.replace(/\r$/, ''));
  }

  // Flush any remaining stderr (the chunk handler only emits on newlines).
  if (stderrBuf.length > 0) {
    if (options.onStderr) options.onStderr(stderrBuf.replace(/\r$/, ''));
  }

  // If the agent exited non-zero with no error event, peek at its stderr for
  // common "command/file not found" patterns and surface a friendly hint.
  // Uses the FULL stderr — a noisy agent can fill the 8 KB window with
  // progress junk before the real error lands at the end.
  if (!killed && !result.error && exitCode !== 0) {
    const hint = diagnoseStderrFailure(stderrFull);
    if (hint) {
      result.error = hint;
      if (options.onError) options.onError(hint);
    }
  }

  // Reply body = concatenation of {"type":"text"} events (direct, no separator).
  result.reply = textChunks.join('');
  if (result.reply.length > profile.max_reply_chars) {
    result.reply = result.reply.slice(0, profile.max_reply_chars) + profile.truncation_suffix;
  }

  // Exit code per spec.
  if (killed) {
    result.exitCode = EXIT_TIMEOUT;
  } else if (result.error) {
    // An error event was emitted — treat as failure even if exit was 0.
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
  parseJsonLine,
  isValidSessionId,
  formatPermissionResponse,
  isValidPermissionRequest,
  substitute,
  expandEnvRef,
  expandPath,
  PROTOCOL_VERSION,
  DEFAULT_TIMEOUT_SECS,
  DEFAULT_KILL_GRACE_SECS,
  DEFAULT_MAX_REPLY_CHARS,
  DEFAULT_TRUNCATION_SUFFIX,
  EXIT_SUCCESS,
  EXIT_ERROR,
  EXIT_TIMEOUT,
  EXIT_SIGINT,
  EXIT_SIGTERM,
  ENV_INFRA_VARS,
  buildBaseEnv,
  STDERR_DIAGNOSTICS,
  diagnoseStderrFailure,
};
