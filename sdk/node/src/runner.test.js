'use strict';
/**
 * Tests for runner.js — the AgentProc canonical bridge implementation (wire 0.3).
 *
 * Run with: `node --test src/runner.test.js`
 *
 * Strategy:
 *   1. Pure-function tests: classifyLine, substitute, expandEnvRef, expandPath,
 *      normalizeProfile, isValidSessionId — no subprocess.
 *   2. run() end-to-end tests: spawn a tiny bash/node helper script that emits
 *      NDJSON events on stdout, assert the runner classifies them correctly.
 *
 * Wire 0.3: every agent stdout line is a JSON object (an NDJSON event); the
 * per-turn request travels on stdin as a {"type":"turn",...} object (no
 * AGENT_* env vars). `command` is always argv[0] and is never split.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  run,
  classifyLine,
  formatPermissionResponse,
  isValidPermissionRequest,
  substitute,
  normalizeProfile,
  expandEnvRef,
  expandPath,
  isValidSessionId,
  PROTOCOL_VERSION,
} = require('./runner.js');

// ---------------------------------------------------------------------------
// 1. Pure-function tests
// ---------------------------------------------------------------------------

describe('classifyLine', () => {
  test('identifies {"type":"session"}', () => {
    assert.deepStrictEqual(classifyLine('{"type":"session","id":"abc-123"}'), { kind: 'session', value: 'abc-123' });
  });

  test('session missing id → empty string', () => {
    assert.deepStrictEqual(classifyLine('{"type":"session"}'), { kind: 'session', value: '' });
  });

  test('session non-string id → empty string', () => {
    assert.deepStrictEqual(classifyLine('{"type":"session","id":123}'), { kind: 'session', value: '' });
  });

  test('identifies {"type":"partial"}', () => {
    assert.deepStrictEqual(classifyLine('{"type":"partial","text":"hello"}'), { kind: 'partial', value: 'hello' });
  });

  test('partial with newline in text', () => {
    assert.deepStrictEqual(classifyLine('{"type":"partial","text":"line1\\nline2"}'), { kind: 'partial', value: 'line1\nline2' });
  });

  test('partial empty text', () => {
    assert.deepStrictEqual(classifyLine('{"type":"partial","text":""}'), { kind: 'partial', value: '' });
  });

  test('partial with role carries role field', () => {
    assert.deepStrictEqual(classifyLine('{"type":"partial","text":"x","role":"thinking"}'), { kind: 'partial', value: 'x', role: 'thinking' });
  });

  test('partial non-string role is dropped', () => {
    assert.deepStrictEqual(classifyLine('{"type":"partial","text":"y","role":42}'), { kind: 'partial', value: 'y' });
  });

  test('identifies {"type":"text"}', () => {
    assert.deepStrictEqual(classifyLine('{"type":"text","text":"hello world"}'), { kind: 'text', value: 'hello world' });
  });

  test('identifies {"type":"error"}', () => {
    assert.deepStrictEqual(classifyLine('{"type":"error","message":"rate limited"}'), { kind: 'error', value: 'rate limited' });
  });

  test('identifies {"type":"permission_request"}', () => {
    const c = classifyLine('{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{}}');
    assert.strictEqual(c.kind, 'permission_request');
    assert.deepStrictEqual(c.value, { type: 'permission_request', request_id: '1', tool_name: 'Bash', input: {} });
  });

  test('plain text line → malformed', () => {
    assert.deepStrictEqual(classifyLine('hello world'), { kind: 'malformed', value: 'hello world' });
  });

  test('empty line → malformed', () => {
    assert.deepStrictEqual(classifyLine(''), { kind: 'malformed', value: '' });
  });

  test('valid JSON but not an object → malformed', () => {
    assert.deepStrictEqual(classifyLine('42'), { kind: 'malformed', value: '42' });
    assert.deepStrictEqual(classifyLine('[1,2,3]'), { kind: 'malformed', value: '[1,2,3]' });
  });

  test('object without type → malformed', () => {
    assert.deepStrictEqual(classifyLine('{"foo":"bar"}'), { kind: 'malformed', value: '{"foo":"bar"}' });
  });

  test('unknown type → malformed', () => {
    assert.deepStrictEqual(classifyLine('{"type":"unknown"}'), { kind: 'malformed', value: '{"type":"unknown"}' });
  });
});

describe('isValidSessionId', () => {
  test('valid UUID', () => {
    assert.ok(isValidSessionId('f47ac10b-58cc-4372-a567-0e02b2c3d479'));
  });

  test('valid CLI handle', () => {
    assert.ok(isValidSessionId('cli-sess-9f3a2c1e'));
  });

  test('valid short token', () => {
    assert.ok(isValidSessionId('abc123'));
  });

  test('empty rejected', () => {
    assert.strictEqual(isValidSessionId(''), false);
  });

  test('wire 0.3: spaces ARE allowed', () => {
    // The 0.2 colon-delimited prefix banned whitespace; 0.3 has no such prefix,
    // so a session id may contain spaces (only storage-safety rules remain).
    assert.ok(isValidSessionId('has space'));
  });

  test('wire 0.3: colons ARE allowed', () => {
    assert.ok(isValidSessionId('thread:abc'));
  });

  test('wire 0.3: plus IS allowed', () => {
    assert.ok(isValidSessionId('a+b'));
  });

  test('control chars rejected (storage safety)', () => {
    assert.strictEqual(isValidSessionId('ctrl\x07char'), false);
    assert.strictEqual(isValidSessionId('tab\there'), false);
  });

  test('slash rejected (path-traversal vector)', () => {
    assert.strictEqual(isValidSessionId('a/b'), false);
    assert.strictEqual(isValidSessionId('..\\..\\tmp'), false);
  });

  test('dot and dotdot rejected', () => {
    assert.strictEqual(isValidSessionId('.'), false);
    assert.strictEqual(isValidSessionId('..'), false);
  });

  test('legitimate ids that contain `..` are accepted (no false positive)', () => {
    assert.ok(isValidSessionId('a..b'));
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

  test('replaces PROFILE_DIR', () => {
    assert.strictEqual(substitute('d={{PROFILE_DIR}}', { profileDir: '/p' }), 'd=/p');
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

  test('allowlist permits listed var', () => {
    assert.strictEqual(expandEnvRef('${HOME}', { HOME: '/h' }, new Set(['HOME'])), '/h');
  });

  test('allowlist blocks unlisted var', () => {
    assert.strictEqual(
      expandEnvRef('${AWS_SECRET_ACCESS_KEY}', { AWS_SECRET_ACCESS_KEY: 's3cr3t' }, new Set(['HOME'])),
      '',
    );
  });

  test('allowlist blocked callback fires', () => {
    const blocked = [];
    const out = expandEnvRef('${A} ${B}', { A: '1', B: '2' }, new Set(['A']), (n) => blocked.push(n));
    assert.strictEqual(out, '1 ');
    assert.deepStrictEqual(blocked, ['B']);
  });

  test('allowlist null means all permitted', () => {
    assert.strictEqual(expandEnvRef('${ANYTHING}', { ANYTHING: 'x' }, null), 'x');
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
  test('minimal valid profile — command is argv[0], never split', () => {
    const p = normalizeProfile({ command: 'bash' });
    assert.deepStrictEqual(p.argv, ['bash']);
    assert.deepStrictEqual(p.args, []);
    assert.strictEqual(p.streaming, true);
  });

  test('hub form: profile nested under agentproc:', () => {
    const p = normalizeProfile({ agentproc: { command: 'node' } });
    assert.deepStrictEqual(p.argv, ['node']);
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

  test('wire 0.3: command with whitespace is kept whole (no shorthand split)', () => {
    // The 0.2 "args absent ⇒ split command" shorthand is gone. A command with
    // spaces is argv[0] verbatim; use `args` for the rest.
    const p = normalizeProfile({ command: 'python3 ./bridge.py' });
    assert.deepStrictEqual(p.argv, ['python3 ./bridge.py']);
    assert.deepStrictEqual(p.args, []);
  });

  test('args field is preserved (cast to string)', () => {
    const p = normalizeProfile({ command: 'x', args: ['--foo', 42] });
    assert.deepStrictEqual(p.args, ['--foo', '42']);
  });

  test('args null → empty array (treated as absent, NOT split)', () => {
    const p = normalizeProfile({ command: 'python3 ./bridge.py', args: null });
    assert.deepStrictEqual(p.argv, ['python3 ./bridge.py']);
    assert.deepStrictEqual(p.args, []);
  });

  test('cwd ~ is expanded', () => {
    const p = normalizeProfile({ command: 'x', cwd: '~/proj' });
    assert.strictEqual(p.cwd, path.join(os.homedir(), 'proj'));
  });

  test('wire 0.3: no `stdin` field on the normalized profile', () => {
    const p = normalizeProfile({ command: 'x' });
    assert.strictEqual(p.stdin, undefined);
  });

  test('wire 0.3: no `env_inherit` field on the normalized profile', () => {
    const p = normalizeProfile({ command: 'x' });
    assert.strictEqual(p.env_inherit, undefined);
    // Even if a legacy profile supplies it, it is ignored (not honored).
    const p2 = normalizeProfile({ command: 'x', env_inherit: 'all' });
    assert.strictEqual(p2.env_inherit, undefined);
  });

  test('truncation_suffix defaults to ellipsis notice', () => {
    const p = normalizeProfile({ command: 'x' });
    assert.strictEqual(p.truncation_suffix, '\n\n…(truncated)');
  });

  test('truncation_suffix: custom cap no longer strips the notice', () => {
    // Regression: a custom max_reply_chars used to silently disable the
    // notice (only === DEFAULT_MAX_REPLY_CHARS got one). Now the notice is
    // tied to truncation_suffix, independent of cap.
    const p = normalizeProfile({ command: 'x', max_reply_chars: 100 });
    assert.strictEqual(p.truncation_suffix, '\n\n…(truncated)');
    const p2 = normalizeProfile({ command: 'x', truncation_suffix: ' [more]' });
    assert.strictEqual(p2.truncation_suffix, ' [more]');
  });

  test('truncation_suffix: empty string disables the notice', () => {
    const p = normalizeProfile({ command: 'x', truncation_suffix: '' });
    assert.strictEqual(p.truncation_suffix, '');
  });

  test('permission defaults false; true only when boolean true', () => {
    assert.strictEqual(normalizeProfile({ command: 'x' }).permission, false);
    assert.strictEqual(normalizeProfile({ command: 'x', permission: true }).permission, true);
    assert.strictEqual(normalizeProfile({ command: 'x', permission: false }).permission, false);
    assert.strictEqual(normalizeProfile({ command: 'x', permission: 'true' }).permission, false);
  });

  test('streaming: false is honored, anything else true', () => {
    assert.strictEqual(normalizeProfile({ command: 'x', streaming: false }).streaming, false);
    assert.strictEqual(normalizeProfile({ command: 'x', streaming: true }).streaming, true);
    assert.strictEqual(normalizeProfile({ command: 'x' }).streaming, true);
  });

  test('env_allowlist absent → null', () => {
    assert.strictEqual(normalizeProfile({ command: 'x', env: { A: '1' } }).env_allowlist, null);
  });

  test('env_allowlist parsed to Set', () => {
    const p = normalizeProfile({ command: 'x', env_allowlist: ['A', 'B'] });
    assert.ok(p.env_allowlist instanceof Set);
    assert.ok(p.env_allowlist.has('A'));
    assert.ok(p.env_allowlist.has('B'));
  });

  test('env_allowlist non-array throws', () => {
    assert.throws(() => normalizeProfile({ command: 'x', env_allowlist: 'A' }), /env_allowlist/);
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

/** Write a Node agent that reads the turn from stdin and emits NDJSON events. */
function writeNodeAgent(src) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-runner-'));
  const file = path.join(dir, 'agent.js');
  fs.writeFileSync(file, src);
  return file;
}

