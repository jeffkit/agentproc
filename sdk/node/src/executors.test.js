'use strict';
/**
 * Tests for executors.js — the built-in in-process executor registry (SDK 0.10).
 *
 * Run with: `node --test src/executors.test.js`
 *
 * Three coverage areas:
 *   1. Registry shape — all expected names present, required fields exist.
 *   2. buildArgs (pure-function) — spot-check key executors for correct argv.
 *   3. runViaExecutor (integration) — spawn real processes to verify the plain
 *      and NDJSON executor paths, including session-id propagation for agy.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { EXECUTORS, executorNames } = require('./executors.js');
const { runViaExecutor, normalizeProfile, isValidSessionId } = require('./runner.js');

// ---------------------------------------------------------------------------
// 1. Registry shape
// ---------------------------------------------------------------------------

describe('EXECUTORS registry', () => {
  const EXPECTED_NAMES = [
    'claude-code', 'codebuddy', 'codex', 'cursor',
    'gemini-cli', 'kimi-code', 'opencode', 'qwen-code',
    'agy', 'aider', 'deepseek', 'pi',
  ];

  test('all expected executors are registered', () => {
    for (const name of EXPECTED_NAMES) {
      assert.ok(EXECUTORS[name], `missing executor: ${name}`);
    }
  });

  test('executorNames matches EXECUTORS keys', () => {
    assert.deepStrictEqual(new Set(executorNames), new Set(Object.keys(EXECUTORS)));
  });

  test('every executor has cliName and installHint', () => {
    for (const [name, exec] of Object.entries(EXECUTORS)) {
      assert.ok(typeof exec.cliName === 'string' && exec.cliName, `${name}: missing cliName`);
      assert.ok(typeof exec.installHint === 'string', `${name}: missing installHint`);
    }
  });

  test('every executor has buildArgs or makeHandlers', () => {
    for (const [name, exec] of Object.entries(EXECUTORS)) {
      const hasBuild = typeof exec.buildArgs === 'function';
      const hasMake = typeof exec.makeHandlers === 'function';
      assert.ok(hasBuild || hasMake, `${name}: must have buildArgs or makeHandlers`);
    }
  });

  test('plain executors have plain: true', () => {
    const plainNames = ['agy', 'aider', 'deepseek', 'pi'];
    for (const name of plainNames) {
      assert.strictEqual(EXECUTORS[name].plain, true, `${name}: expected plain: true`);
    }
  });

  test('NDJSON executors have plain: false or omit it', () => {
    const ndjsonNames = ['claude-code', 'codebuddy', 'codex', 'gemini-cli', 'opencode', 'qwen-code'];
    for (const name of ndjsonNames) {
      assert.ok(!EXECUTORS[name].plain, `${name}: expected plain to be falsy`);
    }
  });
});

// ---------------------------------------------------------------------------
// 2. buildArgs — pure-function tests (no subprocess)
// ---------------------------------------------------------------------------

describe('buildArgs — claude-code', () => {
  const { buildArgs } = EXECUTORS['claude-code'];

  test('new session — no --resume', () => {
    const args = buildArgs('hello', '', {});
    assert.ok(args.includes('claude'));
    assert.ok(args.includes('-p'));
    assert.ok(args.includes('hello'));
    assert.ok(!args.includes('--resume'), 'should not include --resume for empty sessionId');
  });

  test('resume session — includes --resume <id>', () => {
    const args = buildArgs('hello', 'sess-abc', {});
    const idx = args.indexOf('--resume');
    assert.ok(idx !== -1, 'should include --resume');
    assert.strictEqual(args[idx + 1], 'sess-abc');
  });

  test('CLAUDE_MODEL env var adds --model', () => {
    const args = buildArgs('hi', '', { CLAUDE_MODEL: 'claude-opus-4' });
    const idx = args.indexOf('--model');
    assert.ok(idx !== -1);
    assert.strictEqual(args[idx + 1], 'claude-opus-4');
  });

  test('empty CLAUDE_MODEL does not add --model', () => {
    const args = buildArgs('hi', '', { CLAUDE_MODEL: '' });
    assert.ok(!args.includes('--model'));
  });
});

describe('buildArgs — codex', () => {
  const { buildArgs } = EXECUTORS['codex'];

  test('new session uses exec command', () => {
    const args = buildArgs('hello', '', {});
    assert.ok(args.includes('codex'));
    assert.ok(args.includes('exec'));
    assert.ok(!args.includes('resume'));
  });

  test('resume session uses exec resume command', () => {
    const args = buildArgs('hello', 'thread-123', {});
    assert.ok(args.includes('exec'));
    assert.ok(args.includes('resume'));
    assert.ok(args.includes('thread-123'));
  });
});

describe('buildArgs — aider (plain, no session)', () => {
  const { buildArgs } = EXECUTORS['aider'];

  test('does not include any session flag', () => {
    const args = buildArgs('hello', 'some-session', {});
    const joined = args.join(' ');
    assert.ok(!joined.includes('session'), 'aider should not pass session to CLI');
    assert.ok(!joined.includes('conversation'), 'aider should not pass conversation to CLI');
    assert.ok(args.includes('aider'));
    assert.ok(args.includes('--message'));
  });
});

describe('buildArgs — deepseek (plain, no session)', () => {
  const { buildArgs } = EXECUTORS['deepseek'];

  test('does not include any session flag', () => {
    const args = buildArgs('hello', 'some-session', {});
    const joined = args.join(' ');
    assert.ok(!joined.includes('session'));
    assert.ok(!joined.includes('conversation'));
    assert.ok(args.includes('deepseek'));
  });
});

describe('buildArgs — pi (plain, no session)', () => {
  const { buildArgs } = EXECUTORS['pi'];

  test('does not include any session flag', () => {
    const args = buildArgs('hello', 'some-session', {});
    const joined = args.join(' ');
    assert.ok(!joined.includes('session'));
    assert.ok(!joined.includes('conversation'));
    assert.ok(args.includes('pi'));
  });
});

describe('agy executor — session management via makeHandlers', () => {
  test('has makeHandlers (not plain buildArgs)', () => {
    assert.ok(typeof EXECUTORS['agy'].makeHandlers === 'function');
    assert.ok(!EXECUTORS['agy'].buildArgs, 'agy should use makeHandlers, not a top-level buildArgs');
  });

  test('makeHandlers returns { buildArgs, getSessionId }', () => {
    const h = EXECUTORS['agy'].makeHandlers();
    assert.ok(typeof h.buildArgs === 'function');
    assert.ok(typeof h.getSessionId === 'function');
  });

  test('with empty sessionId: generates a UUID and passes --conversation', () => {
    const h = EXECUTORS['agy'].makeHandlers();
    const args = h.buildArgs('hello', '', {});
    const idx = args.indexOf('--conversation');
    assert.ok(idx !== -1, 'should pass --conversation even when session is empty');
    const generatedId = args[idx + 1];
    assert.ok(generatedId, 'generated id should be non-empty');
    assert.ok(isValidSessionId(generatedId), 'generated id should be a valid session id');
    assert.strictEqual(h.getSessionId(), generatedId, 'getSessionId() should return the same id');
  });

  test('with existing sessionId: uses it, does not generate a new one', () => {
    const h = EXECUTORS['agy'].makeHandlers();
    const args = h.buildArgs('hello', 'existing-sess-id', {});
    const idx = args.indexOf('--conversation');
    assert.ok(idx !== -1);
    assert.strictEqual(args[idx + 1], 'existing-sess-id');
    assert.strictEqual(h.getSessionId(), 'existing-sess-id');
  });

  test('each makeHandlers() call is independent (per-turn isolation)', () => {
    const h1 = EXECUTORS['agy'].makeHandlers();
    const h2 = EXECUTORS['agy'].makeHandlers();
    h1.buildArgs('hi', '', {});
    h2.buildArgs('hi', '', {});
    const id1 = h1.getSessionId();
    const id2 = h2.getSessionId();
    assert.notStrictEqual(id1, id2, 'separate handler instances should not share state');
  });

  test('passes --print flag', () => {
    const h = EXECUTORS['agy'].makeHandlers();
    const args = h.buildArgs('hello', '', {});
    assert.ok(args.includes('--print'));
  });

  test('AGY_MODEL env adds --model', () => {
    const h = EXECUTORS['agy'].makeHandlers();
    const args = h.buildArgs('hi', '', { AGY_MODEL: 'claude-3-5' });
    const idx = args.indexOf('--model');
    assert.ok(idx !== -1);
    assert.strictEqual(args[idx + 1], 'claude-3-5');
  });
});

describe('kimi-code executor — stateful makeHandlers (NDJSON)', () => {
  test('has makeHandlers', () => {
    assert.ok(typeof EXECUTORS['kimi-code'].makeHandlers === 'function');
  });

  test('new session: generates a UUID as --session arg', () => {
    const h = EXECUTORS['kimi-code'].makeHandlers();
    const args = h.buildArgs('hello', '', {});
    const idx = args.indexOf('--session');
    assert.ok(idx !== -1);
    const id = args[idx + 1];
    assert.ok(isValidSessionId(id));
  });

  test('resume session: reuses provided id', () => {
    const h = EXECUTORS['kimi-code'].makeHandlers();
    const args = h.buildArgs('hello', 'kimi-sess-abc', {});
    const idx = args.indexOf('--session');
    assert.ok(idx !== -1);
    assert.strictEqual(args[idx + 1], 'kimi-sess-abc');
  });
});

// ---------------------------------------------------------------------------
// 3. runViaExecutor — integration tests (spawn real processes)
// ---------------------------------------------------------------------------

function tmpScript(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-exec-'));
  const file = path.join(dir, 'mock.sh');
  fs.writeFileSync(file, content, { mode: 0o755 });
  return file;
}

function mockProfile() {
  return normalizeProfile({ command: 'dummy', executor: 'test' });
}

describe('runViaExecutor — plain executor', () => {
  test('reply is trimmed stdout on success', async () => {
    const cli = tmpScript('#!/usr/bin/env bash\necho "hello world"\n');
    const executor = {
      cliName: 'mock',
      installHint: '',
      plain: true,
      buildArgs: (message) => [cli],
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.strictEqual(r.reply, 'hello world');
    assert.strictEqual(r.error, '');
    assert.strictEqual(r.exitCode, 0);
  });

  test('sessionId is empty when executor has no getSessionId', async () => {
    const cli = tmpScript('#!/usr/bin/env bash\necho "plain response"\n');
    const executor = {
      cliName: 'mock',
      installHint: '',
      plain: true,
      buildArgs: () => [cli],
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.strictEqual(r.sessionId, '', 'plain executor without getSessionId → empty sessionId');
  });

  test('getSessionId result propagates to RunResult.sessionId', async () => {
    const cli = tmpScript('#!/usr/bin/env bash\necho "response text"\n');
    const executor = {
      cliName: 'mock',
      installHint: '',
      plain: true,
      makeHandlers() {
        const session = { id: 'test-session-id-42' };
        return {
          buildArgs: () => [cli],
          getSessionId: () => session.id,
        };
      },
    };
    const profile = mockProfile();
    const sessions = [];
    const r = await runViaExecutor(
      profile,
      { message: 'hi', onSession: (s) => sessions.push(s) },
      executor,
    );
    assert.strictEqual(r.sessionId, 'test-session-id-42');
    assert.deepStrictEqual(sessions, ['test-session-id-42']);
  });

  test('non-zero exit → error result', async () => {
    const cli = tmpScript('#!/usr/bin/env bash\necho "error info" >&2\nexit 2\n');
    const executor = {
      cliName: 'mock',
      installHint: '',
      plain: true,
      buildArgs: () => [cli],
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.ok(r.error, 'should have an error message');
    assert.notStrictEqual(r.exitCode, 0);
  });

  test('empty stdout → error result', async () => {
    const cli = tmpScript('#!/usr/bin/env bash\n# silent\n');
    const executor = {
      cliName: 'mock',
      installHint: '',
      plain: true,
      buildArgs: () => [cli],
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.ok(r.error.includes('empty output'));
  });

  test('ENOENT CLI → error result with installHint', async () => {
    const executor = {
      cliName: 'definitely-not-a-real-cli-xyz',
      installHint: 'run: install xyz',
      plain: true,
      buildArgs: () => ['definitely-not-a-real-cli-xyz', '--print', 'hi'],
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.ok(r.error, 'should have an error message');
    assert.notStrictEqual(r.exitCode, 0);
  });
});

describe('runViaExecutor — NDJSON executor', () => {
  test('parseEvent result → reply', async () => {
    const cli = tmpScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"result","text":"hello from ndjson"}\'\n',
    );
    const executor = {
      cliName: 'mock-ndjson',
      installHint: '',
      plain: false,
      buildArgs: () => [cli],
      parseEvent(event) {
        if (event.type === 'result') return { finalText: event.text };
        return null;
      },
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.strictEqual(r.reply, 'hello from ndjson');
    assert.strictEqual(r.error, '');
  });

  test('parseEvent sessionId → RunResult.sessionId', async () => {
    const cli = tmpScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"result","text":"ok","session_id":"ndjson-sess-1"}\'\n',
    );
    const executor = {
      cliName: 'mock-ndjson',
      installHint: '',
      plain: false,
      buildArgs: () => [cli],
      parseEvent(event) {
        if (event.type === 'result') return { finalText: event.text, sessionId: event.session_id };
        return null;
      },
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.strictEqual(r.sessionId, 'ndjson-sess-1');
  });

  test('parseEvent partialText → onPartial called, reply empty', async () => {
    const cli = tmpScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"chunk","text":"part1"}\'\n' +
      'echo \'{"type":"chunk","text":"part2"}\'\n',
    );
    const partials = [];
    const executor = {
      cliName: 'mock-ndjson',
      installHint: '',
      plain: false,
      buildArgs: () => [cli],
      parseEvent(event) {
        if (event.type === 'chunk') return { partialText: event.text };
        return null;
      },
    };
    const profile = mockProfile();
    const r = await runViaExecutor(
      profile,
      { message: 'hi', onPartial: (t) => partials.push(t) },
      executor,
    );
    assert.deepStrictEqual(partials, ['part1', 'part2']);
    assert.strictEqual(r.reply, '');
  });

  test('parseEvent error → result.error', async () => {
    const cli = tmpScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"fail","message":"rate limited"}\'\n',
    );
    const executor = {
      cliName: 'mock-ndjson',
      installHint: '',
      plain: false,
      buildArgs: () => [cli],
      parseEvent(event) {
        if (event.type === 'fail') return { error: event.message };
        return null;
      },
    };
    const profile = mockProfile();
    const r = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.strictEqual(r.error, 'rate limited');
    assert.notStrictEqual(r.exitCode, 0);
  });

  test('makeHandlers per-turn isolation for NDJSON executors', async () => {
    const cli = tmpScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"done","text":"ok"}\'\n',
    );
    const callCount = { n: 0 };
    const executor = {
      cliName: 'mock-ndjson',
      installHint: '',
      plain: false,
      makeHandlers() {
        callCount.n += 1;
        const myId = callCount.n;
        return {
          buildArgs: () => [cli],
          parseEvent(event) {
            if (event.type === 'done') return { finalText: `${event.text}-${myId}` };
            return null;
          },
        };
      },
    };
    const profile = mockProfile();
    const r1 = await runViaExecutor(profile, { message: 'hi' }, executor);
    const r2 = await runViaExecutor(profile, { message: 'hi' }, executor);
    assert.strictEqual(callCount.n, 2, 'makeHandlers should be called once per turn');
    assert.strictEqual(r1.reply, 'ok-1');
    assert.strictEqual(r2.reply, 'ok-2');
  });
});
