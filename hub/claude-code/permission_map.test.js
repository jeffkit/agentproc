'use strict';
/**
 * Unit tests for Claude Code ↔ AgentProc permission frame mapping.
 * Run: node --test hub/claude-code/permission_map.test.js
 */

const { test } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const {
  controlToPermissionRequest,
  permissionResponseToControl,
  buildArgs,
  buildPermissionArgs,
  permissionEnabled,
} = require(path.join(__dirname, 'bridge.js'));

test('control_request can_use_tool → AGENT_PERMISSION_REQUEST shape', () => {
  const req = controlToPermissionRequest({
    type: 'control_request',
    request_id: '1f86c122-dbad-46bd-8be6-28f049772de9',
    request: {
      subtype: 'can_use_tool',
      tool_name: 'Bash',
      input: { command: "echo -n 'PERM-OK' > owned-by-perm.txt" },
      description: 'Create file',
      tool_use_id: 'call_00_abc',
    },
  });
  assert.deepStrictEqual(req, {
    request_id: '1f86c122-dbad-46bd-8be6-28f049772de9',
    tool_name: 'Bash',
    input: { command: "echo -n 'PERM-OK' > owned-by-perm.txt" },
    description: 'Create file',
    tool_use_id: 'call_00_abc',
  });
});

test('non-can_use_tool control_request ignored', () => {
  assert.strictEqual(controlToPermissionRequest({
    type: 'control_request',
    request_id: 'x',
    request: { subtype: 'initialize' },
  }), null);
});

test('allow response includes updatedInput', () => {
  const out = permissionResponseToControl(
    { request_id: 'r1', behavior: 'allow', updated_input: { command: 'true' } },
    { command: 'false' },
  );
  assert.strictEqual(out.type, 'control_response');
  assert.strictEqual(out.response.response.behavior, 'allow');
  assert.deepStrictEqual(out.response.response.updatedInput, { command: 'true' });
});

test('allow without updated_input falls back to original input', () => {
  const out = permissionResponseToControl(
    { request_id: 'r1', behavior: 'allow' },
    { command: 'echo hi' },
  );
  assert.deepStrictEqual(out.response.response.updatedInput, { command: 'echo hi' });
});

test('deny response includes message', () => {
  const out = permissionResponseToControl(
    { request_id: 'r2', behavior: 'deny', message: 'nope' },
    {},
  );
  assert.strictEqual(out.response.response.behavior, 'deny');
  assert.strictEqual(out.response.response.message, 'nope');
});

test('buildArgs uses skip-permissions; buildPermissionArgs uses stdio prompt tool', () => {
  const env = { CLAUDE_DISALLOW_TOOLS: 'AskUserQuestion' };
  const skip = buildArgs('hi', '', env).join(' ');
  assert.ok(skip.includes('--dangerously-skip-permissions'));
  assert.ok(!skip.includes('--permission-prompt-tool'));

  const perm = buildPermissionArgs('', env).join(' ');
  assert.ok(perm.includes('--permission-prompt-tool'));
  assert.ok(perm.includes('stdio'));
  assert.ok(!perm.includes('--dangerously-skip-permissions'));
  assert.ok(perm.includes('--input-format'));
});

test('permissionEnabled reads AGENT_PERMISSION', () => {
  assert.strictEqual(permissionEnabled({ AGENT_PERMISSION: '1' }), true);
  assert.strictEqual(permissionEnabled({ AGENT_PERMISSION: '0' }), false);
  assert.strictEqual(permissionEnabled({}), false);
});
