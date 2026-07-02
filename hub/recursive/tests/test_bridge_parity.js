'use strict';
/**
 * Cross-language parity test for hub/recursive/bridge.js.
 *
 * Drives the shared `hub/recursive/tests/parity.json` fixture through the
 * Node bridge's pure helpers (`providerArgs`, `globalArgs`, `extractSessionDir`,
 * `lastAssistantText`) and asserts they match the fixture's expectations. The
 * Python bridge runs the same fixture through its helpers in
 * `hub/recursive/tests/test_bridge_parity.py` — together they guard the
 * hub/README.md claim that both bridges produce identical observable behaviour.
 *
 * Run: `node --test hub/recursive/tests/test_bridge_parity.js`
 */

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const HERE = __dirname;
const BRIDGE_JS = path.join(HERE, '..', 'bridge.js');
const PARITY_JSON = path.join(HERE, 'parity.json');

// bridge.js guards main() with `require.main === module`, so requiring it here
// does not run the bridge.
const bridge = require(BRIDGE_JS);
const data = JSON.parse(fs.readFileSync(PARITY_JSON, 'utf8'));

const RECURSIVE_VARS = [
  'RECURSIVE_API_KEY', 'RECURSIVE_PROVIDER', 'RECURSIVE_API_BASE',
  'RECURSIVE_MODEL', 'RECURSIVE_WORKSPACE', 'RECURSIVE_MAX_STEPS',
  'RECURSIVE_PERMISSION_MODE', 'RECURSIVE_STATE_DIR', 'AGENT_STREAMING',
];

function withCleanEnv(caseEnv, fn) {
  const saved = {};
  for (const v of RECURSIVE_VARS) {
    saved[v] = process.env[v];
    delete process.env[v];
  }
  try {
    for (const [k, val] of Object.entries(caseEnv)) {
      process.env[k] = val;
    }
    return fn();
  } finally {
    for (const v of RECURSIVE_VARS) {
      if (saved[v] === undefined) delete process.env[v];
      else process.env[v] = saved[v];
    }
  }
}

for (const c of data.arg_cases) {
  test(`arg_building: ${c.name}`, () => {
    withCleanEnv(c.env, () => {
      assert.deepStrictEqual(bridge.providerArgs(), c.expect_provider_args);
      assert.deepStrictEqual(bridge.globalArgs(), c.expect_global_args);
    });
  });
}

for (let i = 0; i < data.extract_session_dir_cases.length; i++) {
  const c = data.extract_session_dir_cases[i];
  test(`extract_session_dir: ${i}`, () => {
    assert.strictEqual(bridge.extractSessionDir(c.stderr), c.expect);
  });
}

for (let i = 0; i < data.last_assistant_text_cases.length; i++) {
  const c = data.last_assistant_text_cases[i];
  test(`last_assistant_text: ${i}`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rec-parity-'));
    if (c.transcript_lines.length) {
      fs.writeFileSync(
        path.join(dir, 'transcript.jsonl'),
        c.transcript_lines.map((l) => JSON.stringify(l)).join('\n') + '\n',
      );
    }
    assert.strictEqual(bridge.lastAssistantText(dir), c.expect);
  });
}

test('last_assistant_text: null session dir', () => {
  assert.strictEqual(bridge.lastAssistantText(null), null);
});
