'use strict';

/**
 * Cross-implementation multi-line scenario tests.
 *
 * Drives the shared `spec/conformance/scenarios.json` fixture through the
 * Node runner's `run()` and asserts the observable outputs (reply,
 * sessionId, error, exitCode, partials) match the expected values. The
 * Python SDK runs the same fixture in `sdk/python/tests/test_scenarios.py`.
 *
 * Together they guarantee the two reference implementations agree on
 * multi-line interaction semantics: last-wins, error-mid-stream,
 * session-with-error, invalid-session handling, streaming vs one-shot.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { run } = require('./runner.js');

const SCENARIOS_PATH = path.resolve(__dirname, '../../../spec/conformance/scenarios.json');
const data = JSON.parse(fs.readFileSync(SCENARIOS_PATH, 'utf8'));
const SCENARIOS = data.scenarios;

/**
 * Build a bash script that prints each line to stdout verbatim then exits 0.
 * Uses printf with single-quoted args so AGENT_PARTIAL:"..." lines (which
 * contain JSON double-quotes) pass through without bash interpretation.
 */
function bashScriptFor(lines) {
  let body = '#!/usr/bin/env bash\n';
  for (const line of lines) {
    const quoted = "'" + String(line).replace(/'/g, "'\\''") + "'";
    body += `printf '%s\\n' ${quoted}\n`;
  }
  return body;
}

function writeScript(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-scenario-'));
  const file = path.join(dir, 'agent.sh');
  fs.writeFileSync(file, content, { mode: 0o755 });
  return file;
}

describe('scenario conformance (scenarios.json)', () => {
  for (const scenario of SCENARIOS) {
    test(scenario.name, async () => {
      const agent = writeScript(bashScriptFor(scenario.lines));
      const exp = scenario.expect;
      // scenario.profile_overrides lets tests set max_reply_chars etc.
      const profile = Object.assign({ command: agent }, scenario.profile_overrides || {});
      const partials = [];
      const r = await run(
        profile,
        {
          message: 'hi',
          streaming: scenario.streaming !== undefined ? scenario.streaming : true,
          onPartial: (t) => partials.push(t),
        },
      );
      assert.strictEqual(r.reply, exp.reply, `reply: got ${JSON.stringify(r.reply)}, expected ${JSON.stringify(exp.reply)}`);
      assert.strictEqual(r.sessionId, exp.session_id, `sessionId: got ${JSON.stringify(r.sessionId)}, expected ${JSON.stringify(exp.session_id)}`);
      assert.strictEqual(r.error, exp.error, `error: got ${JSON.stringify(r.error)}, expected ${JSON.stringify(exp.error)}`);
      assert.strictEqual(r.exitCode, exp.exit_code, `exitCode: got ${r.exitCode}, expected ${exp.exit_code}`);
      // partials comparison: when `partials_any_of` is present, the scenario
      // accepts any of the listed candidate sequences (used for spec-loose
      // semantics like whether to emit a tail-truncated chunk vs nothing).
      // Otherwise the strict `partials` equality applies.
      if (Array.isArray(exp.partials_any_of)) {
        const matches = exp.partials_any_of.some(cand =>
          JSON.stringify(partials) === JSON.stringify(cand)
        );
        assert.ok(matches, `partials: got ${JSON.stringify(partials)}, expected any of ${JSON.stringify(exp.partials_any_of)}`);
      } else {
        assert.deepStrictEqual(partials, exp.partials, `partials: got ${JSON.stringify(partials)}, expected ${JSON.stringify(exp.partials)}`);
      }
    });
  }
});
