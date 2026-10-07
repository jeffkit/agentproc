'use strict';
/**
 * Cross-implementation conformance tests.
 *
 * Drives the shared `spec/conformance/cases.json` fixture through the Node
 * runner's `classifyLine` and asserts the result matches the expected
 * {kind, value}. The Python SDK runs the same fixture through its
 * `classify_line` in `sdk/python/tests/test_conformance.py` — together they
 * guarantee the two reference implementations classify stdout identically.
 *
 * The same file also carries `posture_cases` — the per-executor permission
 * posture matrix. These are driven through `run()` with a fake CLI on PATH
 * that records its own argv, so a case pins observable behaviour (which argv
 * the CLI received, or that it was never spawned) rather than internal
 * signatures.


 * Also drives `spec/conformance/executors.json` through the in-process
 * executor path (`runViaExecutor`) with a fake tmp-script executor, asserting
 * the full RunResult. The Python SDK (`tests/test_conformance.py`) and the
 * Rust SDK (`sdk/rust/src/conformance.rs`) drive the same fixture.
 *
 * When you change the spec's line-recognition rules, add a case to the JSON
 * file first; both SDKs will fail until they agree.
 */

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { run, classifyLine, AUTO_APPROVE_FLAGS, runViaExecutor, normalizeProfile } = require('./runner.js');
const { EXECUTORS } = require('./executors.js');

const CONFORMANCE_DIR = path.resolve(__dirname, '../../../spec/conformance');
const CASES_PATH = path.join(CONFORMANCE_DIR, 'cases.json');
const data = JSON.parse(fs.readFileSync(CASES_PATH, 'utf8'));

for (const c of data.cases) {
  test(`classifyLine: ${c.line.slice(0, 60)}`, () => {
    assert.deepStrictEqual(classifyLine(c.line), c.expect);
  });
}

// ---------------------------------------------------------------------------
// env composition conformance: the same three-layer policy as the Python
// SDK's _compose_env, driven through the exported composeEnv.
// ---------------------------------------------------------------------------

const { composeEnv } = require('./runner.js');

for (const c of data.env_compose || []) {
  test(`composeEnv: ${JSON.stringify(c.profile_env).slice(0, 50)}`, () => {
    const sourceEnv = c.host_env;
    const profile = normalizeProfile({
      executor: 'conformance',
      env: c.profile_env,
      env_allowlist: c.env_allowlist,
    });
    const env = composeEnv(
      profile,
      { message: 'hi', extraEnv: c.extra_env },
      sourceEnv,
    );
    for (const [k, v] of Object.entries(c.expect_contains)) {
      assert.strictEqual(env[k], v, `${k}: expected ${v}, got ${env[k]}`);
    }
    for (const name of c.expect_absent) {
      assert.ok(!(name in env), `${name} leaked into the composed child env`);
    }
  });
}

// Fake CLI: record every argv token in <dir>/argv.txt, then report a clean turn.
const SHIM = `#!/usr/bin/env bash
printf '%s\\n' "$@" > {argvFile}
echo '{"type":"result","result":"ok","session_id":"sess-1"}'
`;

/** Drive one `posture_cases` entry through `run`; returns { result, argv } (argv null = never spawned). */
async function runPostureCase(c) {
  const cliName = EXECUTORS[c.executor].cliName;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-posture-'));
  const argvFile = path.join(dir, 'argv.txt');
  fs.writeFileSync(path.join(dir, cliName), SHIM.replace('{argvFile}', argvFile), { mode: 0o755 });

  const caseEnv = c.env || {};
  const savedPath = process.env.PATH;
  const savedKnob = process.env.AGENTPROC_AUTO_APPROVE;
  process.env.PATH = `${dir}${path.delimiter}${savedPath}`;
  // Written to the runner process env and to the per-run env extras, so the
  // case reaches the runner either way. Cases without the knob explicitly
  // blank it, so the ambient environment cannot leak a posture switch into
  // the fixture.
  process.env.AGENTPROC_AUTO_APPROVE = caseEnv.AGENTPROC_AUTO_APPROVE || '';

  const profile = { executor: c.executor };
  if ('permission' in c) profile.permission = c.permission;

  let result;
  try {
    result = await run(profile, { message: 'hi', extraEnv: caseEnv });
  } finally {
    process.env.PATH = savedPath;
    if (savedKnob === undefined) delete process.env.AGENTPROC_AUTO_APPROVE;
    else process.env.AGENTPROC_AUTO_APPROVE = savedKnob;
  }

  const argv = fs.existsSync(argvFile)
    ? fs.readFileSync(argvFile, 'utf8').split('\n').filter(Boolean)
    : null;
  return { result, argv };
}

test('embedded AUTO_APPROVE_FLAGS matches auto_approve_flags', () => {
  assert.deepStrictEqual(AUTO_APPROVE_FLAGS, data.auto_approve_flags);
});

