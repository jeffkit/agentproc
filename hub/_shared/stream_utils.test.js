'use strict';
/**
 * Node-side regression for issue #14: stderr must be drained event-driven so
 * a >64KB stderr flood can't stall stdout. Node has no deadlock (stderr is
 * consumed via 'data' listeners); this locks the behavior for parity with the
 * Python fix. Run: node --test hub/_shared/stream_utils.test.js
 */

const { test } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { spawnSync } = require('node:child_process');

const DRIVER = `
const { runBridge } = require(process.argv[2]);
runBridge({
  cliName: 'fakecli',
  cliInstallHint: 'install hint',
  buildArgs: () => process.argv.slice(3),
  parseEvent: e => (e.type === 'result')
    ? { finalText: e.text }
    : null,
});
`;

function withFakeCli(body, fn) {
  const td = fs.mkdtempSync(path.join(os.tmpdir(), 'sujs-'));
  const cli = path.join(td, 'fakecli.js');
  fs.writeFileSync(cli, body);
  try {
    return fn(cli);
  } finally {
    fs.rmSync(td, { recursive: true, force: true });
  }
}

function runDriver(turnLine, cliPath) {
  const driver = path.join(os.tmpdir(), `sujs-driver-${process.pid}-${Date.now()}.js`);
  fs.writeFileSync(driver, DRIVER);
  try {
    return spawnSync(process.execPath, [driver, path.join(__dirname, 'stream_utils.js'), process.execPath, cliPath], {
      input: turnLine,
      timeout: 20000,
      encoding: 'utf8',
    });
  } finally {
    fs.rmSync(driver, { force: true });
  }
}

test('stderr flood does not stall stdout; result event emitted', () => {
  const body = [
    "const N = 2000;",
    "for (let i = 0; i < N; i++) process.stderr.write('x'.repeat(64) + '\\n');",
    "process.stdout.write(JSON.stringify({ type: 'result', text: 'done' }) + '\\n');",
  ].join('\n');
  withFakeCli(body, cli => {
    const start = Date.now();
    const r = runDriver('{"type":"turn","message":"hi"}\n', cli);
    const elapsed = Date.now() - start;
    assert.strictEqual(r.status, 0, `stderr: ${r.stderr}`);
    assert.ok(elapsed < 20000, `runBridge stalled: ${elapsed}ms`);
    const events = r.stdout.trim().split('\n').map(JSON.parse);
    const results = events.filter(e => e.type === 'result');
    assert.strictEqual(results.length, 1);
    assert.strictEqual(results[0].text, 'done');
  });
});
