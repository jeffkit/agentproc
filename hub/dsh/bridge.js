#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the DeepSeek Harness CLI `dsh` (wire 0.4).
 *
 *   dsh --profile headless --json "<task>"
 *
 * dsh headless is a one-shot, full agent runtime: it boots the headless
 * bundle (coding persona + bash/fs/search tools + sandbox), runs the task to
 * quiescence, flushes the Session, and reports the answer.
 *
 * JSON mode (dsh >= 0.1.6-alpha.1, feature-detected via `--profile headless
 * --help`) reads newline-delimited run events from stdout:
 *
 *   {type:"session",sessionId,cwd}   opening frame — the session id source
 *   {type:"status",phase:"step_end",usage:{inputTokens,...}}
 *                                    per-step token counts in DISJOINT
 *                                    buckets (uncached input only; billed
 *                                    input = input + cacheRead + cacheWrite).
 *                                    The bridge sums steps into a turn total.
 *   {type:"text"|"thinking",text}    committed assistant content
 *   {type:"final",text}              terminal answer — always written, even
 *                                    when the turn ends in error (exit 1)
 *   {type:"error",message}           driver failure — no final follows
 *
 * The exit code separates completed (0) from error-ended (1) turns, but a
 * SIGTERM'd dsh also exits 0 (launcher supervisor semantics), so the bridge
 * trusts frames, not the exit code: a run counts as successful only when a
 * `final` frame arrived and the exit code was 0.
 *
 * Session continuity (JSON mode with `--session-id` advertised): the bridge
 * stamps `session_id` from the opening frame and resumes the persisted
 * Session on later turns. Adoption is strict upstream — same cwd, no
 * subagent/fork, no agent preset — and a mismatch surfaces as an error
 * event ("session … was recorded in …").
 *
 * Plain fallback (older dsh): the final assistant message is plain stdout;
 * errors go to stderr ("dsh: CODE: message") with a non-zero exit. Stateless
 * on the wire.
 *
 * Usage mapping: dsh buckets are disjoint, agentproc's `input_tokens` is the
 * inclusive billed input, so the bridge adds cacheRead/cacheWrite into it
 * and passes the buckets through under their agentproc names.
 *
 * `thinking` frames stay off the wire (reasoning projection, same posture as
 * the claude-code profile, which forwards text deltas only).
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
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const {
  readTurn,
  emitPartial,
  emitResult,
  emitError,
} = require(path.join(__dirname, '..', '_shared', 'stream_utils.js'));

const CLI_NAME = 'dsh';
const INSTALL_HINT = 'Install: npm install -g @deepseek-ai/dsh';
const DEFAULT_TIMEOUT_SECS = 1800;
const KILL_GRACE_SECS = 5;
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
 * Read feature support off the headless app's own --help text.
 * `--json` alone upgrades the transport; `--session-id` additionally gates
 * session continuity.
 * @param {string} help
 * @returns {{jsonMode: boolean, sessionResume: boolean}}
 */
function detectSupport(help) {
  const text = String(help || '');
  const jsonMode = text.includes('--json');
  return { jsonMode, sessionResume: jsonMode && text.includes('--session-id') };
}

/**
 * Build the dsh argv. JSON mode always requests the event stream (and `--`
 * so a task starting with `-` stays a positional); with session support and
 * an inbound id, adopt that Session.
 * @param {string} task
 * @param {string} sessionId - inbound turn session id ('' = new session)
 * @param {{jsonMode: boolean, sessionResume: boolean}} support
 */
function buildArgs(task, sessionId, support) {
  const args = [CLI_NAME, '--profile', 'headless'];
  if (support.jsonMode) {
    args.push('--json');
    if (support.sessionResume && sessionId) args.push('--session-id', sessionId);
    args.push('--', task);
  } else {
    args.push(task);
  }
  return args;
}

/**
 * Sum one step_end frame's usage buckets into the turn accumulator. Buckets
 * stay disjoint per step (they are summed across attempts inside dsh); an
 * optional bucket is kept only while every step reports it.
 * @param {object|null} total - accumulator so far (null = none yet)
 * @param {object} usage - one frame's {inputTokens,outputTokens,...}
 */
