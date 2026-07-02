'use strict';
/**
 * Tests for agentproc.
 *
 * Run with: `node --test src/index.test.js`
 *
 * Strategy: the SDK calls process.exit() at the end of createProfile, which is
 * hard to test in-process. So we split tests into two groups:
 *
 *   1. Pure-function tests (loadHistory, appendHistory, sessionFilePath,
 *      parseAttachments) — run in-process, assert on return values.
 *
 *   2. createProfile end-to-end tests — spawn a child node process that sets
 *      AGENT_* env vars, requires the SDK, and writes the captured output
 *      back to us over stdout (we intercept process.exit via a small shim).
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
    // No file created.
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

describe('attachments removed in 0.5.0', () => {
  test('parseAttachments and Attachment are no longer exported', () => {
    assert.strictEqual(SDK.parseAttachments, undefined);
    assert.strictEqual(SDK.Attachment, undefined);
  });
});

test('PROTOCOL_VERSION is "0.1"', () => {
  assert.strictEqual(SDK.PROTOCOL_VERSION, '0.1');
});

// ---------------------------------------------------------------------------
// 2. createProfile end-to-end tests
// ---------------------------------------------------------------------------

/**
 * Spawn a child node process that:
 *   - sets AGENT_* env vars
 *   - loads the SDK
 *   - runs the given handler body
 *
 * The SDK calls process.exit(); Node flushes stdout on exit, so we observe
 * the full output via the close event. No process.exit stubbing needed.
 *
 * Returns a promise resolving to { stdout, stderr, code }.
 */
function runAgent(env, handlerSrc) {
  return new Promise((resolve, reject) => {
    const bootstrap = `
      const sdk = require(${JSON.stringify(SDK_PATH)});
      (${handlerSrc})(sdk).catch(e => {
        process.stderr.write('HANDLER_ERROR: ' + (e && e.stack || e) + '\\n');
        if (!process.exitCode) process.exitCode = 1;
      });
    `;
    const child = spawn(process.execPath, ['-e', bootstrap], {
      env: { ...process.env, ...env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', d => { stdout += d.toString(); });
    child.stderr.on('data', d => { stderr += d.toString(); });
    child.on('error', reject);
    child.on('close', code => resolve({ stdout, stderr, code }));
  });
}

describe('createProfile end-to-end', () => {
  test('returns a plain string → emitted as reply body', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '0' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return 'You said: ' + ctx.message;
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('You said: hi\n'), 'stdout=' + JSON.stringify(r.stdout));
  });

  test('returns AgentResult with session_id → AGENT_SESSION: line emitted', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '0' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return { response: 'ok', sessionId: 'sess-123' };
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('AGENT_SESSION:sess-123\n'), 'stdout=' + JSON.stringify(r.stdout));
    assert.ok(r.stdout.includes('ok\n'));
  });

  test('sendPartial emits AGENT_PARTIAL: lines', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '1' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          await ctx.sendPartial('chunk 1');
          await ctx.sendPartial('chunk 2');
          return { response: '', sessionId: 's1' };
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('AGENT_PARTIAL:"chunk 1"\n'), 'stdout=' + JSON.stringify(r.stdout));
    assert.ok(r.stdout.includes('AGENT_PARTIAL:"chunk 2"\n'));
    assert.ok(r.stdout.includes('AGENT_SESSION:s1\n'));
  });

  test('sendError emits AGENT_ERROR: line', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '0' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          await ctx.sendError('rate limited; retry in 60s');
          return { response: 'should be discarded by bridge', sessionId: '' };
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(
      r.stdout.includes('AGENT_ERROR:"rate limited; retry in 60s"\n'),
      'stdout=' + JSON.stringify(r.stdout)
    );
  });

  test('protocolError surfaces as AGENT_ERROR + exit 1', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '0' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          throw sdk.protocolError('bad input');
        });
      })`
    );
    assert.strictEqual(r.code, 1, 'stdout=' + r.stdout + ' stderr=' + r.stderr);
    assert.ok(r.stdout.includes('AGENT_ERROR:"bad input"\n'), 'stdout=' + JSON.stringify(r.stdout));
  });

  test('legacy `throw await sdk.protocolError(...)` still works', async () => {
    // The old idiom awaited the helper. Awaiting a non-thenable Error instance
    // returns the instance, so this form must keep functioning after the
    // switch from an async-throwing function to a synchronous factory.
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '0' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          throw await sdk.protocolError('legacy form');
        });
      })`
    );
    assert.strictEqual(r.code, 1, 'stdout=' + r.stdout + ' stderr=' + r.stderr);
    assert.ok(r.stdout.includes('AGENT_ERROR:"legacy form"\n'), 'stdout=' + JSON.stringify(r.stdout));
  });

  test('handler exception → exit 1, stderr has stack', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '0' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          throw new Error('boom');
        });
      })`
    );
    assert.strictEqual(r.code, 1);
    assert.ok(r.stderr.includes('boom'), 'stderr=' + JSON.stringify(r.stderr));
    assert.ok(!r.stdout.includes('AGENT_ERROR'), 'should NOT emit AGENT_ERROR for generic errors');
  });

  test('context carries AGENT_* env vars', async () => {
    const r = await runAgent(
      {
        AGENT_MESSAGE: 'hello',
        AGENT_SESSION_ID: 'prev-sess',
        AGENT_SESSION_NAME: 'work',
        AGENT_FROM_USER: 'u123',
        AGENT_STREAMING: '0',
        AGENT_IMAGE_URL: 'https://x/img.png',
        AGENT_FILE_URL: 'https://y/file.pdf',
      },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return JSON.stringify({
            msg: ctx.message,
            sid: ctx.sessionId,
            sname: ctx.sessionName,
            from: ctx.fromUser,
            stream: ctx.streaming,
            img: ctx.imageUrl,
            file: ctx.fileUrl,
          });
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    const parsed = JSON.parse(r.stdout.trim());
    assert.strictEqual(parsed.msg, 'hello');
    assert.strictEqual(parsed.sid, 'prev-sess');
    assert.strictEqual(parsed.sname, 'work');
    assert.strictEqual(parsed.from, 'u123');
    assert.strictEqual(parsed.stream, false);
    assert.strictEqual(parsed.img, 'https://x/img.png');
    assert.strictEqual(parsed.file, 'https://y/file.pdf');
  });

  test('handler can return undefined (signaled everything via partials)', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '1' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          await ctx.sendPartial('only partial');
          // no return → undefined
        });
      })`
    );
    assert.strictEqual(r.code, 0, 'stderr=' + r.stderr);
    assert.ok(r.stdout.includes('AGENT_PARTIAL:"only partial"\n'));
    // No trailing garbage
    assert.ok(!r.stdout.includes('undefined'));
  });

  test('default protocolVersion is 0.1', async () => {
    const r = await runAgent(
      { AGENT_MESSAGE: 'hi', AGENT_STREAMING: '0' },
      `(async (sdk) => {
        sdk.createProfile(async (ctx) => {
          return 'pv=' + ctx.protocolVersion;
        });
      })`
    );
    assert.strictEqual(r.code, 0);
    assert.ok(r.stdout.includes('pv=0.1'));
  });
});
