#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `recursive` CLI (self-improving Rust coding agent).
 *
 * Parity implementation of hub/recursive/bridge.py. See that file for the
 * full design rationale; this file mirrors it line-for-line in behaviour.
 *
 *   recursive --json [--stream] ... run <message>                       // turn 1
 *   recursive --json [--stream] ... resume --from-file <session-dir> \
 *       -p <message>                                                    // turn N+
 *
 * Multi-turn continuity uses recursive's native session resume: turn 1 runs
 * `recursive run` and captures the session directory recursive logs on stderr
 * (`session: recording to <dir>`); turn N+ runs `recursive resume --from-file
 * <dir> -p <msg>`, which continues that session by appending <msg> as the next
 * user turn. No transcript-file replay, no --resume-from indexing, no
 * system-message stripping.
 */

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const CLI_NAME = 'recursive';
const INSTALL_HINT =
  'Install: cargo install --locked --path .  (then `recursive init` to configure a provider)';

// `session: recording to <abs-dir>` — recursive logs this to stderr when it
// creates a session writer.
const SESSION_RECORDING_RE = /session: recording to (\S+)/;

// ---------------------------------------------------------------------------
// Emission helpers
// ---------------------------------------------------------------------------

function emit(line) {
  process.stdout.write(line + '\n');
}

function emitSession(sessionId) {
  emit(`AGENT_SESSION:${sessionId}`);
}

function emitPartial(text) {
  emit(`AGENT_PARTIAL:${JSON.stringify(text)}`);
}

function emitError(text) {
  emit(`AGENT_ERROR:${JSON.stringify(text)}`);
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
  const args = [CLI_NAME, '--json', '-H'];
  if (env('AGENT_STREAMING') !== '0') args.push('--stream');
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

function hasAttachment() {
  if (env('AGENT_IMAGE_URL') || env('AGENT_FILE_URL')) return true;
  const raw = env('AGENT_ATTACHMENTS');
  return !!raw && raw !== '[]';
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function main() {
  const message = process.env.AGENT_MESSAGE || '';
  if (!message && !hasAttachment()) {
    emitError(
      'AGENT_MESSAGE env var is required (or set AGENT_ATTACHMENTS / ' +
        'AGENT_IMAGE_URL / AGENT_FILE_URL)'
    );
    return 1;
  }

  const streaming = env('AGENT_STREAMING') !== '0';

  // Resolve opaque session id + whether we resume an existing recursive session.
  const givenSid = env('AGENT_SESSION_ID');
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

  // Emit the session id FIRST so the runner captures it even if recursive
  // later fails to produce any output.
  emitSession(sid);

  let child;
  try {
    child = spawn(args[0], args.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    emitError(`${CLI_NAME} CLI not found. ${INSTALL_HINT}`);
    return 1;
  }

  let sawPartial = false;
  const stepsWithPartials = new Set();
  const assistantTexts = [];
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
        if (event.step != null) stepsWithPartials.add(event.step);
        if (streaming) {
          emitPartial(text);
          sawPartial = true;
        } else {
          assistantTexts.push(text);
        }
        return;
      }

      if (etype === 'assistant_text') {
        const text = event.text || '';
        if (!text) return;
        const step = event.step;
        if (streaming) {
          if (stepsWithPartials.has(step)) return; // duplicate of streamed deltas
          emitPartial(text);
          sawPartial = true;
        } else {
          assistantTexts.push(text);
        }
        return;
      }

      // turn_finished: terminal; keep draining until close.
    }

    child.on('error', (err) => {
      // spawn-side ENOENT surfaces here on some platforms.
      if (!errorMessage) {
        errorMessage = `${CLI_NAME} CLI not found. ${INSTALL_HINT}`;
      }
      // resolve below via close
    });

    child.on('close', (code) => {
      // For a fresh run, record the recursive session directory so the next
      // turn can resume it. (resume reuses the same dir — no update needed.)
      if (resumeDir == null) {
        const captured = extractSessionDir(stderrOutput);
        if (captured) writeSessionDir(sid, captured);
      }

      // Non-streaming: emit collected assistant text as the reply body.
      if (!streaming && assistantTexts.length) {
        const body = assistantTexts.join('').trim();
        if (body) {
          emit(body);
          sawPartial = true;
        }
      }

      // Fallback: recover last assistant text from the session transcript.
      if (!sawPartial && !errorMessage) {
        const sessDir = resumeDir || readSessionDir(sid);
        const recovered = lastAssistantText(sessDir);
        if (recovered) {
          if (streaming) emitPartial(recovered);
          else emit(recovered);
          sawPartial = true;
        }
      }

      if (errorMessage) {
        emitError(errorMessage);
        return resolve(1);
      }
      if (code !== 0 && !sawPartial) {
        let msg = `${CLI_NAME} exited with ${code}`;
        const tail = stderrOutput.trim();
        if (tail) msg += `: ${tail.slice(0, 500)}`;
        emitError(msg);
        return resolve(1);
      }
      if (!sawPartial) {
        emitError(`${CLI_NAME} produced no reply text`);
        return resolve(1);
      }
      resolve(0);
    });
  });
}

main().then((code) => {
  process.exitCode = code;
});