function addStep(total, usage) {
  if (!usage || typeof usage !== 'object') return total || null;
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined);
  const input = num(usage.inputTokens);
  const output = num(usage.outputTokens);
  if (input === undefined && output === undefined) return total || null;
  if (!total) {
    // First reported step: adopt its buckets wholesale (dsh's addUsage
    // semantics — there is nothing to sum into yet).
    return {
      inputTokens: input || 0,
      outputTokens: output || 0,
      totalTokens: num(usage.totalTokens),
      cacheReadTokens: num(usage.cacheReadTokens),
      cacheWriteTokens: num(usage.cacheWriteTokens),
      reasoningTokens: num(usage.reasoningTokens),
    };
  }
  const sum = (a, b) => (a === undefined || b === undefined ? undefined : a + b);
  return {
    inputTokens: (total.inputTokens || 0) + (input || 0),
    outputTokens: (total.outputTokens || 0) + (output || 0),
    totalTokens: sum(total.totalTokens, num(usage.totalTokens)),
    cacheReadTokens: sum(total.cacheReadTokens, num(usage.cacheReadTokens)),
    cacheWriteTokens: sum(total.cacheWriteTokens, num(usage.cacheWriteTokens)),
    reasoningTokens: sum(total.reasoningTokens, num(usage.reasoningTokens)),
  };
}

/**
 * Map the summed dsh buckets onto agentproc's recommended usage keys.
 * dsh counts input/cache buckets disjointly; agentproc's `input_tokens` is
 * the inclusive billed input, so the cache buckets fold into it.
 * @returns {object|null} null when the accumulator carries no usable counts
 */
function toUsage(u) {
  if (!u || typeof u !== 'object') return null;
  const has = (k) => typeof u[k] === 'number' && Number.isFinite(u[k]);
  const out = {};
  if (has('inputTokens')) {
    out.input_tokens = u.inputTokens
      + (has('cacheReadTokens') ? u.cacheReadTokens : 0)
      + (has('cacheWriteTokens') ? u.cacheWriteTokens : 0);
  }
  if (has('outputTokens')) out.output_tokens = u.outputTokens;
  if (has('totalTokens')) out.total_tokens = u.totalTokens;
  if (has('cacheReadTokens')) out.cache_read_input_tokens = u.cacheReadTokens;
  if (has('cacheWriteTokens')) out.cache_creation_input_tokens = u.cacheWriteTokens;
  if (has('reasoningTokens')) out.reasoning_tokens = u.reasoningTokens;
  return Object.keys(out).length > 0 ? out : null;
}

/**
 * Probe (once per process) what this dsh supports by reading the headless
 * app's own --help. A failed probe means "plain fallback".
 * @returns {{jsonMode: boolean, sessionResume: boolean}}
 */