/** Profile that runs a Node agent: { command: node, args: [script] }. */
function nodeProfile(script) {
  return { command: process.execPath, args: [script] };
}

describe('run() — end-to-end', () => {
  test('simple text event → reply', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho \'{"type":"text","text":"hello"}\'\n');
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.reply, 'hello');
    assert.strictEqual(r.sessionId, '');
    assert.strictEqual(r.error, '');
    assert.strictEqual(r.exitCode, 0);
  });

  test('session last wins', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"session","id":"first"}\'\n' +
      'echo \'{"type":"session","id":"second"}\'\n' +
      'echo \'{"type":"text","text":"done"}\'\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.sessionId, 'second');
    assert.strictEqual(r.reply, 'done');
  });

  test('partial triggers onPartial callback', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"partial","text":"chunk1"}\'\n' +
      'echo \'{"type":"partial","text":"chunk2"}\'\n' +
      'echo \'{"type":"text","text":"final"}\'\n'
    );
    const partials = [];
    const r = await run(
      { command: agent },
      { message: 'hi', onPartial: (t) => partials.push(t) },
    );
    assert.deepStrictEqual(partials, ['chunk1', 'chunk2']);
    assert.strictEqual(r.reply, 'final');
  });

  test('partial role is passed to onPartial', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"partial","text":"thinking...","role":"thinking"}\'\n' +
      'echo \'{"type":"text","text":"ok"}\'\n'
    );
    const seen = [];
    await run({ command: agent }, { message: 'hi', onPartial: (t, role) => seen.push({ t, role }) });
    assert.deepStrictEqual(seen, [{ t: 'thinking...', role: 'thinking' }]);
  });

  test('streaming=false → onPartial NOT called, text still captured', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"partial","text":"chunk1"}\'\n' +
      'echo \'{"type":"text","text":"final"}\'\n'
    );
    const partials = [];
    const r = await run(
      { command: agent },
      { message: 'hi', streaming: false, onPartial: (t) => partials.push(t) },
    );
    assert.deepStrictEqual(partials, []);
    assert.strictEqual(r.reply, 'final');
  });

  test('error event surfaces in result.error', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"partial","text":"thinking..."}\'\n' +
      'echo \'{"type":"error","message":"rate limited"}\'\n' +
      'exit 1\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.error, 'rate limited');
    assert.strictEqual(r.exitCode, 1);
  });

  test('error event marks exit 1 even if process exits 0', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"error","message":"soft fail"}\'\n' +
      'exit 0\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.error, 'soft fail');
    assert.strictEqual(r.exitCode, 1);
  });

  test('multiple text events concatenate with no separator', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"text","text":"a"}\'\n' +
      'echo \'{"type":"text","text":"b"}\'\n' +
      'echo \'{"type":"text","text":"c"}\'\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.reply, 'abc');
  });

  test('text event preserves embedded newlines', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      "echo '{\"type\":\"text\",\"text\":\"line 1\\nline 2\\nline 3\"}'\n"
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.reply, 'line 1\nline 2\nline 3');
  });

  test('malformed stdout lines are ignored, never appended to reply', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo " AGENT_SESSION:foo"\n' +   // plain text → malformed (leading space irrelevant)
      'echo "not json"\n' +             // malformed
      'echo \'{"type":"text","text":"real reply"}\'\n'
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.sessionId, '');
    assert.strictEqual(r.reply, 'real reply');
  });

  test('exit code propagates from agent', async () => {
    const agent = writeScript('#!/usr/bin/env bash\nexit 3\n');
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.exitCode, 3);
  });

  test('wire 0.3: AGENT_* env vars are NOT injected', async () => {
    // The per-turn request travels on stdin, not env. An agent reading
    // $AGENT_MESSAGE must see it unset.
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "{\\"type\\":\\"text\\",\\"text\\":\\"m=<${AGENT_MESSAGE:-unset}>\\"}"\n'
    );
    const r = await run({ command: agent }, { message: 'payload' });
    assert.strictEqual(r.reply, 'm=<unset>');
  });

  test('the turn object is written to stdin (message, session, from_user, protocol_version)', async () => {
    const agent = writeNodeAgent(
      "const fs=require('fs');\n" +
      "const line=fs.readFileSync(0,'utf8').split('\\n')[0];\n" +
      "const t=JSON.parse(line);\n" +
      "const out={type:'text',text:['msg='+t.message,'sid='+t.session_id,'sname='+t.session_name,'from='+t.from_user,'pv='+t.protocol_version].join('|')};\n" +
      "process.stdout.write(JSON.stringify(out)+'\\n');\n"
    );
    const r = await run(
      nodeProfile(agent),
      { message: 'hello', sessionId: 'prev-123', sessionName: 'work', fromUser: 'u123' },
    );
    assert.strictEqual(r.reply, 'msg=hello|sid=prev-123|sname=work|from=u123|pv=' + PROTOCOL_VERSION);
  });

  test('attachments travel in the turn object on stdin', async () => {
    const agent = writeNodeAgent(
      "const fs=require('fs');\n" +
      "const line=fs.readFileSync(0,'utf8').split('\\n')[0];\n" +
      "const t=JSON.parse(line);\n" +
      "const atts=(t.attachments||[]).map(a=>a.kind+':'+a.url).join(',');\n" +
      "process.stdout.write(JSON.stringify({type:'text',text:'atts='+atts})+'\\n');\n"
    );
    const r = await run(
      nodeProfile(agent),
      {
        message: 'hi',
        attachments: [
          { kind: 'image', url: 'https://example.com/a.png' },
          { kind: 'file', url: 'https://example.com/b.pdf' },
        ],
      },
    );
    assert.strictEqual(r.reply, 'atts=image:https://example.com/a.png,file:https://example.com/b.pdf');
  });

  test('{{MESSAGE}} placeholder in args', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "{\\"type\\":\\"text\\",\\"text\\":\\"args:$1\\"}"\n');
    const r = await run(
      { command: agent, args: ['{{MESSAGE}}'] },
      { message: 'hello' },
    );
    assert.strictEqual(r.reply, 'args:hello');
  });

  test('profile.env injects env vars with ${VAR} expansion', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "{\\"type\\":\\"text\\",\\"text\\":\\"MY_KEY=$MY_KEY\\"}"\n');
    const r = await run(
      { command: agent, env: { MY_KEY: '${HOME}' } },
      { message: 'hi' },
    );
    assert.strictEqual(r.reply, `MY_KEY=${process.env.HOME}`);
  });

  test('extraEnv from options is applied', async () => {
    const agent = writeScript('#!/usr/bin/env bash\necho "{\\"type\\":\\"text\\",\\"text\\":\\"x=$X\\"}"\n');
    const r = await run(
      { command: agent },
      { message: 'hi', extraEnv: { X: 'extra' } },
    );
    assert.strictEqual(r.reply, 'x=extra');
  });

  test('timeout kills long-running agent', async () => {
    const agent = writeScript('#!/usr/bin/env bash\nsleep 30\necho "should not reach"\n');
    const r = await run(
      { command: agent, kill_grace_secs: 1 },
      { message: 'hi', timeoutSecs: 1 },
    );
    assert.strictEqual(r.timedOut, true);
    assert.strictEqual(r.exitCode, 124);
  });

  test('spawn error (command not found) → exit 1', async () => {
    const r = await run(
      { command: '/nonexistent/command/xyz' },
      { message: 'hi' },
    );
    assert.strictEqual(r.exitCode, 1);
  });

  test('env_allowlist permits listed var, blocks unlisted, warns onStderr', async () => {
    process.env.ALLOWED_KEY = 'ok-val';
    process.env.SECRET_KEY = 'top-secret';
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "{\\"type\\":\\"text\\",\\"text\\":\\"ALLOWED=$ALLOWED_KEY SECRET=$SECRET_KEY\\"}"\n',
    );
    try {
      const warnings = [];
      const r = await run(
        {
          command: agent,
          env: {
            ALLOWED_KEY: '${ALLOWED_KEY}',
            SECRET_KEY: '${SECRET_KEY}',
          },
          env_allowlist: ['ALLOWED_KEY'],
        },
        { message: 'hi', onStderr: (s) => warnings.push(s) },
      );
      assert.ok(r.reply.includes('ALLOWED=ok-val'), `reply was: ${r.reply}`);
      assert.ok(r.reply.includes('SECRET='), `reply was: ${r.reply}`);
      assert.ok(!r.reply.includes('top-secret'), `secret leaked: ${r.reply}`);
      assert.ok(warnings.some((w) => w.includes('SECRET_KEY') && w.includes('allowlist')));
    } finally {
      delete process.env.ALLOWED_KEY;
      delete process.env.SECRET_KEY;
    }
  });

  test('undeclared secrets do not leak (minimal infra base, no env_inherit: all)', async () => {
    process.env.BRIDGE_DB_PASSWORD = 'db-top-secret';
    process.env.AGENTPROC_SECURE_DEFAULT_LEAK = 'should-not-leak';
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "{\\"type\\":\\"text\\",\\"text\\":\\"DB=${BRIDGE_DB_PASSWORD:-unset} LEAK=${AGENTPROC_SECURE_DEFAULT_LEAK:-unset} PATH_SET=${PATH:+yes}\\"}"\n',
    );
    try {
      const r = await run(
        { command: agent, env: { BRIDGE_DB_PASSWORD: '${BRIDGE_DB_PASSWORD}' } },
        { message: 'hi' },
      );
      // Declared (no allowlist ⇒ all permitted) → reaches the agent.
      assert.ok(r.reply.includes('DB=db-top-secret'), `reply: ${r.reply}`);
      // Undeclared secret never leaks via inheritance (infra base only).
      assert.ok(r.reply.includes('LEAK=unset'), `reply: ${r.reply}`);
      assert.ok(!r.reply.includes('should-not-leak'), `leaked: ${r.reply}`);
      // Infra (PATH) still present so the agent can run.
      assert.ok(r.reply.includes('PATH_SET=yes'), `PATH missing: ${r.reply}`);
    } finally {
      delete process.env.BRIDGE_DB_PASSWORD;
      delete process.env.AGENTPROC_SECURE_DEFAULT_LEAK;
    }
  });

  test('invalid session id (path separator) ignored, preserves previous valid id', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"session","id":"valid-id-1"}\'\n' +
      'echo \'{"type":"session","id":"bad/path"}\'\n' +
      'echo \'{"type":"text","text":"done"}\'\n',
    );
    const warnings = [];
    const r = await run({ command: agent }, { message: 'hi', onStderr: (s) => warnings.push(s) });
    assert.strictEqual(r.sessionId, 'valid-id-1');
    assert.strictEqual(r.reply, 'done');
    assert.ok(warnings.some((w) => w.includes('invalid') && w.includes('session id')));
  });

  test('invalid session id when no previous → session stays empty', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo \'{"type":"session","id":"bad/path"}\'\n' +
      'echo \'{"type":"text","text":"done"}\'\n',
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.sessionId, '');
    assert.strictEqual(r.reply, 'done');
  });

  test('stderr diagnosis survives a >1 MB noisy stderr (head cap keeps early signal)', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'echo "python3: can\'t open file \'/tmp/missing.py\': [Errno 2] No such file or directory" >&2\n' +
      'head -c 2097152 /dev/zero | tr "\\0" "x" >&2\n' +
      'exit 1\n',
    );
    const r = await run({ command: agent }, { message: 'hi' });
    assert.strictEqual(r.exitCode, 1);
    assert.strictEqual(
      r.error,
      "agent script not found: /tmp/missing.py. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).",
      `diagnosis lost in noise; r.error=${JSON.stringify(r.error)}`,
    );
  });

  test('permission:true — turn carries permission:true, request → allow → response on stdin', async () => {
    // The agent reads the turn line (discards it), emits a permission_request,
    // then reads the permission_response line the runner writes back. We grep
    // the response (it contains quotes, so we must not embed it raw into JSON)
    // and emit a fixed text event reporting what we saw.
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'read -r turn\n' +
      'echo \'{"type":"permission_request","request_id":"r1","tool_name":"Bash","input":{"command":"true"}}\'\n' +
      'IFS= read -r resp\n' +
      "if echo \"$resp\" | grep -q '\"behavior\":\"allow\"'; then echo '{\"type\":\"text\",\"text\":\"ALLOWED\"}'; fi\n" +
      'echo \'{"type":"session","id":"sess-perm-1"}\'\n'
    );
    const seen = [];
    const r = await run(
      { command: agent, permission: true },
      {
        message: 'hi',
        onPermission: (req) => {
          seen.push(req);
          return { behavior: 'allow', updated_input: req.input };
        },
      },
    );
    assert.strictEqual(seen.length, 1);
    assert.strictEqual(seen[0].request_id, 'r1');
    assert.strictEqual(r.reply, 'ALLOWED');
    assert.strictEqual(r.sessionId, 'sess-perm-1');
    assert.strictEqual(r.exitCode, 0);
  });

  test('permission:false ignores permission_request (no onPermission call)', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'read -r turn\n' +   // consume turn; runner closes stdin after (permission off)
      'echo \'{"type":"permission_request","request_id":"r1","tool_name":"Bash","input":{}}\'\n' +
      'echo \'{"type":"text","text":"done"}\'\n'
    );
    const stderr = [];
    let called = false;
    const r = await run(
      { command: agent },
      {
        message: 'hi',
        onPermission: () => { called = true; return { behavior: 'allow' }; },
        onStderr: (l) => stderr.push(l),
      },
    );
    assert.strictEqual(called, false);
    assert.strictEqual(r.reply, 'done');
    assert.ok(stderr.some((l) => /ignoring .*permission_request/.test(l)));
  });

  test('permission deny is written when onPermission returns deny', async () => {
    const agent = writeScript(
      '#!/usr/bin/env bash\n' +
      'read -r turn\n' +
      'echo \'{"type":"permission_request","request_id":"r2","tool_name":"Bash","input":{}}\'\n' +
      'IFS= read -r resp\n' +
      "if echo \"$resp\" | grep -q '\"behavior\":\"deny\"'; then echo '{\"type\":\"text\",\"text\":\"DENIED\"}'; fi\n" +
      "if echo \"$resp\" | grep -q 'not allowed'; then echo '{\"type\":\"text\",\"text\":\"HASMSG\"}'; fi\n"
    );
    const r = await run(
      { command: agent, permission: true },
      {
        message: 'hi',
        onPermission: () => ({ behavior: 'deny', message: 'not allowed' }),
      },
    );
    assert.ok(r.reply.includes('DENIED'), `reply: ${r.reply}`);
    assert.ok(r.reply.includes('HASMSG'), `reply: ${r.reply}`);
  });
});

