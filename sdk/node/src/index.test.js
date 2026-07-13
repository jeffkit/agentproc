'use strict';
/**
 * Tests for agentproc (agent-side SDK entry, wire 0.3).
 *
 * Run with: `node --test src/index.test.js`
 *
 * Strategy: the SDK calls process.exit() at the end of createProfile, which is
 * hard to test in-process. So we split tests into two groups:
 *
 *   1. Pure-function tests (loadHistory, appendHistory, sessionFilePath) — run
 *      in-process, assert on return values.
 *
 *   2. createProfile end-to-end tests — spawn a child node process, write a
 *      {"type":"turn",...} object to its stdin, and let the handler run under
 *      createProfile. The SDK emits NDJSON events on stdout and calls
 *      process.exit(); Node flushes stdout on exit, so we observe the full
 *      output via the close event. No process.exit stubbing needed.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const SDK = require('./index.js');
const SDK_PATH = require.resolve('./index.js');

// ---------------------------------------------------------------------------
// 1. Pure-function tests
// ---------------------------------------------------------------------------

describe('sessionFilePath', () => {
  test('returns a path under the default session dir', () => {
    const p = SDK.sessionFilePath('abc123');
    assert.match(p, /\.agentproc[\\/]+sessions[\\/]+abc123\.jsonl$/);
  });

  test('respects the sessionDir argument', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-test-'));
    const p = SDK.sessionFilePath('xyz', tmp);
    assert.strictEqual(p, path.join(tmp, 'xyz.jsonl'));
  });

  test('throws on empty sessionId', () => {
    assert.throws(() => SDK.sessionFilePath(''), /sessionId must be non-empty/);
  });

  test('rejects path-traversal ids (defense in depth)', () => {
    assert.throws(() => SDK.sessionFilePath('a/b'), /safe filename component/);
    assert.throws(() => SDK.sessionFilePath('a\\b'), /safe filename component/);
    assert.throws(() => SDK.sessionFilePath('..'), /safe filename component/);
    assert.throws(() => SDK.sessionFilePath('../../tmp/x'), /safe filename component/);
  });

  test('accepts legitimate ids that contain `..` (no false positive)', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-test-'));
    const p = SDK.sessionFilePath('a..b', tmp);
    assert.strictEqual(p, path.join(tmp, 'a..b.jsonl'));
  });
});

describe('loadHistory / appendHistory', () => {
  test('loadHistory returns [] for empty sessionId', () => {
    assert.deepStrictEqual(SDK.loadHistory(''), []);
  });

  test('loadHistory returns [] when file does not exist', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-test-'));
    assert.deepStrictEqual(SDK.loadHistory('never-existed', tmp), []);
  });

  test('appendHistory → loadHistory round trip', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-test-'));
    SDK.appendHistory('s1', [
      { role: 'user', content: 'hello' },
      { role: 'assistant', content: 'hi' },
    ], tmp);
    const loaded = SDK.loadHistory('s1', tmp);
    assert.strictEqual(loaded.length, 2);
    assert.strictEqual(loaded[0].role, 'user');
    assert.strictEqual(loaded[0].content, 'hello');
    assert.ok(loaded[0].timestamp, 'timestamp should be set');
    assert.strictEqual(loaded[1].role, 'assistant');
  });

  test('appendHistory is no-op when sessionId is empty', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-test-'));
    SDK.appendHistory('', [{ role: 'user', content: 'x' }], tmp);
    assert.strictEqual(fs.readdirSync(tmp).length, 0);
  });

  test('appendHistory is no-op when entries is empty', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-test-'));
    SDK.appendHistory('s1', [], tmp);
    assert.strictEqual(fs.readdirSync(tmp).length, 0);
  });

  test('loadHistory skips malformed JSON lines', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-test-'));
    const file = path.join(tmp, 's1.jsonl');
    fs.writeFileSync(file, [
      JSON.stringify({ role: 'user', content: 'ok', timestamp: 't1' }),
      'this is not json',
      JSON.stringify({ role: 'assistant', content: 'still ok', timestamp: 't2' }),
    ].join('\n') + '\n');
    const loaded = SDK.loadHistory('s1', tmp);
    assert.strictEqual(loaded.length, 2);
  });
});

describe('attachments (wire 0.3)', () => {
  test('no parseAttachments / Attachment helpers are exported — attachments live in the turn', () => {
    // Wire 0.3 carries attachments as `turn.attachments` (read by createProfile
    // from stdin); there is no separate parser helper on the SDK surface.
    assert.strictEqual(SDK.parseAttachments, undefined);
    assert.strictEqual(SDK.Attachment, undefined);
  });
});

test('PROTOCOL_VERSION is "0.3"', () => {
  assert.strictEqual(SDK.PROTOCOL_VERSION, '0.3');
});

// ---------------------------------------------------------------------------
// 2. createProfile end-to-end tests
// ---------------------------------------------------------------------------

/**
 * Spawn a child node process that:
 *   - loads the SDK
 *   - runs the given handler body under createProfile
 *   - reads the turn object from stdin (written by us as one NDJSON line)
 *
 * The SDK calls process.exit(); Node flushes stdout on exit, so we observe
 * the full output via the close event. No process.exit stubbing needed.
 *
 * Returns a promise resolving to { stdout, stderr, code }.
 */
