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
 * When you change the spec's line-recognition rules, add a case to the JSON
 * file first; both SDKs will fail until they agree.
 */

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { classifyLine } = require('./runner.js');

const CASES_PATH = path.resolve(__dirname, '../../../spec/conformance/cases.json');
const data = JSON.parse(fs.readFileSync(CASES_PATH, 'utf8'));

for (const c of data.cases) {
  test(`classifyLine: ${c.line.slice(0, 60)}`, () => {
    assert.deepStrictEqual(classifyLine(c.line), c.expect);
  });
}
