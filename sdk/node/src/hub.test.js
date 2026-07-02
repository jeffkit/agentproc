'use strict';
/**
 * Tests for hub.js — mock-based, no real network access.
 *
 * Strategy: redirect HOME to a tmp dir for cache isolation; point the
 * bundled-hub dir at a tmp path (non-existent by default, so tests exercise
 * the jsDelivr remote path; populated explicitly for bundled-path tests);
 * intercept global.fetch with fixtures. Concurrency is disabled because HOME
 * and the bundled-dir pointer are process-global state.
 */

const { test, describe, before, after, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const hub = require('./hub.js');

// ---------------------------------------------------------------------------
// Test fixtures: synthetic GitHub tree + file contents
// ---------------------------------------------------------------------------

const FAKE_TREE = [
  { path: 'hub', type: 'tree' },
  { path: 'hub/_shared', type: 'tree' },
  { path: 'hub/_shared/stream_utils.py', type: 'blob' },
  { path: 'hub/_shared/stream_utils.js', type: 'blob' },
  { path: 'hub/_shared/README.md', type: 'blob' },
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
  'hub/_shared/stream_utils.py': 'def main_entry():\n    pass\n',
  'hub/_shared/stream_utils.js': "'use strict';\nmodule.exports = {};\n",
  'hub/_shared/README.md': '# shared\n',
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
let _origBundled = null;

function setupHome() {
  _origHome = process.env.HOME;
  _tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-hub-home-'));
  process.env.HOME = _tmpHome;
  // Disable the bundled copy by default so tests exercise the jsDelivr
  // remote path. Bundled-path tests point this at a real tmp dir.
  _origBundled = hub.setBundledHubDir(path.join(_tmpHome, 'no-such-bundle'));
  hub.clearTreeCache();
}

function teardownHome() {
  if (_origHome !== null) process.env.HOME = _origHome;
  if (_tmpHome && fs.existsSync(_tmpHome)) {
    fs.rmSync(_tmpHome, { recursive: true, force: true });
  }
  if (_origBundled !== null) hub.setBundledHubDir(_origBundled);
  _tmpHome = null;
  _origBundled = null;
}

/**
 * Convert a flat [{path, type:'blob'|'tree'}] tree (GitHub Trees API shape)
 * into the nested {files:[{type:'directory'|'file', name, files}]} shape that
 * jsDelivr's data API returns, so the fake fetch can serve it.
 */
function flatToNested(flat) {
  const root = { files: [] };
  const dirNodes = new Map([['', root]]);
  // Sort so directory entries come before their children.
  const sorted = flat.slice().sort((a, b) => a.path.localeCompare(b.path));
  for (const e of sorted) {
    const segs = e.path.split('/');
    const name = segs.pop();
    const parentPath = segs.join('/');
    const parent = dirNodes.get(parentPath) || root;
    if (e.type === 'tree') {
      const node = { type: 'directory', name, files: [] };
      parent.files.push(node);
      dirNodes.set(e.path, node);
    } else {
      parent.files.push({ type: 'file', name });
    }
  }
  return root.files;
}

/**
 * Install a fake global.fetch that serves our fixtures from jsDelivr URLs.
 */
function installFakeFetch(tree = FAKE_TREE, contents = FAKE_FILE_CONTENTS) {
  const counter = { json: 0, text: 0 };
  const orig = global.fetch;
  const nested = flatToNested(tree);
  global.fetch = async (url, opts) => {
    if (typeof url !== 'string') url = String(url);
    if (url.includes('data.jsdelivr.com')) {
      counter.json++;
      return {
        ok: true,
        json: async () => ({ files: nested }),
        text: async () => JSON.stringify({ files: nested }),
      };
    }
    for (const [p, content] of Object.entries(contents)) {
      if (url.endsWith(p)) {
        counter.text++;
        return { ok: true, text: async () => content };
      }
    }
    // Unmatched raw URL → 404 (optional file missing, or wrong profile name).
    return { ok: false, status: 404, text: async () => '' };
  };
  counter.restore = () => { global.fetch = orig; };
  return counter;
}

/**
 * Materialize a tmp "bundled hub" directory with the given profile subdirs
 * (copied from FAKE_FILE_CONTENTS) and point the module at it.
 */
function useBundledDir(profileNames) {
  const dir = path.join(_tmpHome, 'bundled-hub');
  fs.mkdirSync(dir, { recursive: true });
  for (const name of profileNames) {
    const prefix = `hub/${name}/`;
    const dest = path.join(dir, name);
    fs.mkdirSync(dest, { recursive: true });
    for (const [p, content] of Object.entries(FAKE_FILE_CONTENTS)) {
      if (p.startsWith(prefix)) {
        fs.writeFileSync(path.join(dest, path.basename(p)), content, 'utf8');
      }
    }
  }
  // Always include _shared in the bundle.
  const sharedDest = path.join(dir, '_shared');
  fs.mkdirSync(sharedDest, { recursive: true });
  for (const [p, content] of Object.entries(FAKE_FILE_CONTENTS)) {
    if (p.startsWith('hub/_shared/')) {
      fs.writeFileSync(path.join(sharedDest, path.basename(p)), content, 'utf8');
    }
  }
  hub.setBundledHubDir(dir);
  return dir;
}

// ---------------------------------------------------------------------------
// All tests live inside this suite so we can disable concurrency.
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

  // ----- fetchProfile (remote / jsDelivr) -----

  describe('fetchProfile (remote)', () => {
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

    test('happy path does not call the jsDelivr data API', async () => {
      const counter = installFakeFetch();
      try {
        await hub.fetchProfile('echo-agent');
        assert.strictEqual(counter.json, 0);
      } finally {
        counter.restore();
      }
    });

    test('skips optional files that 404 (e.g. bridge.sh on claude-code)', async () => {
      const counter = installFakeFetch();
      try {
        const dir = await hub.fetchProfile('claude-code');
        const names = fs.readdirSync(dir);
        assert.ok(names.includes('profile.yaml'));
        assert.ok(names.includes('bridge.py'));
        assert.ok(names.includes('bridge.js'));
        assert.ok(names.includes('README.md'));
        assert.ok(!names.includes('bridge.sh'));
      } finally {
        counter.restore();
      }
    });

    test('populates _shared in the cache root (bridge import dependency)', async () => {
      const counter = installFakeFetch();
      try {
        await hub.fetchProfile('claude-code');
        const shared = path.join(hub.cacheRoot(), '_shared');
        assert.ok(fs.existsSync(path.join(shared, 'stream_utils.py')),
          '_shared/stream_utils.py not cached — bridge.py import would fail');
        assert.ok(fs.existsSync(path.join(shared, 'stream_utils.js')));
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
        const firstText = counter.text;
        await hub.fetchProfile('echo-agent', { refresh: true });
        assert.ok(counter.text > firstText);
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

  // ----- fetchProfile (bundled) -----

  describe('fetchProfile (bundled)', () => {
    test('uses the bundled copy with zero network', async () => {
      useBundledDir(['echo-agent']);
      const counter = installFakeFetch();
      try {
        const dir = await hub.fetchProfile('echo-agent');
        assert.ok(fs.existsSync(path.join(dir, 'profile.yaml')));
        assert.ok(fs.existsSync(path.join(dir, 'bridge.py')));
        assert.strictEqual(counter.json, 0);
        assert.strictEqual(counter.text, 0);
      } finally {
        counter.restore();
      }
    });

    test('bundled NDJSON profile also caches _shared', async () => {
      // The pre-bundle bug: fetching claude-code did not bring _shared, so
      // bridge.py's `from _shared.stream_utils import ...` failed at runtime.
      useBundledDir(['claude-code']);
      const counter = installFakeFetch();
      try {
        await hub.fetchProfile('claude-code');
        assert.ok(fs.existsSync(path.join(hub.cacheRoot(), '_shared', 'stream_utils.py')));
        assert.strictEqual(counter.text, 0);
      } finally {
        counter.restore();
      }
    });

    test('falls back to remote for a profile not in the bundle', async () => {
      // Bundle only has echo-agent; requesting claude-code goes to jsDelivr.
      useBundledDir(['echo-agent']);
      const counter = installFakeFetch();
      try {
        const dir = await hub.fetchProfile('claude-code');
        assert.ok(fs.existsSync(path.join(dir, 'profile.yaml')));
        assert.ok(counter.text > 0);
      } finally {
        counter.restore();
      }
    });
  });

  // ----- listProfiles -----

  describe('listProfiles', () => {
    test('remote: returns all profile dirs with metadata', async () => {
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

    test('remote: skips underscore-prefixed utility dirs like _shared', async () => {
      const counter = installFakeFetch();
      try {
        const profiles = await hub.listProfiles({ refresh: true });
        const names = profiles.map(p => p.name);
        assert.ok(!names.some(n => n.startsWith('_')),
          `utility dir leaked into listing: ${names}`);
      } finally {
        counter.restore();
      }
    });

    test('remote: disk-caches the tree so repeat calls skip the data API', async () => {
      const counter = installFakeFetch();
      try {
        await hub.listProfiles({ refresh: true });
        assert.strictEqual(counter.json, 1);
        const treeCache = path.join(hub.cacheRoot(), 'tree.json');
        assert.ok(fs.existsSync(treeCache), 'tree.json not written to disk');
        await hub.listProfiles({ refresh: true });
        assert.strictEqual(counter.json, 1, 'second call hit the data API again');
      } finally {
        counter.restore();
      }
    });

    test('bundled: reads metadata locally with zero network', async () => {
      useBundledDir(['echo-agent', 'claude-code']);
      const counter = installFakeFetch();
      try {
        const profiles = await hub.listProfiles();
        const names = profiles.map(p => p.name).sort();
        assert.deepStrictEqual(names, ['claude-code', 'echo-agent']);
        assert.strictEqual(counter.json, 0);
        assert.strictEqual(counter.text, 0);
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

    test('installs _shared alongside so bridge imports resolve', async () => {
      const counter = installFakeFetch();
      const tmpTarget = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-install-'));
      try {
        await hub.installProfile('claude-code', tmpTarget, { refresh: true });
        assert.ok(fs.existsSync(path.join(tmpTarget, '_shared', 'stream_utils.py')),
          '_shared not installed — bridge.py import would fail');
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
