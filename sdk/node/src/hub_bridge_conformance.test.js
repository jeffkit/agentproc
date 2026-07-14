'use strict';

/**
 * Hub bridge engine conformance (wire 0.4) — Node side.
 *
 * Drives the shared `hub/_shared/stream_utils.js` `runBridge` (via a tiny
 * harness that uses an identity parseEvent) against the same
 * `spec/conformance/hub_bridge.json` fixture the Python SDK consumes.
 *
 * Because `runBridge` ends with `process.exit`, the bridge must run as a
 * subprocess. The test:
 *   1. Writes a fake-CLI bash script that prints each scenario's cli_stdout
 *      lines and exits cli_exit.
 *   2. Writes a fake-CLI-name wrapper on PATH so buildArgs()'s argv[0]
 *      resolves to it.
 *   3. Spawns the Node bridge harness (sdk/node/src/_hub_bridge_harness.js)
 *      with PATH overridden, writes the scenario's turn object on stdin,
 *      collects stdout NDJSON, and asserts against the fixture.
 *
 * The harness uses an identity parseEvent (maps {partial/result/error} +
 * optional session_id) so this isolates runBridge engine behaviour, not
 * per-CLI parse logic.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const FIXTURE_PATH = path.resolve(__dirname, '../../../spec/conformance/hub_bridge.json');
const HARNES_PATH = path.join(__dirname, '_hub_bridge_harness.js');
const DATA = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf8'));
const SCENARIOS = DATA.scenarios;

function writeFakeCli(dir, lines, exitCode, stderr) {
  // Bash script that prints each line to stdout, optionally writes stderr,
  // then exits. argv[1] (if any) is ignored — the harness's buildArgs puts
  // the message as argv[1], which we discard.
  const script = '#!/usr/bin/env bash\n' +
    (stderr ? `printf '%s\\n' ${JSON.stringify(stderr)} >&2\n` : '') +
    lines.map(l => `printf '%s\\n' ${JSON.stringify(l)}`).join('\n') + '\n' +
    `exit ${exitCode}\n`;
  const file = path.join(dir, 'fake-cli');
  fs.writeFileSync(file, script, { mode: 0o755 });
  return file;
}

function runHarness(env, stdinPayload) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HARNES_PATH], {
      env: { ...process.env, ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', d => { stdout += d.toString(); });
    child.stderr.on('data', d => { stderr += d.toString(); });
    child.on('close', code => resolve({ code, stdout, stderr }));
    child.stdin.write(stdinPayload);
    child.stdin.end();
  });
}

describe('hub bridge engine conformance (hub_bridge.json)', () => {
  for (const scenario of SCENARIOS) {
    test(scenario.name, async () => {
      const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-hubbridge-'));
      const fakeCli = writeFakeCli(
        tmpDir,
        scenario.cli_stdout || [],
        scenario.cli_exit != null ? scenario.cli_exit : 0,
        scenario.cli_stderr || '',
      );
      // Put fake-cli on PATH under its expected name.
      const binDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-bin-'));
      fs.symlinkSync(fakeCli, path.join(binDir, 'fake-cli'));

      try {
        const result = await runHarness(
          { PATH: `${binDir}:${process.env.PATH}` },
          JSON.stringify(scenario.turn) + '\n',
        );
        const actualLines = result.stdout.split('\n').filter(Boolean);
        const exp = scenario.expect;
        assert.strictEqual(result.code, exp.exit_code,
          `${scenario.name}: exit got ${result.code}, expected ${exp.exit_code}\nstderr: ${result.stderr}`);

        if (exp.stdout_lines) {
          assert.deepStrictEqual(actualLines, exp.stdout_lines,
            `${scenario.name}: stdout got ${JSON.stringify(actualLines)}, expected ${JSON.stringify(exp.stdout_lines)}`);
          return;
        }
        if (exp.stdout_lines_any_of) {
          const matches = exp.stdout_lines_any_of.some(cand =>
            JSON.stringify(actualLines) === JSON.stringify(cand)
          );
          assert.ok(matches, `${scenario.name}: stdout got ${JSON.stringify(actualLines)}, expected any of ${JSON.stringify(exp.stdout_lines_any_of)}`);
          return;
        }
        if (exp.stdout_lines_contains) {
          const joined = actualLines.join('\n');
          for (const needle of exp.stdout_lines_contains) {
            assert.ok(joined.includes(needle),
              `${scenario.name}: expected substring ${JSON.stringify(needle)} in stdout, got ${JSON.stringify(joined)}`);
          }
        }
      } finally {
        try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch {}
        try { fs.rmSync(binDir, { recursive: true, force: true }); } catch {}
      }
    });
  }
});
