'use strict';
/**
 * Issue #17 — concurrency gate, per-session serializer, env bootstrap,
 * public API wiring. The gate is process-wide and module-level, so these
 * tests drive configureConcurrency()/concurrencySnapshot() directly and
 * restore the default (unbounded) config in afterEach.
 */

const { test, describe, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  run,
  configureConcurrency,
  concurrencySnapshot,
  ConcurrencyRejected,
} = require('./runner.js');

function writeSelfTimingAgent(dir, extraBody) {
  // Emits its own flight-window markers into $ORDER_LOG so tests can assert
  // on real child-process windows (a caller entering run() is not the same
  // moment the gate slot is acquired).
  const file = path.join(dir, `agent-self-timing-${Date.now()}-${Math.random().toString(36).slice(2)}.sh`);
  fs.writeFileSync(
    file,
    '#!/usr/bin/env bash\n' +
      'echo "$APTAG-start $(date +%s.%N)" >> "$ORDER_LOG"\n' +
      (extraBody || '') + '\n' +
      "echo '{\"type\":\"result\",\"text\":\"done\"}'\n" +
      'echo "$APTAG-end $(date +%s.%N)" >> "$ORDER_LOG"\n',
    { mode: 0o755 }
  );
  return file;
}

function writeAgent(dir, sleepSecs) {
  const file = path.join(dir, `agent-${Date.now()}-${Math.random().toString(36).slice(2)}.sh`);
  fs.writeFileSync(
    file,
    '#!/usr/bin/env bash\n' +
      (sleepSecs ? `sleep ${sleepSecs}\n` : '') +
      "echo '{\"type\":\"result\",\"text\":\"done\"}'\n",
    { mode: 0o755 }
  );
  return file;
}

function readWindows(logPath) {
  const marks = {};
  for (const ln of fs.readFileSync(logPath, 'utf8').split('\n')) {
    const parts = ln.trim().split(/\s+/);
    if (parts.length === 2 && /-start$|-end$/.test(parts[0])) {
      const tag = parts[0].replace(/-start$|-end$/, '');
      const kind = parts[0].endsWith('-start') ? 'start' : 'end';
      marks[tag] = marks[tag] || {};
      marks[tag][kind] = parseFloat(parts[1]);
    }
  }
  return marks;
}

function assertWindowsDisjoint(logPath) {
  const marks = readWindows(logPath);
  const tags = Object.keys(marks);
  assert.strictEqual(tags.length, 2, `expected two agents' windows, got ${JSON.stringify(marks)}`);
  for (const t of tags) {
    assert.ok(marks[t].start && marks[t].end, `missing markers for ${t}`);
  }
  const [a, b] = tags;
  const firstEnd = marks[a].start < marks[b].start ? marks[a].end : marks[b].end;
  const secondStart = marks[a].start < marks[b].start ? marks[b].start : marks[a].start;
  assert.ok(
    secondStart >= firstEnd,
    `flight windows overlapped: ${JSON.stringify(marks)}`
  );
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(predicate, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(20);
  }
  return predicate();
}

