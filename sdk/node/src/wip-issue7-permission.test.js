'use strict';
/**
 * [wip] issue #7 — executor permission posture (repro tests, expected to FAIL on baseline 7327f18).
 *
 * Node mirror of `sdk/python/tests/test_wip_issue7_permission.py`. Baseline:
 * `{executor: claude-code, permission: true}` spawns
 * `claude -p <msg> --output-format stream-json --verbose --dangerously-skip-permissions`
 * (no `--permission-prompt-tool stdio`, no warning), executors without a
 * permission channel run their auto-approve flag (`--dangerously-skip-permissions`
 * / `--yolo`) unchecked, and there is no environment-level posture switch.
 *
 * Required end state — see the Python file's docstring and the issue acceptance:
 * (1) claude-code + permission:true → `--permission-prompt-tool stdio`, no
 * skip-permissions; (2) permission:true + channelless executor → hard fail;
 * (3) `AGENTPROC_AUTO_APPROVE=0` → fail closed on every auto-approve argv;
 * (4) all of it driven by `spec/conformance/cases.json` (`posture_cases`).
 *
 * Run with: `node --test src/wip-issue7-permission.test.js`
 * Observable-only: the fake CLI records its own argv, so the tests survive any
 * internal signature change. Remove the `[wip]` prefix when green.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { run } = require('./runner.js');
const { EXECUTORS } = require('./executors.js');

const CONFORMANCE_CASES = path.resolve(__dirname, '../../../spec/conformance/cases.json');

const AUTO_APPROVE_FLAGS = [
  '--dangerously-skip-permissions',
  '--yolo',
  '--always-approve',
  '--yes-always',
  '--approve',
  '--auto',
];

const SHIM = `#!/usr/bin/env bash
printf '%s\\n' "$@" > {argvFile}
echo '{"type":"result","result":"ok","session_id":"sess-1"}'
`;

/**
 * Run an executor with a fake CLI on PATH; returns { result, argv } where argv
 * is the token list the CLI actually received (null when it never spawned).
 */
