#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the DeepSeek Harness CLI `dsh` (wire 0.4).
 *
 *   dsh --profile headless "<task>"
 *
 * dsh headless is a one-shot, full agent runtime: it boots the headless
 * bundle (coding persona + bash/fs/search tools + sandbox), runs the task to
 * quiescence, and prints the last non-empty assistant message to stdout.
 * Errors are written to stderr ("dsh: CODE: message") with a non-zero exit.
 *
 * Session continuity is feature-detected: when the installed dsh supports
 * `--resume <id>` and `--print-session-id` (upstream PR pending; tracked in
 * the README's upgrade-hook note), the bridge stamps `session_id` on events
 * and resumes the persisted session on later turns — true multi-turn for
 * process bridges. Older dsh builds stay stateless on the wire (no id
 * stamped), exactly as before.
 *
 * Per-CLI config (read from the process env the runner injects):
 *   DEEPSEEK_API_KEY    API key passthrough (alternative: store it once via
 *                       the web UI's Models page — dsh reads stored
 *                       credentials too; an empty env var falls through)
 *   DSH_PERMISSION_MODE danger-full-access (bridge default, auto-approve) |
 *                       workspace-write | read-only
 *   DSH_TOOLS_MODE      optional dsh Code Mode opt-in passthrough
 *   DSH_TIMEOUT         process timeout in seconds (default: 1800)
 */

const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const {
  readTurn,
  emitResult,
  emitError,
} = require(path.join(__dirname, '..', '_shared', 'stream_utils.js'));

const CLI_NAME = 'dsh';
const INSTALL_HINT = 'Install: npm install -g @deepseek-ai/dsh';
const DEFAULT_TIMEOUT_SECS = 1800;
const KILL_GRACE_SECS = 5;
/** stderr line through which a resume-capable dsh reports the session id. */
const SESSION_ID_LINE = /^dsh: session-id: (\S+)\s*$/;
/**
 * Wire rules for session_id: non-empty, no path separators or control
 * characters (the runner drops non-conforming values, so fail soft here).
 */
const SESSION_ID_VALID = /^[^\s/\\\x00-\x1f]+$/;

/** Compose the dsh task text: message plus attachments as reference URLs. */
function composeTask(message, turn) {
  const atts = Array.isArray(turn.attachments) ? turn.attachments : [];
  if (atts.length === 0) return message;
  const lines = atts.map((a, i) => {
    if (!a || typeof a !== 'object') return `- [unknown] attachment-${i + 1}`;
    const kind = typeof a.kind === 'string' && a.kind ? a.kind : 'file';
    const name = typeof a.filename === 'string' && a.filename ? a.filename : `attachment-${i + 1}`;
    const url = typeof a.url === 'string' ? a.url : '';
    return `- [${kind}] ${name}${url ? `: ${url}` : ''}`;
  });
  const base = message || 'Please look at the following attachments.';
  return `${base}\n\nAttachments (referenced by URL):\n${lines.join('\n')}`;
}

/**
 * Build the dsh argv. With continuity support, always request the id print
 * and resume a known session; otherwise stay stateless.
 * @param {string} task
 * @param {string} sessionId - inbound turn session id ('' = new session)
 * @param {boolean} supportsResume
 */
function buildArgs(task, sessionId, supportsResume) {
  const args = [CLI_NAME, '--profile', 'headless'];
  if (supportsResume) {
    args.push('--print-session-id');
    if (sessionId) args.push('--resume', sessionId);
  }
  args.push(task);
  return args;
}

/**
 * Extract the session id a resume-capable dsh printed on stderr.
 * @returns {string} the id, or '' when absent or wire-invalid.
 */
function parseSessionId(stderr) {
  for (const line of String(stderr).split('\n')) {
    const m = line.match(SESSION_ID_LINE);
    if (m && SESSION_ID_VALID.test(m[1])) return m[1];
  }
  return '';
}

/**
 * Probe (once per process) whether this dsh exposes session continuity by
 * reading the headless app's own --help. A failed probe means "unsupported".
 */
let resumeSupport = null;
function probeResumeSupport() {
  if (resumeSupport !== null) return resumeSupport;
  try {
    const r = spawnSync(CLI_NAME, ['--profile', 'headless', '--help'], {
      encoding: 'utf8',
      timeout: 30_000,
    });
    const help = `${r.stdout || ''}${r.stderr || ''}`;
    resumeSupport = help.includes('--resume') && help.includes('--print-session-id');
  } catch {
    resumeSupport = false;
  }
  return resumeSupport;
}

/**
 * Child environment. Unattended default: auto-approve tools — the same
 * posture as the claude-code profile's --dangerously-skip-permissions
 * default. dsh's own default is "ask", which has no UI to answer it in
 * headless mode, so we only leave it when the operator explicitly set a
 * stricter mode (workspace-write / read-only lean on the sandbox instead).
 *
 * The runner expands unset `${VAR}` references in the profile env block to
 * empty strings, and dsh validates several of these as real values (e.g.
 * DSH_TOOLS_MODE "" fails schema validation with "expected native|code|both")
 * — so drop empties and let dsh defaults win.
 */
function childEnv() {
  const env = { ...process.env };
  for (const key of ['DEEPSEEK_API_KEY', 'DSH_PERMISSION_MODE', 'DSH_TOOLS_MODE']) {
    if (env[key] === '') delete env[key];
  }
  if (!env.DSH_PERMISSION_MODE) env.DSH_PERMISSION_MODE = 'danger-full-access';
  return env;
}

async function main() {
  const turn = await readTurn();
  const message = typeof turn.message === 'string' ? turn.message : '';
  const inboundSession = typeof turn.session_id === 'string' ? turn.session_id : '';
  const hasAtt = Array.isArray(turn.attachments) && turn.attachments.length > 0;
  if (!message && !hasAtt) {
    emitError('turn.message is required (or include turn.attachments)');
    process.exit(1);
  }

  const supportsResume = probeResumeSupport();
  const args = buildArgs(composeTask(message, turn), inboundSession, supportsResume);
  let child;
  try {
    child = spawn(args[0], args.slice(1), {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: childEnv(),
    });
  } catch {
    emitError(`${CLI_NAME} CLI not found. ${INSTALL_HINT}`);
    process.exit(1);
  }

  let stdout = '';
  let stderr = '';
  let spawnError = null;
  let timedOut = false;
  child.on('error', (err) => { spawnError = err; });
  child.stdout.on('data', (d) => { stdout += d.toString(); });
  child.stderr.on('data', (d) => { stderr += d.toString(); });

  const timeoutSecs = parseInt(process.env.DSH_TIMEOUT || String(DEFAULT_TIMEOUT_SECS), 10);
  let killer = null;
  const timer = setTimeout(() => {
    timedOut = true;
    child.kill('SIGTERM');
    killer = setTimeout(() => child.kill('SIGKILL'), KILL_GRACE_SECS * 1000);
  }, timeoutSecs * 1000);

  const code = await new Promise((resolve) => child.on('close', resolve));
  clearTimeout(timer);
  if (killer) clearTimeout(killer);

  // The id line is bridge bookkeeping, not a user-facing error; stamp it on
  // every outcome (error turns included) so continuity survives failures.
  const sessionId = supportsResume ? parseSessionId(stderr) : '';

  if (spawnError) {
    const notFound = spawnError.code === 'ENOENT';
    const msg = notFound
      ? `${CLI_NAME} CLI not found. ${INSTALL_HINT}`
      : spawnError.message;
    emitError(msg, sessionId);
    process.exit(1);
  }
  if (timedOut) {
    emitError(`${CLI_NAME} timed out after ${timeoutSecs}s`, sessionId);
    process.exit(124);
  }
  if (code !== 0) {
    // dsh headless reports errors on stderr with the exit code; prefer that
    // text ("dsh: MISSING_CREDENTIAL: ...") for an actionable message.
    const s = stderr.split('\n').filter((line) => !SESSION_ID_LINE.test(line)).join('\n').trim();
    let msg = `${CLI_NAME} exited with ${code}`;
    if (s) msg += `: ${s.slice(0, 500)}`;
    emitError(msg, sessionId);
    process.exit(1);
  }

  const text = stdout.trim();
  if (!text) {
    emitError(`${CLI_NAME} returned empty output (task completed with no assistant message)`, sessionId);
    process.exit(1);
  }
  emitResult(text, sessionId);
  process.exit(0);
}

if (require.main === module) {
  main().catch((e) => {
    process.stderr.write(`[dsh bridge] unhandled error: ${e && (e.stack || e)}\n`);
    process.exit(1);
  });
}

module.exports = { composeTask, buildArgs, parseSessionId, probeResumeSupport, childEnv };
