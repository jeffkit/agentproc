'use strict';
/**
 * Hub client — fetch and manage profile directories from the official Hub.
 *
 * The Hub lives at https://github.com/jeffkit/agentproc/tree/main/hub/
 * Profiles are cached locally at ~/.agentproc/cache/hub/<name>/ with a
 * 24-hour TTL. Pass refresh=true to force re-fetch.
 *
 * Public API:
 *   HUB_REPO            — 'jeffkit/agentproc'
 *   HUB_REF             — 'main'
 *   HUB_CACHE_TTL_SECS  — 24 hours
 *   cacheDir(name)      — Path to the local cache directory for a profile
 *   fetchProfile(name, opts) -> Promise<string>
 *   listProfiles(opts)  -> Promise<Array<{name, description, cli, tested}>>
 *   showReadme(name, opts) -> Promise<string>
 *   installProfile(name, targetDir, opts) -> Promise<string>
 *
 * All network access is via global fetch() (Node 18+). Zero dependencies.
 */

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const HUB_REPO = 'jeffkit/agentproc';
const HUB_REF = 'main';
const HUB_CACHE_TTL_SECS = 24 * 60 * 60;  // 24 hours

const GITHUB_API = (subpath) =>
  `https://api.github.com/repos/${HUB_REPO}/contents/${subpath}?ref=${HUB_REF}`;
const GITHUB_TREES = `https://api.github.com/repos/${HUB_REPO}/git/trees/${HUB_REF}?recursive=1`;
const GITHUB_RAW = (p) =>
  `https://raw.githubusercontent.com/${HUB_REPO}/${HUB_REF}/${p}`;

// ---------------------------------------------------------------------------
// Cache helpers
// ---------------------------------------------------------------------------

function cacheRoot() {
  // Prefer process.env.HOME (overridable in tests, set by sudo -E etc.),
  // fall back to os.homedir() (cached at first call on some platforms).
  const home = process.env.HOME || os.homedir();
  return path.join(home, '.agentproc', 'cache', 'hub');
}

function cacheDir(name) {
  return path.join(cacheRoot(), name);
}

function cacheAgeSecs(name) {
  const marker = path.join(cacheDir(name), '.cache-meta.json');
  if (!fs.existsSync(marker)) return null;
  try {
    const meta = JSON.parse(fs.readFileSync(marker, 'utf8'));
    const ts = meta.fetched_at || 0;
    return Math.max(0, Date.now() / 1000 - ts);
  } catch {
    return null;
  }
}

function writeCacheMeta(name) {
  const dir = cacheDir(name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, '.cache-meta.json'),
    JSON.stringify({ fetched_at: Date.now() / 1000, ref: HUB_REF }),
    'utf8'
  );
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async function httpGetJson(url) {
  const r = await fetch(url, {
    headers: {
      Accept: 'application/vnd.github+json',
      'User-Agent': 'agentproc-cli',
    },
  });
  if (!r.ok) {
    const text = await r.text().catch(() => '');
    throw new Error(`GitHub API ${r.status}: ${text.slice(0, 200)}`);
  }
  return r.json();
}

async function httpGetText(url) {
  const r = await fetch(url, { headers: { 'User-Agent': 'agentproc-cli' } });
  if (!r.ok) {
    throw new Error(`fetch ${r.status}: ${url}`);
  }
  return r.text();
}

/**
 * Fetch the entire repo tree (1 API call, returns all paths under hub/).
 * Cached in memory for the lifetime of the process.
 * @returns {Promise<Array<{path: string, type: 'blob'|'tree'}>>}
 */
let _treeCache = null;
async function getTree() {
  if (_treeCache) return _treeCache;
  const data = await httpGetJson(GITHUB_TREES);
  if (!data || !Array.isArray(data.tree)) {
    throw new Error('unexpected tree API response');
  }
  _treeCache = data.tree
    .filter((e) => e && typeof e === 'object')
    .map((e) => ({
      path: String(e.path || ''),
      type: String(e.type || ''),  // 'blob' or 'tree'
    }));
  return _treeCache;
}

/**
 * List top-level entries under a hub subpath (e.g. 'hub/' → all profile dirs).
 * @param {string} subpath  e.g. 'hub' or 'hub/claude-code'
 * @returns {Promise<Array<{name: string, type: 'file'|'dir'}>>}
 */
async function listRemoteFiles(subpath) {
  if (!subpath.endsWith('/')) subpath = subpath + '/';
  const tree = await getTree();
  const seen = new Set();
  const out = [];
  for (const e of tree) {
    if (!e.path.startsWith(subpath)) continue;
    const name = e.path.slice(subpath.length).split('/')[0];
    if (!name || seen.has(name)) continue;
    seen.add(name);
    // Determine type: is there a path equal to subpath+name with type 'tree'?
    const isDir = tree.some((t) => t.path === subpath + name && t.type === 'tree');
    out.push({
      name,
      type: isDir ? 'dir' : 'file',
      path: e.path,
      download_url: '',
    });
  }
  return out;
}