for (const c of data.posture_cases) {
  test(`posture: ${c.name}`, async () => {
    const { result, argv } = await runPostureCase(c);
    const expect = c.expect;
    if (expect.refused) {
      assert.strictEqual(argv, null, `${c.name}: CLI was spawned with ${argv}`);
      assert.notStrictEqual(result.error, '', `${c.name}: expected a hard failure`);
      assert.notStrictEqual(result.exitCode, 0, `${c.name}: expected a non-zero exit code`);
      return;
    }
    assert.ok(argv, `${c.name}: CLI was never spawned (${result.error})`);
    if (expect.error) {
      assert.notStrictEqual(result.error, '', `${c.name}: expected an error`);
      assert.notStrictEqual(result.exitCode, 0, `${c.name}: expected non-zero exit`);
    }
    if (expect.exit_zero) {
      assert.strictEqual(result.error, '', `${c.name}: ${result.error}`);
      assert.strictEqual(result.exitCode, 0, `${c.name}: exit ${result.exitCode}`);
    }
    if (expect.reply !== undefined) assert.strictEqual(result.reply, expect.reply, c.name);
    for (const token of expect.argv_contains || []) {
      assert.ok(argv.includes(token), `${c.name}: ${token} missing from ${argv}`);
    }
    for (const token of expect.argv_excludes || []) {
      assert.ok(!argv.includes(token), `${c.name}: ${token} present in ${argv}`);
    }
  });
}

const EXECUTORS_PATH = path.join(CONFORMANCE_DIR, 'executors.json');
const { scenarios } = JSON.parse(fs.readFileSync(EXECUTORS_PATH, 'utf8'));

function tmpScript(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-exec-conf-'));
  const file = path.join(dir, 'mock.sh');
  fs.writeFileSync(file, content, { mode: 0o755 });
  return file;
}

// The fixture's shared rule table (see executors.json _comment).
function parseEvent(event) {
  const t = event.type;
  if (t === 'partial') return { partialText: event.text };
  if (t === 'result') {
    const out = { finalText: event.text ?? '' };
    if (event.session_id) out.sessionId = event.session_id;
    if (event.usage != null) out.usage = event.usage;
    return out;
  }
  if (t === 'error') {
    const out = { error: event.message };
    if (event.session_id) out.sessionId = event.session_id;
    if (event.usage != null) out.usage = event.usage;
    return out;
  }
  return null;
}

function fakeExecutor(lines) {
  const body = lines.map((l) => `echo ${JSON.stringify(JSON.stringify(l))}`).join('\n');
  const cli = tmpScript(`#!/usr/bin/env bash\n${body}\n`);
  return {
    cliName: 'mock-ndjson',
    installHint: '',
    plain: false,
    buildArgs: () => [cli],
    parseEvent,
  };
}

test('executors.json has scenarios', () => {
  // Sanity: an emptied or mis-pathed fixture must fail, not pass vacuously.
  assert.ok(scenarios.length >= 13, `expected >= 13 executor scenarios, got ${scenarios.length}`);
});

// ---------------------------------------------------------------------------
// partial_role_cases: the `onPartial` second argument, observed through a real
// spawn. The `cases` array above only pins `classifyLine`, which cannot see
// the callback's arguments.
// ---------------------------------------------------------------------------

test('partial_role_cases has cases', () => {
  // Sanity: an emptied or mis-pathed fixture must fail, not pass vacuously.
  const n = (data.partial_role_cases || []).length;
  assert.ok(n >= 3, `expected >= 3 partial_role_cases, got ${n}`);
});

for (const c of data.partial_role_cases || []) {
  test(`partial_role_cases: ${c.name}`, async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-role-'));
    try {
      const agent = path.join(dir, 'agent.sh');
      fs.writeFileSync(
        agent,
        '#!/usr/bin/env bash\n' +
          `printf '%s\\n' ${JSON.stringify(c.line)}\n` +
          'printf \'%s\\n\' \'{"type":"result","text":""}\'\n',
        { mode: 0o755 },
      );
      const seen = [];
      const r = await run(
        { command: agent },
        {
          message: 'hi',
          streaming: true,
          onPartial: (text, role) => seen.push([text, role === undefined ? null : role]),
        },
      );
      assert.strictEqual(r.exitCode, 0, `${c.name}: exit ${r.exitCode} (${r.error})`);
      assert.deepStrictEqual(
        seen,
        [[c.expect_text, c.expect_role]],
        `${c.name}: onPartial calls were ${JSON.stringify(seen)}`,
      );
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}

for (const sc of scenarios) {
  test(`executors.json: ${sc.name}`, async () => {
    const exp = sc.expect;
    const partials = [];
    const r = await runViaExecutor(
      normalizeProfile({ command: 'dummy', executor: 'test' }),
      { message: 'hi', streaming: sc.streaming, onPartial: (p) => partials.push(p) },
      fakeExecutor(sc.lines),
    );
    assert.deepStrictEqual(r.reply, exp.reply, 'reply');
    assert.deepStrictEqual(r.sessionId, exp.session_id, 'sessionId');
    assert.deepStrictEqual(r.error, exp.error, 'error');
    assert.deepStrictEqual(r.exitCode, exp.exit_code, 'exitCode');
    assert.deepStrictEqual(r.usage ?? null, exp.usage ?? null, 'usage');
    assert.deepStrictEqual(partials, exp.partials, 'partials');
  });
}
