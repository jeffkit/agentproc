'use strict';
/**
 * Unit + integration tests for the dsh bridge: --json feature detection,
 * argv shape, usage summation/mapping, and frame-driven wire behavior.
 * Run: node --test hub/dsh/bridge.test.js
 *
 * Integration cases drive bridge.js as a subprocess with a fake `dsh` shim
 * on PATH (JSON-mode and legacy flavors), mirroring how the AgentProc runner
 * exercises the bridge for real.
 */

const { test } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { spawnSync } = require('node:child_process');

const {
  composeTask,
  buildArgs,
  detectSupport,
  addStep,
  toUsage,
} = require(path.join(__dirname, 'bridge.js'));

const NEW_TURN = '{"type":"turn","message":"hi","session_id":"","attachments":[],"permission":false,"protocol_version":"0.4"}\n';
const JSON_HELP = [
  'Options:',
  '  --json                      write newline-delimited run events to stdout',
  '  --session-id <id>           adopt the persisted Session with this id',
].join('\n');

// ── pure functions ─────────────────────────────────────────────────────

test('detectSupport: --json upgrades transport, --session-id gates continuity', () => {
  assert.deepStrictEqual(detectSupport(JSON_HELP), { jsonMode: true, sessionResume: true });
  assert.deepStrictEqual(detectSupport('  --json  '), { jsonMode: true, sessionResume: false });
  assert.deepStrictEqual(detectSupport('-h, --help only'), { jsonMode: false, sessionResume: false });
  assert.deepStrictEqual(detectSupport(''), { jsonMode: false, sessionResume: false });
});

test('buildArgs: plain fallback for legacy dsh — stateless', () => {
  const plain = { jsonMode: false, sessionResume: false };
  assert.deepStrictEqual(buildArgs('hi', '', plain), ['dsh', '--profile', 'headless', 'hi']);
  assert.deepStrictEqual(buildArgs('hi', 'session-abc', plain), ['dsh', '--profile', 'headless', 'hi']);
});

test('buildArgs: json mode requests the stream, `--` guards the task', () => {
  const support = { jsonMode: true, sessionResume: true };
  assert.deepStrictEqual(
    buildArgs('hi', '', support),
    ['dsh', '--profile', 'headless', '--json', '--', 'hi'],
  );
  assert.deepStrictEqual(
    buildArgs('hi', 'session-abc', support),
    ['dsh', '--profile', 'headless', '--json', '--session-id', 'session-abc', '--', 'hi'],
  );
});

test('buildArgs: continuity needs --session-id advertised', () => {
  const jsonOnly = { jsonMode: true, sessionResume: false };
  assert.deepStrictEqual(
    buildArgs('hi', 'session-abc', jsonOnly),
    ['dsh', '--profile', 'headless', '--json', '--', 'hi'],
  );
});

test('addStep: sums steps; optional buckets survive only while reported', () => {
  let u = addStep(null, { inputTokens: 10, outputTokens: 5, cacheReadTokens: 3 });
  assert.strictEqual(u.inputTokens, 10);
  assert.strictEqual(u.outputTokens, 5);
  assert.strictEqual(u.cacheReadTokens, 3);
  u = addStep(u, { inputTokens: 7, outputTokens: 2 });
  assert.strictEqual(u.inputTokens, 17);
  assert.strictEqual(u.outputTokens, 7);
  assert.strictEqual(u.cacheReadTokens, undefined, 'a bucket omitted by a later step is dropped');
  assert.strictEqual(addStep(u, null), u, 'a frame without usage keeps the accumulator');
  assert.strictEqual(addStep(null, undefined), null);
});

test('toUsage: folds disjoint cache buckets into the inclusive input_tokens', () => {
  assert.deepStrictEqual(
    toUsage({ inputTokens: 17, outputTokens: 7, totalTokens: 28, cacheReadTokens: 5, cacheWriteTokens: 4, reasoningTokens: 2 }),
    {
      input_tokens: 26,
      output_tokens: 7,
      total_tokens: 28,
      cache_read_input_tokens: 5,
      cache_creation_input_tokens: 4,
      reasoning_tokens: 2,
    },
  );
  assert.deepStrictEqual(toUsage({ inputTokens: 10, outputTokens: 5 }), { input_tokens: 10, output_tokens: 5 });
  assert.strictEqual(toUsage(null), null);
  assert.strictEqual(toUsage({}), null);
});