/**
 * List actual files inside a hub/<name>/ directory.
 * @param {string} name
 * @returns {Promise<Array<{name: string, path: string}>>}
 */
async function listRemoteProfileFiles(name) {
  const prefix = `hub/${name}/`;
  const tree = await getTree();
  return tree
    .filter((e) => e.type === 'blob' && e.path.startsWith(prefix))
    .map((e) => ({
      name: e.path.slice(prefix.length).split('/').pop(),
      path: e.path,
    }));
}

async function downloadFile(remotePath, localPath) {
  const text = await httpGetText(GITHUB_RAW(remotePath));
  fs.mkdirSync(path.dirname(localPath), { recursive: true });
  fs.writeFileSync(localPath, text, 'utf8');
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Fetch a profile directory to local cache. Returns the cache path.
 *
 * @param {string} name
 * @param {{refresh?: boolean, onLog?: function(string): void}} [opts]
 * @returns {Promise<string>} absolute cache path
 */
async function fetchProfile(name, opts = {}) {
  const { refresh = false, onLog = null } = opts;

  // On refresh, also clear the in-memory tree cache so we see new files
  // (e.g. profiles added since the process started).
  if (refresh) _treeCache = null;

  const age = cacheAgeSecs(name);
  const dir = cacheDir(name);
  const profileYaml = path.join(dir, 'profile.yaml');

  if (!refresh && age !== null && age < HUB_CACHE_TTL_SECS && fs.existsSync(profileYaml)) {
    if (onLog) onLog(`using cached profile: ${dir} (age ${Math.floor(age)}s)`);
    return dir;
  }

  if (onLog) {
    if (refresh) {
      onLog(`refreshing profile '${name}' from ${HUB_REPO}:${HUB_REF}...`);
    } else {
      onLog(`fetching profile '${name}' from ${HUB_REPO}:${HUB_REF}...`);
    }
  }

  const entries = await listRemoteProfileFiles(name);
  if (entries.length === 0) {
    throw new Error(`profile '${name}' not found in hub`);
  }

  // Clear cache, then re-download every file in the profile directory.
  if (fs.existsSync(dir)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  fs.mkdirSync(dir, { recursive: true });

  for (const entry of entries) {
    const local = path.join(dir, entry.name);
    await downloadFile(entry.path, local);
    if (onLog) onLog(`  - ${entry.name}`);
  }

  writeCacheMeta(name);
  return dir;
}

/**
 * List profiles in the official hub.
 *
 * @param {{refresh?: boolean, onLog?: function(string): void}} [opts]
 * @returns {Promise<Array<{name: string, description: string, cli: string, tested: string}>>}
 */
async function listProfiles(opts = {}) {
  const { onLog = null } = opts;
  const entries = await listRemoteFiles('hub');
  const profiles = [];
  for (const entry of entries) {
    if (entry.type !== 'dir') continue;
    const name = entry.name;
    try {
      const yamlText = await httpGetText(GITHUB_RAW(`hub/${name}/profile.yaml`));
      const { parseYaml } = require('./cli.js');
      const data = parseYaml(yamlText);
      profiles.push({
        name: String(data.name || name),
        description: String(data.description || ''),
        cli: String(data.cli || ''),
        tested: String(data.tested || 'unverified'),
      });
    } catch (e) {
      if (onLog) onLog(`warning: could not read metadata for ${name}: ${e.message}`);
      profiles.push({
        name,
        description: '(failed to read metadata)',
        cli: '',
        tested: 'unverified',
      });
    }
  }
  return profiles;
}

/**
 * Return the README.md content for a profile.
 *
 * @param {string} name
 * @param {{refresh?: boolean, onLog?: function(string): void}} [opts]
 * @returns {Promise<string>}
 */
async function showReadme(name, opts = {}) {
  const dir = await fetchProfile(name, opts);
  const readme = path.join(dir, 'README.md');
  if (!fs.existsSync(readme)) {
    return `(no README.md for profile '${name}')`;
  }
  return fs.readFileSync(readme, 'utf8');
}

/**
 * Copy a cached profile into targetDir/<name>/.
 *
 * @param {string} name
 * @param {string} targetDir
 * @param {{refresh?: boolean, onLog?: function(string): void}} [opts]
 * @returns {Promise<string>} destination path
 */
async function installProfile(name, targetDir, opts = {}) {
  const cached = await fetchProfile(name, opts);
  const dest = path.join(targetDir, name);
  if (fs.existsSync(dest)) {
    throw new Error(`target already exists: ${dest}`);
  }
  fs.cpSync(cached, dest, { recursive: true });
  // Drop our cache meta file from the installed copy.
  const meta = path.join(dest, '.cache-meta.json');
  if (fs.existsSync(meta)) fs.unlinkSync(meta);
  if (opts.onLog) opts.onLog(`installed to: ${dest}`);
  return dest;
}

module.exports = {
  HUB_REPO,
  HUB_REF,
  HUB_CACHE_TTL_SECS,
  cacheRoot,
  cacheDir,
  cacheAgeSecs,
  fetchProfile,
  listProfiles,
  showReadme,
  installProfile,
};