let supportCache = null;
function probeSupport() {
  if (supportCache !== null) return supportCache;
  try {
    const r = spawnSync(CLI_NAME, ['--profile', 'headless', '--help'], {
      encoding: 'utf8',
      timeout: 30_000,
    });
    supportCache = detectSupport(`${r.stdout || ''}${r.stderr || ''}`);
  } catch {
    supportCache = detectSupport('');
  }
  return supportCache;
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

/** First actionable line of a dsh stderr diagnostic ("dsh: CODE: msg"). */
function stderrHint(stderr) {
  const s = String(stderr || '').trim();
  if (!s) return '';
  return s.replace(/^dsh:\s*/g, '').slice(0, 500);
}

/**
 * JSON mode: translate the run-event stream into AgentProc events. The run
 * is a success only when a `final` frame arrived AND the exit code was 0 —
 * an error-reasoned turn still writes `final`, and a SIGTERM'd dsh exits 0
 * without one.
 */
async function runJson(child, support) {
  const rl = readline.createInterface({ input: child.stdout });
  let stderr = '';
  child.stderr.on('data', (d) => { stderr += d.toString(); });

  let sessionId = '';
  let finalText = null;
  let errorMsg = null;
  let usage = null;

  const pump = (async () => {
    for await (const raw of rl) {
      const line = String(raw).trim();
      if (!line) continue;
      let frame;
      try { frame = JSON.parse(line); } catch { continue; }
      if (!frame || typeof frame !== 'object') continue;
      switch (frame.type) {
        case 'session':
          if (typeof frame.sessionId === 'string' && SESSION_ID_VALID.test(frame.sessionId)) {
            sessionId = frame.sessionId;
          }
          break;
        case 'status':
          if (frame.phase === 'step_end') usage = addStep(usage, frame.usage);
          break;
        case 'text':
          if (typeof frame.text === 'string' && frame.text) emitPartial(frame.text, sessionId);
          break;
        case 'final':
          if (typeof frame.text === 'string') finalText = frame.text;
          break;
        case 'error':
          if (typeof frame.message === 'string' && frame.message) errorMsg = frame.message;
          break;
        default:
          break; // thinking / tool_call / tool_result stay off the wire
      }
    }
  })();
  pump.catch(() => {}); // readline destruction races the close event; frames already seen win

  const code = await new Promise((resolve) => child.on('close', resolve));
  const mapped = toUsage(usage);
  return { code, stderr, sessionId, finalText, errorMsg, usage: mapped };
}

function finishJson({ code, stderr, sessionId, finalText, errorMsg, usage }, timedOut, timeoutSecs) {
  if (timedOut) {
    emitError(`${CLI_NAME} timed out after ${timeoutSecs}s`, sessionId, usage);
    process.exit(124);
  }
  if (errorMsg) {
    emitError(errorMsg, sessionId, usage);
    process.exit(1);
  }
  if (finalText !== null) {
    if (code === 0) {
      emitResult(finalText, sessionId, usage);
      process.exit(0);
    }
    // The turn ended in an error reason after committing text; stderr has
    // the actionable "dsh: CODE: message" diagnostic.
    const hint = stderrHint(stderr);
    const msg = hint ? `${CLI_NAME}: ${hint}` : `${CLI_NAME} turn ended with an error (exit ${code})`;
    emitError(msg, sessionId, usage);
    process.exit(1);
  }
  // No final: killed mid-run (dsh maps SIGTERM to exit 0) or crashed early.
  const hint = stderrHint(stderr);
  let msg = `${CLI_NAME} exited with ${code} without a final message`;
  if (hint) msg += `: ${hint}`;
  emitError(msg, sessionId, usage);
  process.exit(1);
}

/** Plain fallback: stateless one-shot, exactly the pre-0.1.6 behavior. */
function finishPlain({ code, stdout, stderr }, timedOut, timeoutSecs) {
  if (timedOut) {
    emitError(`${CLI_NAME} timed out after ${timeoutSecs}s`);
    process.exit(124);
  }
  if (code !== 0) {
    let msg = `${CLI_NAME} exited with ${code}`;
    const hint = stderrHint(stderr);
    if (hint) msg += `: ${hint}`;
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

async function main() {
  const turn = await readTurn();
  const message = typeof turn.message === 'string' ? turn.message : '';
  const inboundSession = typeof turn.session_id === 'string' ? turn.session_id : '';
  const hasAtt = Array.isArray(turn.attachments) && turn.attachments.length > 0;
  if (!message && !hasAtt) {
    emitError('turn.message is required (or include turn.attachments)');
    process.exit(1);
  }

  const support = probeSupport();
  const args = buildArgs(composeTask(message, turn), inboundSession, support);
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
  child.on('error', (err) => {
    const msg = err && err.code === 'ENOENT'
      ? `${CLI_NAME} CLI not found. ${INSTALL_HINT}`
      : (err && err.message) || 'failed to start';
    emitError(msg);
    process.exit(1);
  });

  const timeoutSecs = parseInt(process.env.DSH_TIMEOUT || String(DEFAULT_TIMEOUT_SECS), 10);
  let timedOut = false;
  let killer = null;
  const timer = setTimeout(() => {
    timedOut = true;
    child.kill('SIGTERM');
    killer = setTimeout(() => child.kill('SIGKILL'), KILL_GRACE_SECS * 1000);
  }, timeoutSecs * 1000);

  let outcome = null;
  if (support.jsonMode) {
    outcome = await runJson(child, support);
  } else {
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });
    const code = await new Promise((resolve) => child.on('close', resolve));
    outcome = { code, stdout, stderr };
  }
  clearTimeout(timer);
  if (killer) clearTimeout(killer);
  if (support.jsonMode) finishJson(outcome, timedOut, timeoutSecs);
  else finishPlain(outcome, timedOut, timeoutSecs);
}

if (require.main === module) {
  main().catch((e) => {
    process.stderr.write(`[dsh bridge] unhandled error: ${e && (e.stack || e)}\n`);
    process.exit(1);
  });
}

module.exports = { composeTask, buildArgs, detectSupport, addStep, toUsage, probeSupport, childEnv };