test('composeTask: appends attachments as reference URLs', () => {
  const task = composeTask('look', { attachments: [{ kind: 'image', url: 'https://x/a.png', filename: 'a.png' }] });
  assert.ok(task.startsWith('look\n'));
  assert.ok(task.includes('- [image] a.png: https://x/a.png'));
});

// ── integration: drive bridge.js as the runner would ───────────────────

/**
 * A fake `dsh` on PATH: `--help` prints the given help text; any other
 * invocation logs its argv, prints the given stdout lines, the given stderr,
 * and exits with the given code after an optional delay.
 */
function shimDir({ helpText, frames, exitCode = 0, stderrText = '', sleepSecs = 0 }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-shim-'));
  const bin = path.join(dir, 'dsh');
  fs.writeFileSync(path.join(dir, 'help.txt'), helpText);
  fs.writeFileSync(path.join(dir, 'stdout.txt'), (frames || []).join('\n') + ((frames || []).length ? '\n' : ''));
  fs.writeFileSync(path.join(dir, 'stderr.txt'), stderrText);
  fs.writeFileSync(path.join(dir, 'exit.txt'), String(exitCode));
  fs.writeFileSync(bin, `#!/bin/bash
if printf '%s\\n' "$@" | grep -q -- '--help'; then cat '${dir}/help.txt'; exit 0; fi
printf 'ARG:%s\\n' "$@" >> '${dir}/calls.log'
${sleepSecs ? `sleep ${sleepSecs}\n` : ''}cat '${dir}/stdout.txt'
cat '${dir}/stderr.txt' >&2
exit $(cat '${dir}/exit.txt')
`);
  fs.chmodSync(bin, 0o755);
  return { dir, callsFile: path.join(dir, 'calls.log') };
}

function driveBridge(shim, turnLine, extraEnv = {}) {
  return spawnSync(process.execPath, [path.join(__dirname, 'bridge.js')], {
    input: turnLine,
    encoding: 'utf8',
    env: { ...process.env, PATH: `${shim.dir}:${process.env.PATH}`, ...extraEnv },
    timeout: 60_000,
  });
}

test('json-mode dsh: partials then result, session stamped, usage summed+mapped', () => {
  const shim = shimDir({
    helpText: JSON_HELP,
    frames: [
      '{"type":"session","sessionId":"session-shim-42","cwd":"/tmp"}',
      '{"type":"status","phase":"turn_start","turn":1}',
      '{"type":"status","phase":"step_end","turn":1,"step":1,"usage":{"inputTokens":17,"outputTokens":7,"totalTokens":28,"cacheReadTokens":5,"cacheWriteTokens":4,"reasoningTokens":2}}',
      '{"type":"text","text":"partial one"}',
      '{"type":"status","phase":"step_end","turn":1,"step":2}',
      '{"type":"final","text":"shim reply"}',
    ],
  });
  const r = driveBridge(shim, NEW_TURN);
  assert.strictEqual(r.status, 0, r.stderr);
  const events = r.stdout.trim().split('\n').map((l) => JSON.parse(l));
  assert.deepStrictEqual(events[0], { type: 'partial', text: 'partial one', session_id: 'session-shim-42' });
  assert.deepStrictEqual(events[1], {
    type: 'result',
    text: 'shim reply',
    session_id: 'session-shim-42',
    usage: {
      input_tokens: 26,
      output_tokens: 7,
      total_tokens: 28,
      cache_read_input_tokens: 5,
      cache_creation_input_tokens: 4,
      reasoning_tokens: 2,
    },
  });
  const call = fs.readFileSync(shim.callsFile, 'utf8');
  assert.ok(call.includes('ARG:--json'));
  assert.ok(call.includes('ARG:--'));
  assert.ok(!call.includes('ARG:--session-id'), 'new session must not pass --session-id');
});

