'use strict';
/**
 * Tests for runner.js — the AgentProc canonical bridge implementation.
 *
 * Run with: `node --test src/runner.test.js`
 *
 * Strategy:
 *   1. Pure-function tests: classifyLine, decodeJsonValue, substitute,
 *      normalizeProfile, expandEnvRef — no subprocess.
 *   2. run() end-to-end tests: spawn a tiny bash/node helper script that
 *      emits protocol lines, assert the runner classifies them correctly.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  run,
  classifyLine,
  decodeJsonValue,
  substitute,
  normalizeProfile,
  expandEnvRef,
  expandPath,
  PROTOCOL_VERSION,
} = require('./runner.js');

// ---------------------------------------------------------------------------
// 1. Pure-function tests
// ---------------------------------------------------------------------------

describe('classifyLine', () => {
  test('identifies AGENT_SESSION:', () => {
    assert.deepStrictEqual(classifyLine('AGENT_SESSION:abc-123'), { kind: 'session', value: 'abc-123' });
  });

  test('strips whitespace from session id', () => {
    assert.deepStrictEqual(classifyLine('AGENT_SESSION:  abc-123  '), { kind: 'session', value: 'abc-123' });
  });

  test('identifies AGENT_PARTIAL: with JSON string', () => {
    assert.deepStrictEqual(classifyLine('AGENT_PARTIAL:"hello"'), { kind: 'partial', value: 'hello' });
  });

  test('AGENT_PARTIAL: with newline in JSON', () => {
    assert.deepStrictEqual(classifyLine('AGENT_PARTIAL:"line1\\nline2"'), { kind: 'partial', value: 'line1\nline2' });
  });

  test('AGENT_PARTIAL: lenient on bad JSON — treats as raw text', () => {
    assert.deepStrictEqual(classifyLine('AGENT_PARTIAL:not json'), { kind: 'partial', value: 'not json' });
  });

  test('AGENT_PARTIAL: empty value', () => {
    assert.deepStrictEqual(classifyLine('AGENT_PARTIAL:'), { kind: 'partial', value: '' });
  });

  test('identifies AGENT_ERROR:', () => {
    assert.deepStrictEqual(classifyLine('AGENT_ERROR:"rate limited"'), { kind: 'error', value: 'rate limited' });
  });

  test('AGENT_ERROR: lenient on bad JSON', () => {
    assert.deepStrictEqual(classifyLine('AGENT_ERROR:boom'), { kind: 'error', value: 'boom' });
  });

  test('body line: anything else', () => {
    assert.deepStrictEqual(classifyLine('hello world'), { kind: 'body', value: 'hello world' });
  });

  test('body line: line starting with space is NOT a protocol line', () => {
    // Per spec: agents that want their text to NOT be a protocol line should prefix with space.
    assert.deepStrictEqual(classifyLine(' AGENT_SESSION:foo'), { kind: 'body', value: ' AGENT_SESSION:foo' });
  });

  test('body line: prefix-like but not exact prefix', () => {
    assert.deepStrictEqual(classifyLine('AGENT_SESSION'), { kind: 'body', value: 'AGENT_SESSION' });
  });
});

describe('decodeJsonValue', () => {
  test('JSON string', () => {
    assert.strictEqual(decodeJsonValue('"hi"'), 'hi');
  });

  test('JSON string with newline', () => {
    assert.strictEqual(decodeJsonValue('"a\\nb"'), 'a\nb');
  });

  test('empty', () => {
    assert.strictEqual(decodeJsonValue(''), '');
  });

  test('lenient: non-JSON returns trimmed raw', () => {
    assert.strictEqual(decodeJsonValue('  not json  '), 'not json');
  });

  test('JSON number becomes string', () => {
    assert.strictEqual(decodeJsonValue('42'), '42');
  });
});

describe('substitute', () => {
  test('replaces MESSAGE', () => {
    assert.strictEqual(substitute('You said: {{MESSAGE}}', { message: 'hi' }), 'You said: hi');
  });

  test('replaces SESSION_ID', () => {
    assert.strictEqual(substitute('s={{SESSION_ID}}', { sessionId: 'abc' }), 's=abc');
  });

  test('replaces SESSION_NAME', () => {
    assert.strictEqual(substitute('n={{SESSION_NAME}}', { sessionName: 'work' }), 'n=work');
  });

  test('empty SESSION_ID when new session', () => {
    assert.strictEqual(substitute('s={{SESSION_ID}}', { sessionId: '' }), 's=');
  });

  test('multiple placeholders', () => {
    assert.strictEqual(
      substitute('{{MESSAGE}} [{{SESSION_ID}}] ({{SESSION_NAME}})', {
        message: 'hi', sessionId: 's1', sessionName: 'work',
      }),
      'hi [s1] (work)',
    );
  });
});

describe('expandEnvRef', () => {
  test('expands ${VAR} from env', () => {
    assert.strictEqual(expandEnvRef('${HOME}', { HOME: '/u/x' }), '/u/x');
  });

  test('unknown var → empty', () => {
    assert.strictEqual(expandEnvRef('${NOPE}', {}), '');
  });

  test('no refs', () => {
    assert.strictEqual(expandEnvRef('plain value', {}), 'plain value');
  });

  test('mixed', () => {
    assert.strictEqual(
      expandEnvRef('key=${HOME} and ${missing}', { HOME: '/h' }),
      'key=/h and ',
    );
  });
});

describe('expandPath', () => {
  test('~ expands to homedir', () => {
    assert.strictEqual(expandPath('~'), os.homedir());
  });

  test('~/foo expands', () => {
    assert.strictEqual(expandPath('~/foo'), path.join(os.homedir(), 'foo'));
  });

  test('absolute path unchanged', () => {
    assert.strictEqual(expandPath('/usr/bin'), '/usr/bin');
  });

  test('relative path unchanged', () => {
    assert.strictEqual(expandPath('./foo'), './foo');
  });
});

describe('normalizeProfile', () => {
  test('minimal valid profile', () => {
    const p = normalizeProfile({ command: 'bash ./x.sh' });
    assert.deepStrictEqual(p.argv, ['bash', './x.sh']);
    assert.strictEqual(p.stdin, 'none');
    assert.strictEqual(p.streaming, true);
  });

  test('hub form: profile nested under agentproc:', () => {
    const p = normalizeProfile({ agentproc: { command: 'node ./x.js' } });
    assert.deepStrictEqual(p.argv, ['node', './x.js']);
  });

  test('rejects missing command', () => {
    assert.throws(() => normalizeProfile({}), /command must be a non-empty string/);
  });

  test('rejects empty command', () => {
    assert.throws(() => normalizeProfile({ command: '   ' }), /command must be a non-empty string/);
  });

  test('rejects non-object', () => {
    assert.throws(() => normalizeProfile(null), /must be an object/);
  });

  test('argv splits on whitespace, multiple spaces', () => {
    const p = normalizeProfile({ command: 'bash    ./spaced.sh' });
    assert.deepStrictEqual(p.argv, ['bash', './spaced.sh']);
  });

  test('args field defaults to empty array', () => {
    const p = normalizeProfile({ command: 'x' });
    assert.deepStrictEqual(p.args, []);
  });

  test('args field is preserved (cast to string)', () => {
    const p = normalizeProfile({ command: 'x', args: ['--foo', 42] });
    assert.deepStrictEqual(p.args, ['--foo', '42']);
  });

  test('cwd ~ is expanded', () => {
    const p = normalizeProfile({ command: 'x', cwd: '~/proj' });
    assert.strictEqual(p.cwd, path.join(os.homedir(), 'proj'));
  });

  test('stdin: message → message, anything else → none', () => {
    assert.strictEqual(normalizeProfile({ command: 'x', stdin: 'message' }).stdin, 'message');
    assert.strictEqual(normalizeProfile({ command: 'x', stdin: 'none' }).stdin, 'none');
    assert.strictEqual(normalizeProfile({ command: 'x', stdin: 'bogus' }).stdin, 'none');
    assert.strictEqual(normalizeProfile({ command: 'x' }).stdin, 'none');
  });

  test('streaming: false is honored, anything else true', () => {
    assert.strictEqual(normalizeProfile({ command: 'x', streaming: false }).streaming, false);
    assert.strictEqual(normalizeProfile({ command: 'x', streaming: true }).streaming, true);
    assert.strictEqual(normalizeProfile({ command: 'x' }).streaming, true);
  });
});

// ---------------------------------------------------------------------------
// 2. run() end-to-end tests with tiny agent scripts
// ---------------------------------------------------------------------------

function writeScript(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-runner-'));
  const file = path.join(dir, 'agent.sh');
  fs.writeFileSync(file, content, { mode: 0o755 });
  return file;
}

describe('run() — end-to-end', () => {
  test('simple reply body', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "hello"\n');
    const r = await run(
      { command: agent },
      { message: 'hi' },
    );
    assert.strictEqual(r.reply.trim(), 'hello');
    assert.strictEqual(r.sessionId, '');
    assert.strictEqual(r.error, '');
    assert.strictEqual(r.exitCode, 0);
  });

  test('AGENT_SESSION: last wins', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "AGENT_SESSION:first"\n' +
      'echo "AGENT_SESSION:second"\n' +
      'echo "done"\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.sessionId, 'second');
    assert.strictEqual(r.reply.trim(), 'done');
  });

  test('AGENT_PARTIAL: triggers onPartial callback', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "AGENT_PARTIAL:\\"chunk1\\""\n' +
      'echo "AGENT_PARTIAL:\\"chunk2\\""\n' +
      'echo "final"\n'
    );
    const partials = [];
    const r = await run(
      { command: agent },
      { message: 'hi', onPartial: (t) => partials.push(t) },
    );
    assert.deepStrictEqual(partials, ['chunk1', 'chunk2']);
    assert.strictEqual(r.reply.trim(), 'final');
  });

  test('AGENT_PARTIAL: when streaming=false, onPartial NOT called', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "AGENT_PARTIAL:\\"chunk1\\""\n' +
      'echo "final"\n'
    );
    const partials = [];
    const r = await run(
      { command: agent },
      { message: 'hi', streaming: false, onPartial: (t) => partials.push(t) },
    );
    assert.deepStrictEqual(partials, []);
    // partial lines are still NOT added to reply body (they're protocol lines)
    assert.strictEqual(r.reply.trim(), 'final');
  });

  test('AGENT_ERROR: surfaces in result.error', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "AGENT_PARTIAL:\\"thinking...\\""\n' +
      'echo "AGENT_ERROR:\\"rate limited\\""\n' +
      'exit 1\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.error, 'rate limited');
    assert.strictEqual(r.exitCode, 1);
  });

  test('AGENT_ERROR: marks exit 1 even if process exits 0', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "AGENT_ERROR:\\"soft fail\\""\n' +
      'exit 0\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.error, 'soft fail');
    assert.strictEqual(r.exitCode, 1);
  });

  test('reply body lines are NOT prefixed with protocol markers', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo " AGENT_SESSION:foo"\n' +  // leading space → body
      'echo "real reply"\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.sessionId, '');
    assert.strictEqual(r.reply.trim().split('\n').length, 2);
  });

  test('exit code propagates from agent', async () => {
    const agent = writeScript('#!/usr/bin/env bash\nexit 3\n');
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.exitCode, 3);
  });

  test('message is injected as AGENT_MESSAGE', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "got: $AGENT_MESSAGE"\n');
    const r = await run({ command: agent }, { message: 'payload' });
    assert.strictEqual(r.reply.trim(), 'got: payload');
  });

  test('AGENT_SESSION_ID injected from options.sessionId', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "prev: $AGENT_SESSION_ID"\n');
    const r = await run({ command: agent }, { message: 'hi', sessionId: 'prev-123' });
    assert.strictEqual(r.reply.trim(), 'prev: prev-123');
  });

  test('AGENT_PROTOCOL_VERSION injected', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "pv=$AGENT_PROTOCOL_VERSION"\n');
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.reply.trim(), `pv=${PROTOCOL_VERSION}`);
  });

  test('AGENT_STREAMING reflects streaming option', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "stream=$AGENT_STREAMING"\n');
    const r1 = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r1.reply.trim(), 'stream=1');
    const r2 = await run({ command: agent }, { message: 'hi', streaming: false });
    assert.strictEqual(r2.reply.trim(), 'stream=0');
  });

  test('profile.env injects env vars with ${VAR} expansion', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "MY_KEY=$MY_KEY"\n');
    const r = await run(
      { command: agent, env: { MY_KEY: '${HOME}' } },
      { message: 'hi' },
    );
    assert.strictEqual(r.reply.trim(), `MY_KEY=${process.env.HOME}`);
  });

  test('{{MESSAGE}} placeholder in args', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\necho "args: $1"\n'
    );
    const r = await run(
      { command: agent, args: ['{{MESSAGE}}'] },
      { message: 'hello' },
    );
    assert.strictEqual(r.reply.trim(), 'args: hello');
  });

  test('extraEnv from options is applied', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "x=$X"\n');
    const r = await run(
      { command: agent },
      { message: 'hi', extraEnv: { X: 'extra' } },
    );
    assert.strictEqual(r.reply.trim(), 'x=extra');
  });

  test('stdin: message — agent reads AGENT_MESSAGE from stdin', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\nread line\necho "stdin: $line"\n'
    );
    const r = await run(
      { command: agent, stdin: 'message' },
      { message: 'via-stdin' },
    );
    assert.strictEqual(r.reply.trim(), 'stdin: via-stdin');
  });

  test('timeout kills long-running agent', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\nsleep 30\necho "should not reach"\n'
    );
    const r = await run(
      { command: agent, kill_grace_secs: 1 },
      { message: 'hi', timeoutSecs: 1 },
    );
    assert.strictEqual(r.timedOut, true);
    assert.strictEqual(r.exitCode, 124);
  });

  test('multiline reply body preserves newlines', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "line 1"\n' +
      'echo "line 2"\n' +
      'echo "line 3"\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    // Lines are joined with \n; the trailing newline is the caller's responsibility
    // (the CLI adds it when printing).
    assert.strictEqual(r.reply, 'line 1\nline 2\nline 3');
  });

  test('spawn error (command not found) → exit 1', async () => {
    const r = await run(
      { command: '/nonexistent/command/xyz' },
      { message: 'hi' },
    );
    assert.strictEqual(r.exitCode, 1);
  });
});

test('PROTOCOL_VERSION is "0.1"', () => {
  assert.strictEqual(PROTOCOL_VERSION, '0.1');
});
