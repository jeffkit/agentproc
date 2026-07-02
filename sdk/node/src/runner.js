'use strict';
/**
 * AgentProc runner — the core engine that turns a profile + message into a
 * protocol-compliant agent invocation.
 *
 * This module is the canonical implementation of the AgentProc bridge-side
 * contract (spec/protocol.md, wire protocol 0.1). The CLI (cli.js) is a thin wrapper around it.
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
const fs = require('node:fs');
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
// Environment inheritance policy
// ---------------------------------------------------------------------------

// When a profile declares `env_allowlist`, the child process does NOT inherit
// the bridge's environment wholesale — doing so would let any secret the
// bridge happens to hold (cloud tokens, API keys) reach the agent even though
// the profile never declared it, making `env_allowlist` a cosmetic filter
// rather than a trust boundary. Instead the child env is built from:
//   (1) this minimal INFRA set (copied from process.env when present) so the
//       agent can still find its interpreter / temp dir / locale, ...
//   (2) the profile `env` block (with ${VAR} expanded, allowlist-filtered),
//   (3) the AGENT_* vars the bridge injects,
//   (4) extraEnv from the CLI --env flag.
// Secrets not declared by the profile never reach the agent. When
// `env_allowlist` is absent, the bridge inherits process.env wholesale
// (back-compat) — the trust-the-profile behaviour.
const ENV_INFRA_VARS = [
  'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE',
  'LC_MESSAGES', 'TERM', 'TMPDIR', 'TZ', 'PWD',
  // Windows infra
  'SystemRoot', 'TEMP', 'TMP', 'USERPROFILE', 'USERNAME', 'PATHEXT',
  'COMSPEC', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'NUMBER_OF_PROCESSORS',
  'PROCESSOR_ARCHITECTURE', 'OS',
];

function buildBaseEnv(allowlist) {
  if (allowlist === null) return { ...process.env };
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
  const p = raw.agentproc ? { ...raw.agentproc } : { ...raw };

  if (typeof p.command !== 'string' || p.command.trim() === '') {
    throw new Error('profile.command must be a non-empty string');
  }

  // Per spec: `command` is argv[0]; `args` is argv[1..]. Two mutually
  // exclusive forms:
  //   (a) `args` absent + command has whitespace → split command into argv
  //       (the legacy shorthand: `command: python3 ./bridge.py`)
  //   (b) `args` present (even empty `[]`) → command is a single token,
  //       never split. Lets paths with spaces stay whole:
  //         command: "/path with spaces/my agent"
  //         args: []
  // `args: []` (explicit empty array) is DISTINCT from "args absent": the
  // explicit form means "do not split command"; the absent form falls back
  // to the whitespace-splitting shorthand.
  const argsFieldPresent = raw.agentproc
    ? (Object.prototype.hasOwnProperty.call(raw.agentproc, 'args') && raw.agentproc.args != null)
    : (Object.prototype.hasOwnProperty.call(raw, 'args') && raw.args != null);
  const argv = argsFieldPresent ? [p.command.trim()] : p.command.trim().split(/\s+/);
  if (argv.length === 0 || argv[0] === '') {
    throw new Error('profile.command produced empty argv');
  }

  // env_allowlist (optional): when present, ${VAR} references in the env
  // block whose name is NOT in the list expand to empty + a stderr warning.
  // Absent ⇒ current behaviour (expand against the full bridge environment).
  // Opt-in: existing profiles keep working unchanged.
  let envAllowlist = null;
  if (p.env_allowlist !== undefined && p.env_allowlist !== null) {
    if (!Array.isArray(p.env_allowlist)) {
      throw new Error('profile.env_allowlist must be a list');
    }
    envAllowlist = new Set(p.env_allowlist.map(String));
  }

  return {
    argv,
    args: Array.isArray(p.args) ? p.args.map(String) : [],
    cwd: p.cwd ? expandPath(String(p.cwd)) : undefined,
    env: p.env && typeof p.env === 'object' ? p.env : {},
    env_allowlist: envAllowlist,
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
 * Best-effort pattern check against the agent's accumulated stderr to spot
 * common "bridge file not found" / "module not found" failures that the
 * wrapped interpreter writes to its own stderr before exiting non-zero.
 * Returns a human-friendly hint, or '' if nothing recognizable.
 *
 * This is intentionally narrow — we only flag high-confidence patterns to
 * avoid mis-diagnosing genuine agent errors.
 */
