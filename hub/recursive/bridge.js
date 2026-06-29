#!/usr/bin/env node
'use strict';
/**
 * AgentProc bridge for the `recursive` CLI (self-improving Rust coding agent).
 *
 * Parity implementation of hub/recursive/bridge.py. See that file for the
 * full design rationale; this file mirrors it line-for-line in behaviour.
 *
 *   recursive --json [--stream] ... run <message>              // first turn
 *   recursive --json [--stream] ... replay <transcript> \
 *       --resume-from <N> <message>                            // subsequent turns
 *
 * Multi-turn continuity is managed by the bridge (recursive's CLI exposes no
 * session id in its --json stream and `recursive resume` re-runs the original
 * goal). The bridge mints an opaque id, persists each turn's transcript via
 * `--transcript-out`, and feeds it back through `replay --resume-from N` on
 * the next turn. System messages are stripped between turns.
 */

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const CLI_NAME = 'recursive';
const INSTALL_HINT =
  'Install: cargo install --locked --path .  (then `recursive init` to configure a provider)';

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

function transcriptPath(sid) {
  return path.join(stateDir(), `${sid}.json`);
}

function countMessages(p) {
  let data;
  try {
    data = JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return null;
  }
  if (!data || !Array.isArray(data.messages)) return null;
  return data.messages.length;
}

function stripSystemMessages(p) {
  let data;
  try {
    data = JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return;
  }
  if (!data || !Array.isArray(data.messages)) return;
  data.messages = data.messages.filter((m) => m && m.role !== 'system');
  try {
    fs.writeFileSync(p + '.tmp', JSON.stringify(data));
    fs.renameSync(p + '.tmp', p);
  } catch {
    /* best-effort */
  }
}

function lastAssistantText(p) {
  let data;
  try {
    data = JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return null;
  }
  if (!data || !Array.isArray(data.messages)) return null;
  for (let i = data.messages.length - 1; i >= 0; i--) {
    const m = data.messages[i];
    if (m && m.role === 'assistant' && m.content) return m.content;
  }
  return null;
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

function buildArgs(message, p, resumeN) {
  const args = [CLI_NAME, '--json', '-H', '--no-session'];
  args.push('--transcript-out', p);
  if (env('AGENT_STREAMING') !== '0') args.push('--stream');
  args.push('--permission-mode', env('RECURSIVE_PERMISSION_MODE') || 'auto');
  if (env('RECURSIVE_MAX_STEPS')) args.push('--max-steps', env('RECURSIVE_MAX_STEPS'));
  if (env('RECURSIVE_WORKSPACE')) args.push('--workspace', env('RECURSIVE_WORKSPACE'));
  for (const a of providerArgs()) args.push(a);
  if (resumeN != null) {
    args.push('replay', p, '--resume-from', String(resumeN), message);
  } else {
    args.push('run', message);
  }
  return args;
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

  // Resolve session id + resume target.
  const givenSid = env('AGENT_SESSION_ID');
  let resumeN = null;
  let sid;
  if (givenSid) {
    const p = transcriptPath(givenSid);
    const n = countMessages(p);
    if (n != null && n > 0) {
      sid = givenSid;
      resumeN = n;
    } else {
      sid = 'rc-' + crypto.randomUUID().replace(/-/g, '');
    }
  } else {
    sid = 'rc-' + crypto.randomUUID().replace(/-/g, '');
  }

  const p = transcriptPath(sid);
  const args = buildArgs(message, p, resumeN);

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
      // Non-streaming: emit collected assistant text as the reply body.
      if (!streaming && assistantTexts.length) {
        const body = assistantTexts.join('').trim();
        if (body) {
          emit(body);
          sawPartial = true;
        }
      }

      // Fallback: recover last assistant text from the transcript file.
      if (!sawPartial && !errorMessage) {
        const recovered = lastAssistantText(p);
        if (recovered) {
          if (streaming) emitPartial(recovered);
          else emit(recovered);
          sawPartial = true;
        }
      }

      // Rewrite transcript so the next turn's seed has no system messages.
      stripSystemMessages(p);

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
