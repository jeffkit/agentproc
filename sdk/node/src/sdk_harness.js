'use strict';
/**
 * SDK entry conformance harness (Node).
 *
 * Run as: `node sdk_harness.js <kind>`
 *
 * Each `kind` maps to a handler that exercises one return / error path of
 * `createProfile`. The conformance test (sdk.test.js) spawns this harness with
 * a controlled AGENT_* env per scenario in spec/conformance/sdk.json and
 * asserts the exact stdout + exit code. The same scenarios run against the
 * Python SDK (tests/_sdk_harness.py), so the two SDK entries cannot drift.
 */

const { createProfile, ProtocolError } = require('./index.js');

const handlers = {
  async_string: async (ctx) => 'hello world',

  async_result: async (ctx) => ({ response: 'hi there', sessionId: 'sess-abc' }),

  async_none_partial: async (ctx) => {
    await ctx.sendPartial('streaming chunk');
    return undefined;
  },

  async_protocol_error: async (ctx) => {
    throw new ProtocolError('bad thing');
  },

  async_send_error_then_return: async (ctx) => {
    await ctx.sendError('warn');
    return 'after';
  },

  async_partial_with_role: async (ctx) => {
    await ctx.sendPartial('thinking...', 'thinking');
    return 'answer';
  },

  sync_string: (ctx) => 'hello from sync',

  sync_partial_bare: (ctx) => {
    // Bare call (no await) — the write happens at call time.
    ctx.sendPartial('sync chunk');
    return 'done';
  },
};

const kind = process.argv[2];
const handler = handlers[kind];
if (!handler) {
  process.stderr.write(`unknown kind: ${kind}\n`);
  process.exit(2);
}
createProfile(handler);