describe('concurrency gate (issue #17)', () => {
  let dir;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-conc-'));
    configureConcurrency({ maxConcurrentRuns: null, overflow: 'wait', serializeSameSession: false });
  });

  afterEach(() => {
    configureConcurrency({ maxConcurrentRuns: null, overflow: 'wait', serializeSameSession: false });
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch { /* best effort */ }
  });

  test('unbounded by default — two runs in flight at once', async () => {
    const agent = writeAgent(dir, 0.35);
    const p1 = run({ command: agent }, { message: 'hi' });
    assert.ok(
      await waitFor(() => concurrencySnapshot().activeRuns === 1),
      'first run should be active'
    );
    const p2 = run({ command: agent }, { message: 'hi' });
    assert.ok(
      await waitFor(() => concurrencySnapshot().activeRuns === 2),
      'unbounded gate must let the second run start concurrently'
    );
    const [r1, r2] = await Promise.all([p1, p2]);
    assert.strictEqual(r1.reply, 'done');
    assert.strictEqual(r2.reply, 'done');
    assert.strictEqual(concurrencySnapshot().activeRuns, 0);
  });

  test('overflow reject — throws before spawning, no slot leak', async () => {
    configureConcurrency({ maxConcurrentRuns: 1, overflow: 'reject' });
    const agent = writeAgent(dir, 0.35);
    const p1 = run({ command: agent }, { message: 'hi' });
    assert.ok(await waitFor(() => concurrencySnapshot().activeRuns === 1));
    await assert.rejects(
      () => run({ command: agent }, { message: 'hi' }),
      (err) => {
        assert.ok(err instanceof ConcurrencyRejected);
        assert.strictEqual(err.maxConcurrentRuns, 1);
        return true;
      }
    );
    const r1 = await p1;
    assert.strictEqual(r1.reply, 'done');
    assert.strictEqual(concurrencySnapshot().activeRuns, 0, 'rejected run must not leak a slot');
  });

  test('overflow wait — serializes, flight windows disjoint', async () => {
    configureConcurrency({ maxConcurrentRuns: 1, overflow: 'wait' });
    const logPath = path.join(dir, 'order.log');
    const agent = writeSelfTimingAgent(dir, 'sleep 0.3');
    const p1 = run({ command: agent }, {
      message: 'hi',
      extraEnv: { APTAG: 'a', ORDER_LOG: logPath },
    });
    assert.ok(await waitFor(() => concurrencySnapshot().activeRuns === 1));
    const p2 = run({ command: agent }, {
      message: 'hi',
      extraEnv: { APTAG: 'b', ORDER_LOG: logPath },
    });
    const [r1, r2] = await Promise.all([p1, p2]);
    assert.strictEqual(r1.reply, 'done');
    assert.strictEqual(r2.reply, 'done');
    assertWindowsDisjoint(logPath);
    assert.strictEqual(concurrencySnapshot().activeRuns, 0);
  });

  test('serializeSameSession — same session disjoint, others parallel', async () => {
    configureConcurrency({ maxConcurrentRuns: 4, serializeSameSession: true });
    const logPath = path.join(dir, 'order.log');
    const agent = writeSelfTimingAgent(dir, 'sleep 0.3');

    const opts = (tag, sid) => ({
      message: 'hi',
      sessionId: sid,
      extraEnv: { APTAG: tag, ORDER_LOG: logPath },
    });
    // same session → serialized
    const p1 = run({ command: agent }, opts('a', 'chat-1'));
    assert.ok(await waitFor(() => concurrencySnapshot().activeRuns === 1));
    const p2 = run({ command: agent }, opts('b', 'chat-1'));
    const [r1, r2] = await Promise.all([p1, p2]);
    assert.strictEqual(r1.reply, 'done');
    assert.strictEqual(r2.reply, 'done');
    assertWindowsDisjoint(logPath);

    // different sessions → parallel (both active at once)
    const agent2 = writeAgent(dir, 0.4);
    const q1 = run({ command: agent2 }, { message: 'hi', sessionId: 'chat-9' });
    assert.ok(await waitFor(() => concurrencySnapshot().activeRuns === 1));
    const q2 = run({ command: agent2 }, { message: 'hi', sessionId: 'chat-8' });
    assert.ok(
      await waitFor(() => concurrencySnapshot().activeRuns === 2),
      'different sessions must run in parallel even with serialization on'
    );
    await Promise.all([q1, q2]);
    assert.strictEqual(concurrencySnapshot().sessionsInFlight, 0);
  });

  test('rejected run releases its session ticket', async () => {
    configureConcurrency({ maxConcurrentRuns: 1, overflow: 'reject', serializeSameSession: true });
    const agent = writeAgent(dir, 0.3);
    const p1 = run({ command: agent }, { message: 'hi', sessionId: 's1' });
    assert.ok(await waitFor(() => concurrencySnapshot().activeRuns === 1));
    // s2 hits the full global gate → rejected; its session ticket must be
    // released (otherwise the session table wedges)
    await assert.rejects(() => run({ command: agent }, { message: 'hi', sessionId: 's2' }), ConcurrencyRejected);
    await p1;
    assert.strictEqual(concurrencySnapshot().sessionsInFlight, 0);
    const r2 = await run({ command: agent }, { message: 'hi', sessionId: 's2' });
    assert.strictEqual(r2.reply, 'done');
  });

  test('env bootstrap — reject + max read once from process.env', async () => {
    process.env.AGENTPROC_MAX_CONCURRENT_RUNS = '1';
    process.env.AGENTPROC_CONCURRENCY_OVERFLOW = 'reject';
    process.env.AGENTPROC_SERIALIZE_SAME_SESSION = '1';
    // re-require to get a fresh module with un-bootstrapped gate state
    const freshPath = require.resolve('./runner.js');
    delete require.cache[freshPath];
    const fresh = require('./runner.js');
    const agent = writeAgent(dir, 0.3);
    const p1 = fresh.run({ command: agent }, { message: 'hi', sessionId: 'x' });
    assert.ok(await waitFor(() => fresh.concurrencySnapshot().activeRuns === 1));
    await assert.rejects(
      () => fresh.run({ command: agent }, { message: 'hi', sessionId: 'y' }),
      (err) => {
        // cross-module-instance class identity differs after re-require;
        // assert on the protocol-visible marker instead of instanceof
        assert.strictEqual(err.name, 'ConcurrencyRejected');
        assert.strictEqual(err.isConcurrencyRejected, true);
        assert.strictEqual(err.maxConcurrentRuns, 1);
        return true;
      }
    );
    const snap = fresh.concurrencySnapshot();
    assert.strictEqual(snap.maxConcurrentRuns, 1);
    assert.strictEqual(snap.overflow, 'reject');
    assert.strictEqual(snap.serializeSameSession, true);
    await p1;
    delete process.env.AGENTPROC_MAX_CONCURRENT_RUNS;
    delete process.env.AGENTPROC_CONCURRENCY_OVERFLOW;
    delete process.env.AGENTPROC_SERIALIZE_SAME_SESSION;
  });

  test('env garbage stays unbounded (no crash)', async () => {
    process.env.AGENTPROC_MAX_CONCURRENT_RUNS = 'not-a-number';
    const freshPath = require.resolve('./runner.js');
    delete require.cache[freshPath];
    const fresh = require('./runner.js');
    const snap = fresh.concurrencySnapshot();
    assert.strictEqual(snap.maxConcurrentRuns, null);
    delete process.env.AGENTPROC_MAX_CONCURRENT_RUNS;
  });

  test('configureConcurrency validation', () => {
    assert.throws(() => configureConcurrency({ overflow: 'explode' }), /overflow/);
    assert.throws(() => configureConcurrency({ maxConcurrentRuns: 0 }), /maxConcurrentRuns/);
    assert.throws(() => configureConcurrency({ maxConcurrentRuns: -3 }), /maxConcurrentRuns/);
  });

  test('index.js re-exports the gate API', () => {
    const idx = require('./index.js');
    assert.strictEqual(typeof idx.configureConcurrency, 'function');
    assert.strictEqual(typeof idx.concurrencySnapshot, 'function');
    assert.strictEqual(typeof idx.ConcurrencyRejected, 'function');
  });
});
