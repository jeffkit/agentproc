'use strict';
/**
 * Cross-implementation conformance tests for the SDK entry point
 * (`createProfile`).
 *
 * Drives the shared `spec/conformance/sdk.json` fixture through the Node SDK
 * entry by spawning `sdk_harness.js` with a controlled AGENT_* env per
 * scenario, then asserts the exact stdout lines and exit code. The Python SDK
 * runs the same fixture through `tests/test_sdk.py` + `tests/_sdk_harness.py`.
 *
 * Together with the existing line-classifier (conformance.test.js) and
 * runner-scenario (scenarios.test.js) suites, this guards the user-facing SDK
 * contract — return types, send_partial / send_error semantics, ProtocolError
 * mapping — against cross-language drift.
 */

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const CASES_PATH = path.resolve(__dirname, '../../../spec/conformance/sdk.json');
const HARNESS = path.resolve(__dirname, 'sdk_harness.js');
const data = JSON.parse(fs.readFileSync(CASES_PATH, 'utf8'));

function runScenario(scenario) {
  // Minimal infra env (no AGENT_* — wire 0.3 input is the turn object on
  // stdin, not env vars). A leftover AGENT_SESSION_ID from the test runner's
  // own environment can't contaminate the result.
  const env = {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    USERPROFILE: process.env.USERPROFILE,
    LANG: process.env.LANG,
    TERM: process.env.TERM,
  };
  const turnLine = JSON.stringify(scenario.turn) + '\n';
  return spawnSync(process.execPath, [HARNESS, scenario.handler], {
    env,
    input: turnLine,
    encoding: 'utf8',
  });
}

for (const s of data.scenarios) {
  test(`sdk: ${s.name}`, () => {
    const r = runScenario(s);
    const lines = r.stdout.replace(/\n$/, '').split('\n');
    assert.strictEqual(
      r.status,
      s.expect.exit,
      `${s.name}: exit ${r.status} !== ${s.expect.exit}\nstdout=${r.stdout}\nstderr=${r.stderr}`,
    );
    assert.deepStrictEqual(
      lines,
      s.expect.stdout_lines,
      `${s.name}: stdout lines mismatch\nexpected: ${JSON.stringify(s.expect.stdout_lines)}\nactual:   ${JSON.stringify(lines)}\nraw: ${JSON.stringify(r.stdout)}`,
    );
  });
}
