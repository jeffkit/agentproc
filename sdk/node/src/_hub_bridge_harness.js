'use strict';

/**
 * Harness for hub_bridge_conformance.test.js.
 *
 * Calls the shared hub `runBridge` with an identity parseEvent (forwards
 * {partial/text/session/error} as-is) and a buildArgs that invokes
 * `fake-cli` (a stub script the test puts on PATH). The test feeds a turn
 * object on stdin, this harness runs runBridge, and the resulting NDJSON
 * events land on stdout — exactly the same observable behaviour as a real
 * hub bridge, but with the per-CLI parse logic stripped out so we isolate
 * the engine.
 *
 * Not loaded directly by user code — invoked as a subprocess by the
 * conformance test.
 */

const path = require('node:path');
const HUB_DIR = path.resolve(__dirname, '../../../hub');
const { runBridge } = require(path.join(HUB_DIR, '_shared', 'stream_utils.js'));

function identityParseEvent(event) {
  const t = event && event.type;
  if (t === 'partial') return { partialText: typeof event.text === 'string' ? event.text : '' };
  if (t === 'text')    return { finalText: typeof event.text === 'string' ? event.text : '' };
  if (t === 'session') {
    const id = event.id;
    return typeof id === 'string' ? { sessionId: id } : null;
  }
  if (t === 'error')   return { error: typeof event.message === 'string' ? event.message : '' };
  return null;
}

function identityBuildArgs(message /*, sessionId, env */) {
  // argv[0] is `fake-cli` (resolved from PATH by the test); the message is
  // passed as argv[1] but the fake CLI discards it — the test pre-baked
  // the cli_stdout lines into the script.
  return ['fake-cli', message];
}

runBridge({
  cliName: 'fake-cli',
  cliInstallHint: 'install hint',
  buildArgs: identityBuildArgs,
  parseEvent: identityParseEvent,
}).catch(err => {
  process.stderr.write(`harness fatal: ${err && err.stack || err}\n`);
  process.exit(1);
});
