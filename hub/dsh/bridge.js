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
 * Plain-text semantics: no streaming, no session resume (headless mints a
 * fresh persisted Agent per run), so this is a stateless agent on the wire —
 * no session_id is stamped on events.
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
const { spawn } = require('node:child_process');
const {
  readTurn,
  emitResult,
  emitError,
} = require(path.join(__dirname, '..', '_shared', 'stream_utils.js'));

const CLI_NAME = 'dsh';
const INSTALL_HINT = 'Install: npm install -g @deepseek-ai/dsh';
const DEFAULT_TIMEOUT_SECS = 1800;
const KILL_GRACE_SECS = 5;

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

function buildArgs(task) {
  return [CLI_NAME, '--profile', 'headless', task];
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
  const hasAtt = Array.isArray(turn.attachments) && turn.attachments.length > 0;
  if (!message && !hasAtt) {
    emitError('turn.message is required (or include turn.attachments)');
    process.exit(1);
  }

  const args = buildArgs(composeTask(message, turn));
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

  if (spawnError) {
    const notFound = spawnError.code === 'ENOENT';
    const msg = notFound
      ? `${CLI_NAME} CLI not found. ${INSTALL_HINT}`
      : spawnError.message;
    emitError(msg);
    process.exit(1);
  }
  if (timedOut) {
    emitError(`${CLI_NAME} timed out after ${timeoutSecs}s`);
    process.exit(124);
  }
  if (code !== 0) {
    // dsh headless reports errors on stderr with the exit code; prefer that
    // text ("dsh: MISSING_CREDENTIAL: ...") for a actionable message.
    const s = stderr.trim();
    let msg = `${CLI_NAME} exited with ${code}`;
    if (s) msg += `: ${s.slice(0, 500)}`;
    emitError(msg);
    process.exit(1);
  }

  const text = stdout.trim();
  if (!text) {
    emitError(`${CLI_NAME} returned empty output (task completed with no assistant message)`);
    process.exit(1);
  }
  emitResult(text);
  process.exit(0);
}

main().catch((e) => {
  process.stderr.write(`[dsh bridge] unhandled error: ${e && (e.stack || e)}\n`);
  process.exit(1);
});