function runAgent(turn, handlerSrc) {
  return new Promise((resolve, reject) => {
    const bootstrap = `
      const sdk = require(${JSON.stringify(SDK_PATH)});
      (${handlerSrc})(sdk).catch(e => {
        process.stderr.write('HANDLER_ERROR: ' + (e && e.stack || e) + '\\n');
        if (!process.exitCode) process.exitCode = 1;
      });
    `;
    const child = spawn(process.execPath, ['-e', bootstrap], {
      env: { PATH: process.env.PATH, HOME: process.env.HOME },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', d => { stdout += d.toString(); });
    child.stderr.on('data', d => { stderr += d.toString(); });
    child.on('error', reject);
    child.on('close', code => resolve({ stdout, stderr, code }));
    child.stdin.write(JSON.stringify(turn) + '\n');
    child.stdin.end();
  });
}

function turn(extra = {}) {
  return { type: 'turn', message: 'hi', session_id: '', session_name: 'default', from_user: '', protocol_version: '0.3', ...extra };
}

describe('createProfile end-to-end', () => {
  test('returns a plain string → emitted as a {"type":"text"} event', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return 'You said: ' + ctx.message;
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('{"type":"text","text":"You said: hi"}\n'), 'stdout=' + JSON.stringify(r.stdout));
  });

  test('returns AgentResult with session_id → session + text events emitted', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return { response: 'ok', sessionId: 'sess-123' };
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('{"type":"session","id":"sess-123"}\n'), 'stdout=' + JSON.stringify(r.stdout));
    assert.ok(r.stdout.includes('{"type":"text","text":"ok"}\n'));
  });

  test('sendPartial emits {"type":"partial"} events', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          await ctx.sendPartial('chunk 1');
          await ctx.sendPartial('chunk 2');
          return { response: '', sessionId: 's1' };
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('{"type":"partial","text":"chunk 1"}\n'), 'stdout=' + JSON.stringify(r.stdout));
    assert.ok(r.stdout.includes('{"type":"partial","text":"chunk 2"}\n'));
    assert.ok(r.stdout.includes('{"type":"session","id":"s1"}\n'));
  });

  test('sendPartial with role emits the role field', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          await ctx.sendPartial('thinking...', 'thinking');
          return 'answer';
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('{"type":"partial","text":"thinking...","role":"thinking"}\n'), 'stdout=' + JSON.stringify(r.stdout));
  });

  test('sendError emits {"type":"error"} event', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          await ctx.sendError('rate limited; retry in 60s');
          return { response: 'should be discarded by bridge', sessionId: '' };
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(
      r.stdout.includes('{"type":"error","message":"rate limited; retry in 60s"}\n'),
      'stdout=' + JSON.stringify(r.stdout)
    );
  });

  test('protocolError surfaces as {"type":"error"} + exit 1', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          throw sdk.protocolError('bad input');
        });
      })`
    );
    assert.strictEqual(r.code, 1, 'stdout=' + r.stdout + ' stderr=' + r.stderr);
    assert.ok(r.stdout.includes('{"type":"error","message":"bad input"}\n'), 'stdout=' + JSON.stringify(r.stdout));
  });

  test('legacy `throw await sdk.protocolError(...)` still works', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          throw await sdk.protocolError('legacy form');
        });
      })`
    );
    assert.strictEqual(r.code, 1, 'stdout=' + r.stdout + ' stderr=' + r.stderr);
    assert.ok(r.stdout.includes('{"type":"error","message":"legacy form"}\n'), 'stdout=' + JSON.stringify(r.stdout));
  });

  test('handler exception → exit 1, stderr has stack, no error event emitted', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          throw new Error('boom');
        });
      })`
    );
    assert.strictEqual(r.code, 1);
    assert.ok(r.stderr.includes('boom'), 'stderr=' + JSON.stringify(r.stderr));
    // Generic exceptions are NOT mapped to a {"type":"error"} event — only
    // ProtocolError is. The bridge sees a non-zero exit + stderr stack.
    assert.ok(!r.stdout.includes('"type":"error"'), 'should NOT emit an error event for generic errors');
  });

  test('context carries turn fields (message, session, from_user, attachments)', async () => {
    const r = await runAgent(
      turn({ session_id: 'prev-sess', session_name: 'work', from_user: 'u123', attachments: [{ kind: 'image', url: 'https://x/img.png' }] }),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return JSON.stringify({
            msg: ctx.message,
            sid: ctx.sessionId,
            sname: ctx.sessionName,
            from: ctx.fromUser,
            pv: ctx.protocolVersion,
            atts: (ctx.attachments || []).map(a => a.kind + ':' + a.url),
          });
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    // The returned string is wrapped in a {"type":"text"} event; parse it out.
    const line = r.stdout.split('\n').find(l => l.includes('"type":"text"'));
    assert.ok(line, 'no text event in stdout: ' + r.stdout);
    const parsed = JSON.parse(line);
    const ctx = JSON.parse(parsed.text);
    assert.strictEqual(ctx.msg, 'hi');
    assert.strictEqual(ctx.sid, 'prev-sess');
    assert.strictEqual(ctx.sname, 'work');
    assert.strictEqual(ctx.from, 'u123');
    assert.strictEqual(ctx.pv, '0.3');
    assert.deepStrictEqual(ctx.atts, ['image:https://x/img.png']);
  });

  test('handler can return undefined (signaled everything via partials)', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          await ctx.sendPartial('only partial');
          // no return → undefined
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('{"type":"partial","text":"only partial"}\n'));
    // No trailing garbage
    assert.ok(!r.stdout.includes('undefined'));
  });

  test('default protocolVersion is 0.3', async () => {
    const r = await runAgent(
      turn(),
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return 'pv=' + ctx.protocolVersion;
        });
      })`
    );
    assert.strictEqual(r.code, 0);
    assert.ok(r.stdout.includes('pv=0.3'));
  });
});