function diagnoseStderrFailure(stderrText) {
  if (!stderrText) return '';
  const lower = stderrText.toLowerCase();

  // python3: "can't open file '/path/x.py': [Errno 2] No such file or directory"
  // Also covers "cannot open file" (localized variants).
  const pyMatch = stderrText.match(/(?:can'?t|cannot) open file '([^']+)': \[Errno 2\] No such file or directory/);
  if (pyMatch) {
    const file = pyMatch[1];
    return `agent script not found: ${file}. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).`;
  }

  // node: "Error: Cannot find module '/path/x.js'"
  const nodeMatch = stderrText.match(/Cannot find module '([^']+)'/);
  if (nodeMatch) {
    const mod = nodeMatch[1];
    return `agent script not found: ${mod}. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).`;
  }

  // bash: "bash: line N: ./x.sh: No such file or directory"
  const bashMatch = stderrText.match(/(?:^|\n)[^:]+: line \d+: ([^:]+): No such file or directory/);
  if (bashMatch) {
    const file = bashMatch[1];
    return `agent script not found: ${file}. Check the profile's command path.`;
  }

  // Generic Errno 2 sentinel, in case the interpreter phrasing differs.
  if (/errno 2|enoent|no such file or directory/.test(lower)) {
    return `agent reported a missing file. Check the profile's command and cwd.`;
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
// Line classification (per spec)
// ---------------------------------------------------------------------------

/**
 * Try to JSON-decode a value after a prefix.
 * Lenient mode (default per spec): on failure, return the raw text.
 */
function decodeJsonValue(raw) {
  const text = raw.trim();
  if (text === '') return '';
  let v;
  try {
    v = JSON.parse(text);
  } catch {
    // Lenient: treat as plain string.
    return text;
  }
  // Only JSON strings are meaningful payloads — a sentinel's value is text
  // for the user. Non-string JSON (number/bool/null/array/object) means the
  // agent misused the API; fall back to the raw text so the result is
  // language-independent (String(true) != str(True) across runtimes).
  return typeof v === 'string' ? v : text;
}

// Per spec: session id is opaque but MUST NOT contain whitespace, control
// characters, or colons. Valid: base64url alphabet (A-Z a-z 0-9 - _) plus
// . ~ = ; non-empty. `/` and `+` are deliberately excluded — `/` makes the id
// unsafe as a filename component (the SDK history helpers store <id>.jsonl,
// and a `/`-bearing id would path-traverse). A session line whose value
// fails this is ignored (previous id preserved).
const SESSION_ID_RE = /^[A-Za-z0-9._~=-]+$/;
function isValidSessionId(value) {
  return typeof value === 'string' && value.length > 0 && SESSION_ID_RE.test(value);
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
  // {{PROFILE_DIR}} resolves to the directory the profile YAML lives in
  // (passed by the CLI; undefined when run programmatically without it),
  // letting profiles reference bundled scripts via absolute paths while
  // still allowing the agent's cwd to be anywhere.
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

  // Build env per the inheritance policy (see buildBaseEnv). When an
  // allowlist is set, the child does NOT inherit process.env wholesale — only
  // the infra vars + profile.env + AGENT_* + extraEnv. This makes env_allowlist
  // a real trust boundary instead of a cosmetic ${VAR} filter.
  const allowlist = profile.env_allowlist;
  const env = buildBaseEnv(allowlist);
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

  // Inject AGENT_* vars per spec.
  env.AGENT_MESSAGE = options.message;
  env.AGENT_SESSION_ID = sessionId;
  env.AGENT_SESSION_NAME = sessionName;
  env.AGENT_FROM_USER = options.fromUser || '';
  env.AGENT_STREAMING = streaming ? '1' : '0';
  env.AGENT_PROTOCOL_VERSION = PROTOCOL_VERSION;
  // Single-attachment passthrough. Only inject when non-empty so an unset
  // variable stays unset (spec: "set when present"); an agent can tell
  // "no image" apart from "image URL is the empty string".
  if (options.imageUrl) env.AGENT_IMAGE_URL = options.imageUrl;
  if (options.fileUrl) env.AGENT_FILE_URL = options.fileUrl;

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
      if (!isValidSessionId(c.value)) {
        if (options.onStderr) {
          options.onStderr(`[agentproc runner] ignoring invalid AGENT_SESSION value ${JSON.stringify(c.value)} (must be non-empty, no whitespace/control chars/colons); previous session id preserved`);
        }
        if (options.onProtocolLine) options.onProtocolLine(line);
      } else {
        result.sessionId = c.value; // last wins
        if (options.onSession) options.onSession(c.value);
        if (options.onProtocolLine) options.onProtocolLine(line);
      }
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
  // Two views on stderr:
  //   - stderrWindow: bounded sliding window (8 KB) — reserved for future
  //     UI/display use so a noisy agent cannot exhaust memory.
  //   - stderrFull:   unbounded capture used for post-mortem pattern
  //     diagnosis. Without the full text, a chatty agent can push the real
  //     error out of the window and the friendly hint goes missing.
  let stderrWindow = '';
  let stderrFull = '';
  const STDERR_CAP = 8192;
  child.stderr.on('data', chunk => {
    const text = chunk.toString();
    stderrBuf += text;
    stderrFull += text;
    stderrWindow += text;
    if (stderrWindow.length > STDERR_CAP) {
      stderrWindow = stderrWindow.slice(stderrWindow.length - STDERR_CAP);
    }
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
  // On POSIX, child.kill('SIGTERM') is a real signal the agent can trap and
  // flush; on Windows, Node translates any signal name to TerminateProcess,
  // so the grace period is effectively a no-op there. The two-step shape is
  // preserved so POSIX behaviour is correct; Windows callers get a hard kill
  // at the deadline (acceptable per the spec's Windows caveat).
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
      // spawn error — usually ENOENT. Node attributes it to argv[0]
      // regardless of whether it was the command or a referenced file that
      // wasn't found, so disambiguate for the user.
      const tip = diagnoseSpawnError(err, { argv, cwd, env });
      if (options.onStderr) {
        options.onStderr(`[agentproc runner] spawn error: ${err.message}`);
        if (tip) options.onStderr(`[agentproc runner] hint: ${tip}`);
      }
      // Surface as an AGENT_ERROR so the user sees it on the bridge too.
      if (options.onError) {
        const msg = tip || err.message;
        options.onError(`failed to start agent: ${msg}`);
      }
      if (!result.error) result.error = tip || err.message;
      resolve(EXIT_ERROR);
    });
  });

  if (timer) clearTimeout(timer);

  // Flush any remaining stdout buffer as a final line (without trailing \n).
  if (stdoutBuf.length > 0) {
    handleLine(stdoutBuf.replace(/\r$/, ''));
  }

  // Flush any remaining stderr (the chunk handler only emits on newlines).
  if (stderrBuf.length > 0) {
    if (options.onStderr) options.onStderr(stderrBuf.replace(/\r$/, ''));
  }

  // If the agent exited non-zero with no AGENT_ERROR, peek at its stderr for
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
  isValidSessionId,
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
  ENV_INFRA_VARS,
  buildBaseEnv,
};
