'use strict';
/**
 * Unit + integration tests for the dsh bridge: session-continuity feature
 * detection, argv shape, and wire stamping.
 * Run: node --test hub/dsh/bridge.test.js
 *
 * Integration cases drive bridge.js as a subprocess with a fake `dsh` shim
 * on PATH (two flavors: resume-capable and legacy), mirroring how the
 * AgentProc runner exercises the bridge for real.
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
  parseSessionId,
  probeResumeSupport,
} = require(path.join(__dirname, 'bridge.js'));

// ── pure functions ─────────────────────────────────────────────────────

test('buildArgs: stateless when continuity unsupported', () => {
  assert.deepStrictEqual(buildArgs('hi', '', false), ['dsh', '--profile', 'headless', 'hi']);
  assert.deepStrictEqual(buildArgs('hi', 'session-abc', false), ['dsh', '--profile', 'headless', 'hi']);
});

test('buildArgs: new session prints the id without --resume', () => {
  assert.deepStrictEqual(buildArgs('hi', '', true), ['dsh', '--profile', 'headless', '--print-session-id', 'hi']);
});

test('buildArgs: known session resumes it', () => {
  assert.deepStrictEqual(
    buildArgs('hi', 'session-abc', true),
    ['dsh', '--profile', 'headless', '--print-session-id', '--resume', 'session-abc', 'hi'],
  );
});

test('parseSessionId: extracts valid ids, drops wire-invalid ones', () => {
  assert.strictEqual(parseSessionId('noise\ndsh: session-id: session-123\n'), 'session-123');
  assert.strictEqual(parseSessionId('dsh: session-id: session-123'), 'session-123');
  assert.strictEqual(parseSessionId(''), '');
  assert.strictEqual(parseSessionId('nothing here'), '');
  // path separators and control characters are wire-invalid → dropped
  assert.strictEqual(parseSessionId('dsh: session-id: a/b/c'), '');
  assert.strictEqual(parseSessionId('dsh: session-id: a\\b'), '');
});

test('composeTask: appends attachments as reference URLs', () => {
  const task = composeTask('look', { attachments: [{ kind: 'image', url: 'https://x/a.png', filename: 'a.png' }] });
  assert.ok(task.startsWith('look\n'));
  assert.ok(task.includes('- [image] a.png: https://x/a.png'));
});

// ── feature detection via PATH shims ───────────────────────────────────

function shimDir(helpText) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-shim-'));
  const bin = path.join(dir, 'dsh');
  const helpFile = path.join(dir, 'help.txt');
  const callsFile = path.join(dir, 'calls.log');
  fs.writeFileSync(helpFile, helpText);
  fs.writeFileSync(bin, `#!/bin/bash
if printf '%s\\n' "$@" | grep -q -- '--help'; then cat '${helpFile}'; exit 0; fi
printf 'ARG:%s\\n' "$@" >> '${callsFile}'
echo 'dsh: session-id: session-shim-42' >&2
echo 'shim reply'
`);
  fs.chmodSync(bin, 0o755);
  return { dir, callsFile };
}

test('probeResumeSupport: true only when help advertises both flags', () => {
  const capable = shimDir('Options:\n  --resume <session-id>\n  --print-session-id\n');
  const r1 = spawnSync(process.execPath, ['-e',
    `process.env.PATH='${capable.dir}:'+process.env.PATH; const {probeResumeSupport}=require(${JSON.stringify(path.join(__dirname, 'bridge.js'))}); console.log(probeResumeSupport())`],
  { encoding: 'utf8' });
  assert.strictEqual(r1.stdout.trim(), 'true', r1.stderr);

  const legacy = shimDir('Options:\n  -h, --help\n');
  const r2 = spawnSync(process.execPath, ['-e',
    `process.env.PATH='${legacy.dir}:'+process.env.PATH; const {probeResumeSupport}=require(${JSON.stringify(path.join(__dirname, 'bridge.js'))}); console.log(probeResumeSupport())`],
  { encoding: 'utf8' });
  assert.strictEqual(r2.stdout.trim(), 'false', r2.stderr);

  const missing = spawnSync(process.execPath, ['-e',
    `process.env.PATH='/nonexistent-dir-only'; const {probeResumeSupport}=require(${JSON.stringify(path.join(__dirname, 'bridge.js'))}); console.log(probeResumeSupport())`],
  { encoding: 'utf8' });
  assert.strictEqual(missing.stdout.trim(), 'false', missing.stderr);
});

// ── integration: drive bridge.js as the runner would ───────────────────

function driveBridge(shim, turnLine) {
  return spawnSync(process.execPath, [path.join(__dirname, 'bridge.js')], {
    input: turnLine,
    encoding: 'utf8',
    env: { ...process.env, PATH: `${shim.dir}:${process.env.PATH}` },
    timeout: 60_000,
  });
}

test('resume-capable dsh: first turn stamps the printed session id', () => {
  const shim = shimDir('--resume <session-id>\n--print-session-id');
  const r = driveBridge(shim, '{"type":"turn","message":"hi","session_id":"","attachments":[],"permission":false,"protocol_version":"0.4"}\n');
  assert.strictEqual(r.status, 0, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.strictEqual(event.type, 'result');
  assert.strictEqual(event.text, 'shim reply');
  assert.strictEqual(event.session_id, 'session-shim-42');
  const call = fs.readFileSync(shim.callsFile, 'utf8');
  assert.ok(call.includes('ARG:--print-session-id'));
  assert.ok(!call.includes('ARG:--resume'), 'new session must not pass --resume');
});

test('resume-capable dsh: known session resumes it', () => {
  const shim = shimDir('--resume <session-id>\n--print-session-id');
  const r = driveBridge(shim, '{"type":"turn","message":"hi","session_id":"session-prior-7","attachments":[],"permission":false,"protocol_version":"0.4"}\n');
  assert.strictEqual(r.status, 0, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.strictEqual(event.session_id, 'session-shim-42');
  const call = fs.readFileSync(shim.callsFile, 'utf8');
  assert.ok(call.includes('ARG:--resume'));
  assert.ok(call.includes('ARG:session-prior-7'));
});

test('legacy dsh: stays stateless — no flags, no session stamped', () => {
  const shim = shimDir('-h, --help only');
  const r = driveBridge(shim, '{"type":"turn","message":"hi","session_id":"","attachments":[],"permission":false,"protocol_version":"0.4"}\n');
  assert.strictEqual(r.status, 0, r.stderr);
  const event = JSON.parse(r.stdout.trim());
  assert.strictEqual(event.type, 'result');
  assert.strictEqual(event.text, 'shim reply');
  assert.strictEqual(event.session_id, undefined, 'legacy dsh must not stamp an id');
  const call = fs.readFileSync(shim.callsFile, 'utf8');
  assert.ok(!call.includes('--print-session-id'));
  assert.ok(!call.includes('--resume'));
});
