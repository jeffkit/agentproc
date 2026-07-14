#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `recursive` CLI (self-improving Rust coding agent,
 * wire 0.4).
 *
 * Parity implementation of hub/recursive/bridge.py. See that file for the
 * full design rationale; this file mirrors it in behaviour.
 *
 *   recursive --json --stream ... run <message>                       // turn 1
 *   recursive --json --stream ... resume --from-file <session-dir> \
 *       -p <message>                                                   // turn N+
 *
 * The bridge always passes `--stream` and always emits {"type":"partial"}
 * events; the runner forwards them only when the profile's streaming is true.
 * A single {"type":"result"} event with the assembled reply is emitted at the
 * end so the reply body is populated regardless of streaming mode.
 */

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const HUB_DIR = path.resolve(__dirname, '..');
const { readTurn } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

const CLI_NAME = 'recursive';
const INSTALL_HINT =
  'Install: cargo install --locked --path .  (then `recursive init` to configure a provider)';

// `session: recording to <abs-dir>` — recursive logs this to stderr when it
// creates a session writer.
const SESSION_RECORDING_RE = /session: recording to (\S+)/;

// ---------------------------------------------------------------------------
// NDJSON emission helpers
// ---------------------------------------------------------------------------

function emitObj(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function emitPartial(text, sessionId) {
  const obj = { type: 'partial', text };
  if (sessionId) obj.session_id = sessionId;
  emitObj(obj);
}

function emitResult(text, sessionId) {
  const obj = { type: 'result', text };
  if (sessionId) obj.session_id = sessionId;
  emitObj(obj);
}

function emitError(text, sessionId) {
  const obj = { type: 'error', message: text };
  if (sessionId) obj.session_id = sessionId;
  emitObj(obj);
}

// ---------------------------------------------------------------------------
// Config from env
// ---------------------------------------------------------------------------

function env(name) {
  const v = process.env[name];
  return v == null ? '' : v.trim();
}

function stateDir() {
  const d = env('RECURSIVE_STATE_DIR') || path.join(os.tmpdir(), 'agentproc-recursive');
  fs.mkdirSync(d, { recursive: true });
  return d;
}

function sessionLinkPath(sid) {
  return path.join(stateDir(), `${sid}.session`);
}

function readSessionDir(sid) {
  let d;
  try {
    d = fs.readFileSync(sessionLinkPath(sid), 'utf8').trim();
  } catch {
    return null;
  }
  if (d && fs.existsSync(d) && fs.statSync(d).isDirectory()) return d;
  return null;
}

function writeSessionDir(sid, sessionDir) {
  try {
    fs.writeFileSync(sessionLinkPath(sid), sessionDir);
  } catch {
    /* best-effort */
  }
}

function extractSessionDir(stderr) {
  const m = SESSION_RECORDING_RE.exec(stderr);
  return m ? m[1] : null;
}

function lastAssistantText(sessionDir) {
  if (!sessionDir) return null;
  const p = path.join(sessionDir, 'transcript.jsonl');
  let last = null;
  let data;
  try {
    data = fs.readFileSync(p, 'utf8');
  } catch {
    return null;
  }
  for (const raw of data.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    let entry;
    try {
      entry = JSON.parse(line);
    } catch {
      continue;
    }
    if (entry && entry.role === 'assistant' && entry.content) last = entry.content;
  }
  return last;
}

// ---------------------------------------------------------------------------
// Argument building
// ---------------------------------------------------------------------------

function providerArgs() {
  const args = [];
  if (env('RECURSIVE_API_KEY')) args.push('--api-key', env('RECURSIVE_API_KEY'));
  if (env('RECURSIVE_PROVIDER')) args.push('--provider', env('RECURSIVE_PROVIDER'));
  if (env('RECURSIVE_API_BASE')) args.push('--api-base', env('RECURSIVE_API_BASE'));
  if (env('RECURSIVE_MODEL')) args.push('--model', env('RECURSIVE_MODEL'));
  return args;
}

function globalArgs() {
  // Wire 0.3: always --stream. The runner filters partials by profile.streaming.
  const args = [CLI_NAME, '--json', '-H', '--stream'];
  args.push('--permission-mode', env('RECURSIVE_PERMISSION_MODE') || 'auto');
  if (env('RECURSIVE_MAX_STEPS')) args.push('--max-steps', env('RECURSIVE_MAX_STEPS'));
  if (env('RECURSIVE_WORKSPACE')) args.push('--workspace', env('RECURSIVE_WORKSPACE'));
  for (const a of providerArgs()) args.push(a);
  return args;
}

function buildRunArgs(message) {
  return globalArgs().concat(['run', message]);
}

function buildResumeArgs(sessionDir, message) {
  // `resume --from-file <dir> -p <msg>` continues the session by appending
  // <msg> as the next user turn (native session-id resume).
  return globalArgs().concat(['resume', '--from-file', sessionDir, '-p', message]);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const turn = await readTurn();
  const message = (typeof turn.message === 'string') ? turn.message : '';
  const attachments = Array.isArray(turn.attachments) ? turn.attachments : [];
  if (!message && attachments.length === 0) {
    emitError('turn.message is required (or include turn.attachments)');
    return 1;
  }

  // Resolve opaque session id + whether we resume an existing recursive session.
  const givenSid = (typeof turn.session_id === 'string') ? turn.session_id : '';
  let resumeDir = null;
  let sid;
  if (givenSid) {
    const d = readSessionDir(givenSid);
    if (d) {
      sid = givenSid;
      resumeDir = d;
    } else {
      sid = 'rc-' + crypto.randomUUID().replace(/-/g, '');
    }
  } else {
    sid = 'rc-' + crypto.randomUUID().replace(/-/g, '');
  }

  const args = resumeDir
    ? buildResumeArgs(resumeDir, message)
    : buildRunArgs(message);

  let child;
  try {
    child = spawn(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    emitError(`${CLI_NAME} CLI not found. ${INSTALL_HINT}`, sid);
    return 1;
  }

  // Per-step text buffers (ordered) so we can assemble the final reply.
  const stepOrder = [];
  const stepBuffers = new Map();
  const stepsWithPartials = new Set();
  let errorMessage = null;
  let stderrOutput = '';

  return new Promise((resolve) => {
    let stdoutBuf = '';
    child.stdout.on('data', (chunk) => {
      stdoutBuf += chunk.toString();
      let nl;
      while ((nl = stdoutBuf.indexOf('\n')) >= 0) {
        const rawLine = stdoutBuf.slice(0, nl);
        stdoutBuf = stdoutBuf.slice(nl + 1);
        handleLine(rawLine.replace(/\r$/, ''));
      }
    });

    child.stderr.on('data', (chunk) => {
      stderrOutput += chunk.toString();
    });

    function handleLine(rawLine) {
      const line = rawLine.trim();
      if (!line) return;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        return;
      }
      const etype = event.type;

      if (etype === 'partial_token') {
        const text = event.text || '';
        if (!text) return;
        const step = event.step;
        if (step != null) {
          stepsWithPartials.add(step);
          if (!stepBuffers.has(step)) {
            stepBuffers.set(step, []);
            stepOrder.push(step);
          }
          stepBuffers.get(step).push(text);
        }
        emitPartial(text, sid);
        return;
      }

      if (etype === 'assistant_text') {
        const text = event.text || '';
        if (!text) return;
        const step = event.step;
        // If this step already streamed deltas, they compose the step text —
        // skip the duplicate full text. Otherwise this is our only chance.
        if (step != null && stepsWithPartials.has(step)) return;
        if (step != null) {
          if (!stepBuffers.has(step)) {
            stepBuffers.set(step, []);
            stepOrder.push(step);
          }
          stepBuffers.get(step).push(text);
        }
        emitPartial(text, sid);
        return;
      }

      // turn_finished: terminal; keep draining until close.
    }

    child.on('error', () => {
      // spawn-side ENOENT surfaces here on some platforms.
      if (!errorMessage) {
        errorMessage = `${CLI_NAME} CLI not found. ${INSTALL_HINT}`;
      }
    });

    child.on('close', (code) => {
      // For a fresh run, record the recursive session directory so the next
      // turn can resume it. (resume reuses the same dir — no update needed.)
      if (resumeDir == null) {
        const captured = extractSessionDir(stderrOutput);
        if (captured) writeSessionDir(sid, captured);
      }

      let replyText = '';
      for (const s of stepOrder) replyText += stepBuffers.get(s).join('');
      replyText = replyText.trim();

      // Fallback: recover last assistant text from the session transcript.
      if (!replyText && !errorMessage) {
        const sessDir = resumeDir || readSessionDir(sid);
        const recovered = lastAssistantText(sessDir);
        if (recovered) replyText = recovered.trim();
      }

      if (errorMessage) {
        emitError(errorMessage, sid);
        return resolve(1);
      }
      if (code !== 0 && !replyText) {
        let msg = `${CLI_NAME} exited with ${code}`;
        const tail = stderrOutput.trim();
        if (tail) msg += `: ${tail.slice(0, 500)}`;
        emitError(msg, sid);
        return resolve(1);
      }
      if (!replyText) {
        emitError(`${CLI_NAME} produced no reply text`, sid);
        return resolve(1);
      }
      emitResult(replyText, sid);
      resolve(0);
    });
  });
}

// Exported for the cross-language parity test (hub/recursive/tests). The
// main() call is guarded so requiring this file as a module does not run the
// bridge — only `node bridge.js` does.
module.exports = {
  CLI_NAME,
  env,
  providerArgs,
  globalArgs,
  extractSessionDir,
  lastAssistantText,
  SESSION_RECORDING_RE,
};

if (require.main === module) {
  main().then((code) => {
    process.exitCode = code;
  });
}