test('formatPermissionResponse / isValidPermissionRequest', () => {
  assert.strictEqual(
    formatPermissionResponse({ request_id: '1', behavior: 'allow', updated_input: { c: 'x' } }),
    '{"type":"permission_response","request_id":"1","behavior":"allow","updated_input":{"c":"x"}}',
  );
  assert.strictEqual(
    formatPermissionResponse({ request_id: '2', behavior: 'deny', message: 'nope' }),
    '{"type":"permission_response","request_id":"2","behavior":"deny","message":"nope"}',
  );
  // allow without updated_input MUST omit the field — the agent/CLI is
  // responsible for falling back to the request's original input. The
  // runner must not pre-fill it (would erase the "user accepted unchanged"
  // vs "user never touched it" distinction downstream).
  assert.strictEqual(
    formatPermissionResponse({ request_id: '3', behavior: 'allow' }),
    '{"type":"permission_response","request_id":"3","behavior":"allow"}',
  );
  assert.ok(isValidPermissionRequest({ request_id: '1', tool_name: 'Bash', input: {} }));
  assert.ok(!isValidPermissionRequest({ request_id: '1', tool_name: 'Bash' }));
  assert.ok(!isValidPermissionRequest({ request_id: 'a b', tool_name: 'Bash', input: {} }));
  assert.ok(!isValidPermissionRequest(null));
});

test('PROTOCOL_VERSION is "0.3"', () => {
  assert.strictEqual(PROTOCOL_VERSION, '0.3');
});