async function runWithShim(executorName, { permission, env = {}, message = 'hi' } = {}) {
  const cliName = EXECUTORS[executorName].cliName;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-issue7-'));
  const argvFile = path.join(dir, 'argv.txt');
  const shim = path.join(dir, cliName);
  fs.writeFileSync(shim, SHIM.replace('{argvFile}', argvFile), { mode: 0o755 });

  const savedPath = process.env.PATH;
  const savedEnv = {};
  process.env.PATH = `${dir}${path.delimiter}${savedPath}`;
  for (const [k, v] of Object.entries(env)) {
    savedEnv[k] = process.env[k];
    process.env[k] = v;
  }

  const profile = { executor: executorName };
  if (permission !== undefined) profile.permission = permission;

  let result;
  try {
    result = await run(profile, { message, extraEnv: env });
  } finally {
    process.env.PATH = savedPath;
    for (const [k, v] of Object.entries(savedEnv)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }

  const argv = fs.existsSync(argvFile)
    ? fs.readFileSync(argvFile, 'utf8').split('\n').filter(Boolean)
    : null;
  return { result, argv };
}

describe('[wip] issue #7 — claude-code permission mode', () => {
  test('permission:true → --permission-prompt-tool stdio, no skip-permissions', async () => {
    const { result, argv } = await runWithShim('claude-code', { permission: true });
    assert.ok(argv, 'claude-code was never spawned');
    assert.ok(argv.includes('--permission-prompt-tool'), `argv: ${argv}`);
    assert.strictEqual(argv[argv.indexOf('--permission-prompt-tool') + 1], 'stdio');
    assert.ok(!argv.includes('--dangerously-skip-permissions'), `argv: ${argv}`);
    assert.strictEqual(result.error, '');
    assert.strictEqual(result.exitCode, 0);
  });

  test('permission absent → unattended argv unchanged', async () => {
    const { result, argv } = await runWithShim('claude-code', {});
    assert.ok(argv, 'claude-code was never spawned');
    assert.ok(argv.includes('--dangerously-skip-permissions'), `argv: ${argv}`);
    assert.ok(!argv.includes('--permission-prompt-tool'), `argv: ${argv}`);
    assert.strictEqual(result.error, '');
    assert.strictEqual(result.exitCode, 0);
  });
});

describe('[wip] issue #7 — executor without a permission channel', () => {
  test('codebuddy + permission:true → hard fail, no skip-permissions run', async () => {
    const { result, argv } = await runWithShim('codebuddy', { permission: true });
    assert.notStrictEqual(result.error, '', 'expected a hard failure, got a silent run');
    assert.notStrictEqual(result.exitCode, 0);
    assert.match(result.error.toLowerCase(), /permission/);
    if (argv) assert.ok(!argv.includes('--dangerously-skip-permissions'), `argv: ${argv}`);
  });

  test('gemini-cli + permission:true → hard fail, no --yolo run', async () => {
    const { result, argv } = await runWithShim('gemini-cli', { permission: true });
    assert.notStrictEqual(result.error, '');
    assert.notStrictEqual(result.exitCode, 0);
    if (argv) assert.ok(!argv.includes('--yolo'), `argv: ${argv}`);
  });
});

describe('[wip] issue #7 — AGENTPROC_AUTO_APPROVE=0 posture switch', () => {
  test('refuses --dangerously-skip-permissions', async () => {
    const { result, argv } = await runWithShim('claude-code', {
      env: { AGENTPROC_AUTO_APPROVE: '0' },
    });
    assert.notStrictEqual(result.error, '', 'expected fail-closed, got a skip-permissions run');
    assert.notStrictEqual(result.exitCode, 0);
    if (argv) for (const flag of AUTO_APPROVE_FLAGS) assert.ok(!argv.includes(flag), `argv: ${argv}`);
  });

  test('refuses --yolo', async () => {
    const { result, argv } = await runWithShim('gemini-cli', {
      env: { AGENTPROC_AUTO_APPROVE: '0' },
    });
    assert.notStrictEqual(result.error, '');
    assert.notStrictEqual(result.exitCode, 0);
    if (argv) assert.ok(!argv.includes('--yolo'), `argv: ${argv}`);
  });

  test('still allows claude-code permission mode', async () => {
    const { result, argv } = await runWithShim('claude-code', {
      permission: true,
      env: { AGENTPROC_AUTO_APPROVE: '0' },
    });
    assert.strictEqual(result.error, '');
    assert.strictEqual(result.exitCode, 0);
    assert.ok(argv, 'claude-code was never spawned');
    assert.ok(argv.includes('--permission-prompt-tool'), `argv: ${argv}`);
    assert.ok(!argv.includes('--dangerously-skip-permissions'), `argv: ${argv}`);
  });
});

describe('[wip] issue #7 — spec/conformance/cases.json posture_cases', () => {
  test('Node honours every shared posture case', async () => {
    const data = JSON.parse(fs.readFileSync(CONFORMANCE_CASES, 'utf8'));
    assert.ok(data.posture_cases, 'spec/conformance/cases.json has no posture_cases section');
    assert.ok(data.posture_cases.length > 0, 'posture_cases is empty');

    for (const c of data.posture_cases) {
      const { result, argv } = await runWithShim(c.executor, {
        permission: c.permission,
        env: c.env || {},
      });
      const expect = c.expect;
      if (expect.error) {
        assert.notStrictEqual(result.error, '', `${c.name}: expected an error`);
        assert.notStrictEqual(result.exitCode, 0, `${c.name}: expected non-zero exit`);
      }
      if (expect.exit_zero) {
        assert.strictEqual(result.error, '', `${c.name}: ${result.error}`);
        assert.strictEqual(result.exitCode, 0, `${c.name}: exit`);
      }
      if (expect.reply !== undefined) assert.strictEqual(result.reply, expect.reply, c.name);
      if (argv) {
        for (const token of expect.argv_contains || []) {
          assert.ok(argv.includes(token), `${c.name}: ${token} missing from ${argv}`);
        }
        for (const token of expect.argv_excludes || []) {
          assert.ok(!argv.includes(token), `${c.name}: ${token} present in ${argv}`);
        }
      } else if ((expect.argv_contains || []).length) {
        assert.fail(`${c.name}: CLI never spawned, cannot check argv`);
      }
    }
  });
});
