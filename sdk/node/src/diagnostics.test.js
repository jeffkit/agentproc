'use strict';
/**
 * Cross-implementation conformance test for the runner's stderr diagnosis.
 *
 * Drives the shared `spec/conformance/diagnostics.json` fixture (the single
 * source of truth for the (pattern, hint) table) through the Node runner and
 * asserts (a) the runner's embedded `STDERR_DIAGNOSTICS` copy matches the
 * fixture rule-for-rule, and (b) each rule's `sample` produces the expected
 * `hint` via `diagnoseStderrFailure`. The Python SDK runs the same fixture
 * through `diagnose_stderr_failure` in `sdk/python/tests/test_diagnostics.py`
 * — together they guarantee the two runners stay at parity on post-mortem
 * hints without one silently drifting.
 */

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { STDERR_DIAGNOSTICS, diagnoseStderrFailure } = require('./runner.js');

const DIAG_PATH = path.resolve(__dirname, '../../../spec/conformance/diagnostics.json');
const data = JSON.parse(fs.readFileSync(DIAG_PATH, 'utf8'));

test('embedded STDERR_DIAGNOSTICS mirrors spec/conformance/diagnostics.json', () => {
  assert.strictEqual(STDERR_DIAGNOSTICS.length, data.rules.length, 'rule count mismatch');
  for (let i = 0; i < data.rules.length; i++) {
    const expected = data.rules[i];
    const got = STDERR_DIAGNOSTICS[i];
    assert.strictEqual(got.id, expected.id, `rule ${i} id`);
    assert.strictEqual(got.pattern, expected.pattern, `rule ${i} pattern`);
    assert.strictEqual(got.hint, expected.hint, `rule ${i} hint`);
    assert.strictEqual(got.flags || '', expected.flags || '', `rule ${i} flags`);
  }
});

for (const rule of data.rules) {
  test(`diagnoseStderrFailure: ${rule.id}`, () => {
    assert.strictEqual(diagnoseStderrFailure(rule.sample), rule.expect);
  });
}

test('diagnoseStderrFailure returns "" on empty / unrecognized stderr', () => {
  assert.strictEqual(diagnoseStderrFailure(''), '');
  assert.strictEqual(diagnoseStderrFailure('totally fine, nothing to see'), '');
});
