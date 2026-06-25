'use strict';
/**
 * Tests for hub.js — mock-based, no real network access.
 *
 * Run with: `node --test src/hub.test.js`
 *
 * Strategy: redirect HOME to a tmp dir for cache isolation; intercept
 * global.fetch with fixtures. Concurrency is disabled because HOME is
 * process-global state.
 */

const { test, describe, before, after, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const hub = require('./hub.js');

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const FAKE_TREE = [
  { path: 'hub', type: 'tree' },
  { path: 'hub/echo-agent', type: 'tree' },
  { path: 'hub/echo-agent/profile.yaml', type: 'blob' },
  { path: 'hub/echo-agent/bridge.py', type: 'blob' },
  { path: 'hub/echo-agent/bridge.js', type: 'blob' },
  { path: 'hub/echo-agent/bridge.sh', type: 'blob' },
  { path: 'hub/echo-agent/README.md', type: 'blob' },
  { path: 'hub/claude-code', type: 'tree' },
  { path: 'hub/claude-code/profile.yaml', type: 'blob' },
  { path: 'hub/claude-code/bridge.py', type: 'blob' },
  { path: 'hub/claude-code/bridge.js', type: 'blob' },
  { path: 'hub/claude-code/README.md', type: 'blob' },
];

const FAKE_FILE_CONTENTS = {
  'hub/echo-agent/profile.yaml':
    'name: echo-agent\n' +
    'description: Minimal hello-world agent\n' +
    'cli: none\n' +
    'agentproc:\n' +
    '  command: python3 ./bridge.py\n' +
    '  cwd: .\n' +
    'tested: official\n' +
    'maintainer: jeffkit\n',
  'hub/echo-agent/bridge.py': '#!/usr/bin/env python3\nprint("echo")\n',
  'hub/echo-agent/bridge.js': "'use strict';\nconsole.log('echo');\n",
  'hub/echo-agent/bridge.sh': '#!/usr/bin/env bash\necho echo\n',
  'hub/echo-agent/README.md': '# echo-agent\n\nHello world.\n',
  'hub/claude-code/profile.yaml':
    'name: claude-code\n' +
    'description: Claude Code wrapper\n' +
    'cli: claude\n' +
    'agentproc:\n' +
    '  command: python3 ./bridge.py\n' +
    'tested: official\n' +
    'maintainer: jeffkit\n',
  'hub/claude-code/bridge.py': '#!/usr/bin/env python3\nprint("claude")\n',
  'hub/claude-code/bridge.js': "'use strict';\nconsole.log('claude');\n",
  'hub/claude-code/README.md': '# claude-code\n\nReal wrapper.\n',
};

let _origHome = null;
let _tmpHome = null;

function setupHome() {
  _origHome = process.env.HOME;
  _tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-hub-home-'));
  process.env.HOME = _tmpHome;
}

function teardownHome() {
  if (_origHome !== null) process.env.HOME = _origHome;
  if (_tmpHome && fs.existsSync(_tmpHome)) {
    fs.rmSync(_tmpHome, { recursive: true, force: true });
  }
  _tmpHome = null;
}

/**
 * Install a fake global.fetch that serves our fixtures.
 */
function installFakeFetch(tree = FAKE_TREE, contents = FAKE_FILE_CONTENTS) {
  const counter = { json: 0, text: 0 };
  const orig = global.fetch;
  global.fetch = async (url, opts) => {
    const acceptJson = opts && opts.headers && opts.headers.Accept && opts.headers.Accept.includes('json');
    if (acceptJson && url.includes('git/trees')) {
      counter.json++;
      return {
        ok: true,
        json: async () => ({ tree }),
        text: async () => JSON.stringify({ tree }),
      };
    }
    if (acceptJson) {
      throw new Error(`unexpected JSON URL: ${url}`);
    }
    for (const [p, content] of Object.entries(contents)) {
      if (url.endsWith(p)) {
        counter.text++;
        return { ok: true, text: async () => content };
      }
    }
    throw new Error(`unexpected text URL: ${url}`);
  };
  counter.restore = () => { global.fetch = orig; };
  return counter;
}


// ---------------------------------------------------------------------------
// All tests live inside this suite so we can disable concurrency.
// (Mutating process.env.HOME is global state; parallel tests would clash.)
// ---------------------------------------------------------------------------

describe('hub', { concurrency: false }, () => {
  beforeEach(setupHome);
  afterEach(teardownHome);

  // ----- cacheAgeSecs -----

  describe('cacheAgeSecs', () => {
    test('null when not cached', () => {
      assert.strictEqual(hub.cacheAgeSecs('never'), null);
    });

    test('small age right after write', () => {
      fs.mkdirSync(hub.cacheDir('fresh'), { recursive: true });
      fs.writeFileSync(
        path.join(hub.cacheDir('fresh'), '.cache-meta.json'),
        JSON.stringify({ fetched_at: Date.now() / 1000, ref: 'main' })
      );
      const age = hub.cacheAgeSecs('fresh');
      assert.ok(age !== null);
      assert.ok(age < 5);
    });

    test('large age for stale cache', () => {
      fs.mkdirSync(hub.cacheDir('stale'), { recursive: true });
      fs.writeFileSync(
        path.join(hub.cacheDir('stale'), '.cache-meta.json'),
        JSON.stringify({ fetched_at: Date.now() / 1000 - 100000, ref: 'main' })
      );
      const age = hub.cacheAgeSecs('stale');
      assert.ok(age !== null);
      assert.ok(age >= 99999, `age=${age}`);
    });

    test('null for invalid meta', () => {
      fs.mkdirSync(hub.cacheDir('bad'), { recursive: true });
      fs.writeFileSync(
        path.join(hub.cacheDir('bad'), '.cache-meta.json'),
        'not json at all'
      );
      assert.strictEqual(hub.cacheAgeSecs('bad'), null);
    });
  });

  // ----- fetchProfile -----

  describe('fetchProfile', () => {
    test('downloads all files', async () => {
      const counter = installFakeFetch();
      try {
        const dir = await hub.fetchProfile('echo-agent');
        assert.ok(fs.existsSync(dir));
        const names = fs.readdirSync(dir).sort();
        assert.ok(names.includes('profile.yaml'));
        assert.ok(names.includes('bridge.py'));
        assert.ok(names.includes('bridge.js'));
        assert.ok(names.includes('README.md'));
        assert.ok(names.includes('.cache-meta.json'));
      } finally {
        counter.restore();
      }
    });

    test('unknown profile raises', async () => {
      const counter = installFakeFetch([{ path: 'hub', type: 'tree' }]);
      try {
        await assert.rejects(hub.fetchProfile('nope'), /not found in hub/);
      } finally {
        counter.restore();
      }
    });

    test('uses cache on second call (no new fetches)', async () => {
      const counter = installFakeFetch();
      try {
        await hub.fetchProfile('echo-agent');
        const afterFirst = { json: counter.json, text: counter.text };
        await hub.fetchProfile('echo-agent');
        assert.strictEqual(counter.json, afterFirst.json);
        assert.strictEqual(counter.text, afterFirst.text);
      } finally {
        counter.restore();
      }
    });

    test('refresh forces refetch', async () => {
      const counter = installFakeFetch();
      try {
        await hub.fetchProfile('echo-agent');
        const first = counter.json;
        await hub.fetchProfile('echo-agent', { refresh: true });
        assert.ok(counter.json > first);
      } finally {
        counter.restore();
      }
    });

    test('overwrites tampered cache on refresh', async () => {
      const counter = installFakeFetch();
      try {
        await hub.fetchProfile('echo-agent');
        const f = path.join(hub.cacheDir('echo-agent'), 'bridge.py');
        const original = fs.readFileSync(f, 'utf8');
        fs.writeFileSync(f, '# tampered\n');
        await hub.fetchProfile('echo-agent', { refresh: true });
        assert.strictEqual(fs.readFileSync(f, 'utf8'), original);
      } finally {
        counter.restore();
      }
    });
  });

  // ----- listProfiles -----

  describe('listProfiles', () => {
    test('returns all profile dirs with metadata', async () => {
      const counter = installFakeFetch();
      try {
        const profiles = await hub.listProfiles({ refresh: true });
        const names = profiles.map(p => p.name).sort();
        assert.deepStrictEqual(names, ['claude-code', 'echo-agent']);
        const ec = profiles.find(p => p.name === 'echo-agent');
        assert.strictEqual(ec.tested, 'official');
        assert.strictEqual(ec.description, 'Minimal hello-world agent');
        assert.strictEqual(ec.cli, 'none');
      } finally {
        counter.restore();
      }
    });
  });

  // ----- showReadme -----

  describe('showReadme', () => {
    test('returns README content', async () => {
      const counter = installFakeFetch();
      try {
        const text = await hub.showReadme('echo-agent', { refresh: true });
        assert.ok(text.includes('echo-agent'));
        assert.ok(text.includes('Hello world'));
      } finally {
        counter.restore();
      }
    });

    test('missing README returns placeholder', async () => {
      const tree = [
        { path: 'hub', type: 'tree' },
        { path: 'hub/noreadme', type: 'tree' },
        { path: 'hub/noreadme/profile.yaml', type: 'blob' },
      ];
      const contents = {
        'hub/noreadme/profile.yaml': 'name: noreadme\ndescription: x\ntested: unverified\n',
      };
      const counter = installFakeFetch(tree, contents);
      try {
        const text = await hub.showReadme('noreadme', { refresh: true });
        assert.ok(text.includes('no README.md'));
      } finally {
        counter.restore();
      }
    });
  });

  // ----- installProfile -----

  describe('installProfile', () => {
    test('copies to target dir', async () => {
      const counter = installFakeFetch();
      const tmpTarget = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-install-'));
      try {
        const dest = await hub.installProfile('echo-agent', tmpTarget, { refresh: true });
        assert.ok(fs.existsSync(dest));
        assert.ok(fs.existsSync(path.join(dest, 'profile.yaml')));
        assert.ok(fs.existsSync(path.join(dest, 'bridge.py')));
        assert.ok(!fs.existsSync(path.join(dest, '.cache-meta.json')));
      } finally {
        counter.restore();
        fs.rmSync(tmpTarget, { recursive: true, force: true });
      }
    });

    test('refuses existing target', async () => {
      const counter = installFakeFetch();
      const tmpTarget = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-install-'));
      try {
        await hub.installProfile('echo-agent', tmpTarget, { refresh: true });
        await assert.rejects(
          hub.installProfile('echo-agent', tmpTarget, { refresh: true }),
          /target already exists/
        );
      } finally {
        counter.restore();
        fs.rmSync(tmpTarget, { recursive: true, force: true });
      }
    });
  });

  // ----- Constants (no HOME mutation needed) -----

  test('HUB_CACHE_TTL_SECS is 24h', () => {
    assert.strictEqual(hub.HUB_CACHE_TTL_SECS, 24 * 60 * 60);
  });

  test('hub repo constants', () => {
    assert.strictEqual(hub.HUB_REPO, 'jeffkit/agentproc');
    assert.strictEqual(hub.HUB_REF, 'main');
  });
});