test('json-mode dsh: known session resumes via --session-id', () => {
  const shim = shimDir({
    helpText: JSON_HELP,
    frames: [
      '{"type":"session","sessionId":"session-prior-7","cwd":"/tmp"}',
      '{"type":"final","text":"shim reply"}',
    ],
  });
  const turn = '{"type":"turn","message":"hi","session_id":"session-prior-7","attachments":[],"permission":false,"protocol_version":"0.4"}\n';
  const r = driveBridge(shim, turn);
  assert.strictEqual(r.status, 0, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.strictEqual(event.session_id, 'session-prior-7');
  const call = fs.readFileSync(shim.callsFile, 'utf8');
  assert.ok(call.includes('ARG:--session-id'));
  assert.ok(call.includes('ARG:session-prior-7'));
});

test('json-mode dsh: error frame (adoption refusal) → error event, session stamped', () => {
  const shim = shimDir({
    helpText: JSON_HELP,
    exitCode: 1,
    frames: [
      '{"type":"session","sessionId":"session-shim-42","cwd":"/tmp"}',
      '{"type":"error","message":"session \\"session-shim-42\\" was recorded in \\"/a\\", not \\"/b\\""}',
    ],
  });
  const r = driveBridge(shim, NEW_TURN);
  assert.strictEqual(r.status, 1, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.deepStrictEqual(event, {
    type: 'error',
    message: 'session "session-shim-42" was recorded in "/a", not "/b"',
    session_id: 'session-shim-42',
  });
});

test('json-mode dsh: final with error exit → error event carrying the stderr diagnostic', () => {
  const shim = shimDir({
    helpText: JSON_HELP,
    exitCode: 1,
    stderrText: 'dsh: PROVIDER_ERROR: quota exceeded\n',
    frames: [
      '{"type":"session","sessionId":"session-shim-42","cwd":"/tmp"}',
      '{"type":"final","text":"partial answer"}',
    ],
  });
  const r = driveBridge(shim, NEW_TURN);
  assert.strictEqual(r.status, 1, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.deepStrictEqual(event, {
    type: 'error',
    message: 'dsh: PROVIDER_ERROR: quota exceeded',
    session_id: 'session-shim-42',
  });
});

test('json-mode dsh: exit 0 without a final (SIGTERM semantics) → error event', () => {
  const shim = shimDir({
    helpText: JSON_HELP,
    frames: ['{"type":"session","sessionId":"session-shim-42","cwd":"/tmp"}'],
  });
  const r = driveBridge(shim, NEW_TURN);
  assert.strictEqual(r.status, 1, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.strictEqual(event.type, 'error');
  assert.ok(event.message.includes('without a final message'), event.message);
});

test('json-mode dsh: DSH_TIMEOUT fires → timeout error, exit 124', () => {
  const shim = shimDir({ helpText: JSON_HELP, frames: ['{"type":"final","text":"late"}'], sleepSecs: 5 });
  const r = driveBridge(shim, NEW_TURN, { DSH_TIMEOUT: '1' });
  assert.strictEqual(r.status, 124, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.strictEqual(event.type, 'error');
  assert.ok(event.message.includes('timed out after 1s'), event.message);
});

test('legacy dsh: stays stateless — no --json, plain stdout as result', () => {
  const shim = shimDir({ helpText: '-h, --help only', frames: ['shim reply'] });
  const r = driveBridge(shim, NEW_TURN);
  assert.strictEqual(r.status, 0, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.deepStrictEqual(event, { type: 'result', text: 'shim reply' });
  const call = fs.readFileSync(shim.callsFile, 'utf8');
  assert.ok(!call.includes('ARG:--json'));
});

test('legacy dsh: non-zero exit with stderr → error event', () => {
  const shim = shimDir({
    helpText: '-h, --help only',
    exitCode: 1,
    stderrText: 'dsh: MISSING_CREDENTIAL: no API key\n',
  });
  const r = driveBridge(shim, NEW_TURN);
  assert.strictEqual(r.status, 1, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.deepStrictEqual(event, { type: 'error', message: 'dsh exited with 1: MISSING_CREDENTIAL: no API key' });
});
