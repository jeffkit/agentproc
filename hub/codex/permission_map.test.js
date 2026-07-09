'use strict';
/**
 * Unit tests for Codex ↔ AgentProc permission mapping + codebuddy guard.
 * Run: node --test hub/codex/permission_map.test.js hub/codebuddy/bridge.js
 *      (codebuddy tests are in this file too via require)
 */

const { test } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');

const {
  permissionEnabled,
  buildArgs,
  buildPermissionArgs,
  parseEvent,
  hookInputToPermissionRequest,
  permissionResponseToHookOutput,
  buildHooksJson,
} = require(path.join(__dirname, 'permission_map.js'));

const codebuddy = require(path.join(__dirname, '..', 'codebuddy', 'bridge.js'));

test('permissionEnabled reads AGENT_PERMISSION', () => {
  assert.strictEqual(permissionEnabled({}), false);
  assert.strictEqual(permissionEnabled({ AGENT_PERMISSION: '1' }), true);
  assert.strictEqual(permissionEnabled({ AGENT_PERMISSION: '0' }), false);
});

test('buildArgs default path has no approval/hook flags', () => {
  const args = buildArgs('hi', '', {});
  assert.deepStrictEqual(args.slice(0, 3), ['codex', 'exec', '--json']);
  assert.ok(!args.includes('--dangerously-bypass-hook-trust'));
});

test('buildPermissionArgs inserts hook-trust + on-request', () => {
  const args = buildPermissionArgs('hi', '', {});
  assert.ok(args.includes('--dangerously-bypass-hook-trust'));
  const cIdx = args.indexOf('-c');
  assert.ok(cIdx > 0);
  assert.strictEqual(args[cIdx + 1], 'approval_policy="on-request"');
  // Still has --json after the inserted flags
  assert.ok(args.includes('--json'));
});

test('buildPermissionArgs resume path keeps resume before flags', () => {
  const args = buildPermissionArgs('hi', 'thread-1', {});
  assert.deepStrictEqual(args.slice(0, 3), ['codex', 'exec', 'resume']);
  assert.ok(args.includes('--dangerously-bypass-hook-trust'));
  assert.ok(args.includes('thread-1'));
});

test('parseEvent maps thread / agent_message / turn.failed', () => {
  assert.deepStrictEqual(
    parseEvent({ type: 'thread.started', thread_id: 't1' }),
    { sessionId: 't1' },
  );
  assert.deepStrictEqual(
    parseEvent({
      type: 'item.completed',
      item: { type: 'agent_message', text: 'hello' },
    }),
    { partialText: 'hello' },
  );
  assert.deepStrictEqual(
    parseEvent({ type: 'turn.failed', error: 'boom' }),
    { error: 'boom' },
  );
});

test('hookInputToPermissionRequest maps Codex hook stdin', () => {
  const req = hookInputToPermissionRequest({
    tool_name: 'Bash',
    turn_id: 'turn-9',
    tool_input: { command: ['ls'], description: 'list files' },
  }, 'rid-1');
  assert.deepStrictEqual(req, {
    request_id: 'rid-1',
    tool_name: 'Bash',
    input: { command: ['ls'], description: 'list files' },
    description: 'list files',
    turn_id: 'turn-9',
  });
});

test('permissionResponseToHookOutput allow/deny', () => {
  assert.deepStrictEqual(
    permissionResponseToHookOutput({ behavior: 'allow' }),
    {
      hookSpecificOutput: {
        hookEventName: 'PermissionRequest',
        decision: { behavior: 'allow' },
      },
    },
  );
  const deny = permissionResponseToHookOutput({ behavior: 'deny', message: 'nope' });
  assert.strictEqual(deny.hookSpecificOutput.decision.behavior, 'deny');
  assert.strictEqual(deny.hookSpecificOutput.decision.message, 'nope');
});

test('buildHooksJson points at absolute hook script', () => {
  const hooks = buildHooksJson('/tmp/permission_hook.py');
  const cmd = hooks.hooks.PermissionRequest[0].hooks[0].command;
  assert.ok(cmd.includes('python3'));
  assert.ok(cmd.includes('/tmp/permission_hook.py'));
});

test('permission_hook.py allow/deny via unix socket', async () => {
  const net = require('node:net');
  const { spawn } = require('node:child_process');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-hook-'));
  const sockPath = path.join(tmp, 'perm.sock');
  const hook = path.join(__dirname, 'permission_hook.py');

  const server = net.createServer((conn) => {
    let buf = '';
    conn.on('data', (chunk) => {
      buf += chunk.toString();
      if (!buf.includes('\n')) return;
      const req = JSON.parse(buf.split('\n')[0]);
      assert.strictEqual(req.tool_name, 'Bash');
      conn.write(JSON.stringify({
        request_id: req.request_id,
        behavior: 'allow',
      }) + '\n');
      conn.end();
    });
  });

  await new Promise((resolve, reject) => {
    server.listen(sockPath, () => {
      const child = spawn(process.env.PYTHON || 'python3', [hook], {
        env: { ...process.env, AGENTPROC_CODEX_PERM_SOCK: sockPath },
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      let stdout = '';
      let stderr = '';
      child.stdout.on('data', (d) => { stdout += d; });
      child.stderr.on('data', (d) => { stderr += d; });
      child.on('error', (err) => {
        server.close();
        fs.rmSync(tmp, { recursive: true, force: true });
        reject(err);
      });
      child.on('close', (code) => {
        try {
          assert.strictEqual(code, 0, stderr || stdout);
          const out = JSON.parse(stdout.trim());
          assert.strictEqual(out.hookSpecificOutput.decision.behavior, 'allow');
          server.close(() => {
            fs.rmSync(tmp, { recursive: true, force: true });
            resolve();
          });
        } catch (err) {
          server.close();
          fs.rmSync(tmp, { recursive: true, force: true });
          reject(err);
        }
      });
      child.stdin.end(JSON.stringify({
        tool_name: 'Bash',
        tool_input: { command: ['echo', 'hi'] },
      }) + '\n');
    });
  });
});

test('codebuddy rejects AGENT_PERMISSION=1', () => {
  assert.strictEqual(codebuddy.permissionEnabled({ AGENT_PERMISSION: '1' }), true);
  assert.ok(codebuddy.PERMISSION_UNSUPPORTED.includes('not support'));
});
