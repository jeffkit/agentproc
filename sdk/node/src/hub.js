'use strict';
/**
 * Hub client — fetch and manage profile directories from the official Hub.
 *
 * The Hub lives at https://github.com/jeffkit/agentproc/tree/main/hub/
 *
 * Resolution order (so `hub run` / `hub list` work with zero network in the
 * common case, and stay usable where GitHub itself is unreachable, e.g. China):
 *
 *   1. Bundled copy — the entire hub/ directory is shipped inside this npm
 *      package (at <pkg>/hub/). `hub run` and `hub list` read from it
 *      directly. No network. This is the default and what most users hit.
 *   2. jsDelivr CDN — for `--refresh` or a profile newer than the installed
 *      CLI: files come from cdn.jsdelivr.net (Fastly CDN, not GitHub's
 *      rate-limited API), and the directory listing from jsDelivr's data
 *      API. jsDelivr is reachable in regions where raw.githubusercontent.com
 *      is not.
 *
 * Remote-fetched profiles are cached at ~/.agentproc/cache/hub/<name>/ with a
 * 24-hour TTL; the shared `_shared/` bridge helpers are cached alongside at
 * ~/.agentproc/cache/hub/_shared/ (the bridge scripts import them via a
 * sibling path).
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

// jsDelivr mirrors the GitHub repo on a global CDN (Fastly), reachable where
// raw.githubusercontent.com / api.github.com are not. No token, no 60/hr limit.
const JSDELIVR_RAW = (p) =>
  `https://cdn.jsdelivr.net/gh/${HUB_REPO}@${HUB_REF}/${p}`;
const JSDELIVR_DATA =
  `https://data.jsdelivr.com/v1/packages/gh/${HUB_REPO}@${HUB_REF}`;

// The hub directory shipped inside this npm package. Defaults to <pkg>/hub/
// (this file is at <pkg>/src/hub.js). Overridable via setBundledHubDir() for
// tests.
let _bundledHubDir = path.resolve(__dirname, '..', 'hub');
function bundledHubDir() { return _bundledHubDir; }
function setBundledHubDir(p) { _bundledHubDir = p; }
function bundledHas(name) {
  return fs.existsSync(path.join(_bundledHubDir, name, 'profile.yaml'));
}

// Every hub profile is this fixed set of files (see hub/README.md):
//   profile.yaml (required) + bridge.py + bridge.js + README.md,
// with echo-agent additionally shipping bridge.sh. `_shared/` ships
// stream_utils.{py,js} + README.md. If a future profile adds a new file
// type, extend these lists.
const PROFILE_FILE_CANDIDATES = [
  'profile.yaml', 'bridge.py', 'bridge.js', 'bridge.sh', 'README.md',
];
const SHARED_FILE_CANDIDATES = ['stream_utils.py', 'stream_utils.js', 'README.md'];

// Exclude Python bytecode / editor cruft when copying bundled dirs to cache.
const COPY_FILTER = (src) => {
  const base = path.basename(src);
  if (base === '__pycache__' || base.endsWith('.pyc')) return false;
  return true;
};

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

/**
 * Custom error type for hub fetch failures. Carries a short, user-facing
 * `hint` with remediation, so the CLI can print something helpful instead
 * of a raw Node stack trace.
 */
class HubError extends Error {
  constructor(message, { hint = '', cause = null, status = 0 } = {}) {
    super(message);
    this.name = 'HubError';
    this.hint = hint;
    this.status = status;
    if (cause) this.cause = cause;
  }
}

function httpHeaders() {
  return { 'User-Agent': 'agentproc-cli' };
}

const NETWORK_HINT = [
  'Could not reach the hub CDN (jsDelivr). Try:',
  '  1. Re-run the command (often succeeds on retry).',
  '  2. If your network requires a proxy, set HTTPS_PROXY.',
  '  3. Profiles ship bundled with this CLI, so the common case needs no',
  '     network. To use a local checkout instead:',
  '       agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"',
].join('\n');

async function httpGetJson(url) {
  let r;
  try {
    r = await fetch(url, { headers: httpHeaders() });
  } catch (e) {
    throw new HubError(`could not reach the hub CDN`, {
      status: 0, cause: e, hint: NETWORK_HINT,
    });
  }
  if (!r.ok) {
    const text = await r.text().catch(() => '');
    throw new HubError(`hub CDN returned HTTP ${r.status}`, {
      status: r.status,
      hint: text.slice(0, 200) || NETWORK_HINT,
    });
  }
  return r.json();
}

async function httpGetText(url) {
  const r = await fetch(url, { headers: httpHeaders() });
  if (!r.ok) {
    throw new HubError(`fetch failed (HTTP ${r.status}) for ${url}`, {
      status: r.status,
      hint: 'Profile files should exist in the hub repo. Try `agentproc hub list` to verify.',
    });
  }
  return r.text();
}

/**
 * Like httpGetText, but returns null on 404 instead of throwing. Used for
 * probing optional profile files (e.g. bridge.sh only exists for echo-agent)
 * and for detecting "profile does not exist" without a separate API call.
 */
async function httpGetTextOptional(url) {
  const r = await fetch(url, { headers: httpHeaders() });
  if (r.status === 404) return null;
  if (!r.ok) {
    throw new HubError(`fetch failed (HTTP ${r.status}) for ${url}`, {
      status: r.status,
      hint: 'Profile files should exist in the hub repo. Try `agentproc hub list` to verify.',
    });
  }
  return r.text();
}

// ---------------------------------------------------------------------------
// Repo tree (jsDelivr data API) — cached in-memory and on disk (24h TTL)
// ---------------------------------------------------------------------------

let _treeCache = null;

function treeCachePath() {
  return path.join(cacheRoot(), 'tree.json');
}

function clearTreeCache() {
  _treeCache = null;
  const p = treeCachePath();
  if (fs.existsSync(p)) {
    try { fs.unlinkSync(p); } catch { /* best effort */ }
  }
}

/**
 * Flatten jsDelivr's nested {files:[{type:'directory',files:[...]}]} tree
 * into the flat [{path, type:'blob'|'tree'}] shape the rest of this module
 * already expects (same shape GitHub's Trees API returned).
 */
function flattenJsdelivrTree(files, prefix = '') {
  const out = [];
  for (const e of files) {
    if (!e || typeof e !== 'object') continue;
    const p = prefix + String(e.name || '');
    if (e.type === 'directory') {
      out.push({ path: p, type: 'tree' });
      if (Array.isArray(e.files)) out.push(...flattenJsdelivrTree(e.files, p + '/'));
    } else {
      out.push({ path: p, type: 'blob' });
    }
  }
  return out;
}

/**
 * Fetch the entire repo tree from jsDelivr's data API (1 call, not
 * rate-limited like GitHub's Trees API). Cached in-memory and on disk at
 * ~/.agentproc/cache/hub/tree.json (24h TTL) so repeat `hub list --refresh`
 * calls don't re-hit the network.
 * @returns {Promise<Array<{path: string, type: 'blob'|'tree'}>>}
 */
async function getTree() {
  if (_treeCache) return _treeCache;

  const tp = treeCachePath();
  if (fs.existsSync(tp)) {
    try {
      const meta = JSON.parse(fs.readFileSync(tp, 'utf8'));
      const age = Math.max(0, Date.now() / 1000 - (meta.fetched_at || 0));
      if (age < HUB_CACHE_TTL_SECS && Array.isArray(meta.tree)) {
        _treeCache = meta.tree.map((e) => ({
          path: String((e && e.path) || ''),
          type: String((e && e.type) || ''),
        }));
        return _treeCache;
      }
    } catch { /* corrupt cache file — refetch */ }
  }

  const data = await httpGetJson(JSDELIVR_DATA);
  if (!data || !Array.isArray(data.files)) {
    throw new Error('unexpected jsDelivr data API response');
  }
  _treeCache = flattenJsdelivrTree(data.files);

  fs.mkdirSync(cacheRoot(), { recursive: true });
  try {
    fs.writeFileSync(tp, JSON.stringify({
      fetched_at: Date.now() / 1000,
      ref: HUB_REF,
      tree: _treeCache,
    }), 'utf8');
  } catch { /* disk cache is best-effort */ }

  return _treeCache;
}

/**
 * List top-level entries under a hub subpath (e.g. 'hub/' → all profile dirs).
 * Uses the disk-cached tree.
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
 * List top-level profile names (directories directly under hub/), excluding
 * `_`-prefixed utility dirs like `_shared`. Uses the bundled copy if present
 * (no network), else the disk-cached remote tree.
 * @returns {Promise<string[]>}
 */
async function listProfileNames() {
  if (fs.existsSync(_bundledHubDir)) {
    return fs.readdirSync(_bundledHubDir, { withFileTypes: true })
      .filter((e) => e.isDirectory() && !e.name.startsWith('_') &&
        fs.existsSync(path.join(_bundledHubDir, e.name, 'profile.yaml')))
      .map((e) => e.name)
      .sort();
  }
  const tree = await getTree();
  const seen = new Set();
  for (const e of tree) {
    if (!e.path.startsWith('hub/')) continue;
    const seg = e.path.slice('hub/'.length).split('/')[0];
    if (seg && !seg.startsWith('_') && !seen.has(seg)) seen.add(seg);
  }
  return [...seen].sort();
}

/**
 * Lightweight "did you mean" hint using edit distance + prefix matching.
 */
function suggestCloseName(input, candidates) {
  if (!input || !candidates || candidates.length === 0) return '';

  const n = input.toLowerCase();

  const prefixMatches = candidates.filter(c => c.toLowerCase().startsWith(n));
  if (prefixMatches.length === 1) return prefixMatches[0];

  const threshold = input.length <= 6 ? 1 : input.length <= 12 ? 2 : 3;
  let best = '';
  let bestDist = Infinity;
  for (const c of candidates) {
    const dist = editDistance(n, c.toLowerCase());
    if (dist < bestDist) { bestDist = dist; best = c; }
  }
  if (best && bestDist <= threshold) return best;
  return '';
}

function editDistance(a, b) {
  const m = a.length, n = b.length;
  if (m === 0) return n;
  if (n === 0) return m;
  const prev = new Array(n + 1);
  const curr = new Array(n + 1);
  for (let j = 0; j <= n; j++) prev[j] = j;
  for (let i = 1; i <= m; i++) {
    curr[0] = i;
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(
        prev[j] + 1,
        curr[j - 1] + 1,
        prev[j - 1] + cost
      );
    }
    for (let j = 0; j <= n; j++) prev[j] = curr[j];
  }
  return prev[n];
}

// ---------------------------------------------------------------------------
// Cache population — from the bundled copy or from jsDelivr
// ---------------------------------------------------------------------------

function clearDir(dir) {
  if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
}

function copyBundledDir(subname, destDir) {
  const src = path.join(_bundledHubDir, subname);
  if (!fs.existsSync(src)) return false;
  clearDir(destDir);
  fs.cpSync(src, destDir, { recursive: true, filter: COPY_FILTER });
  return true;
}

/**
 * Ensure `_shared/` is present in the cache root, copying from the bundle or
 * fetching from jsDelivr. Bridges do `from _shared.stream_utils import ...`
 * with the cache root on sys.path, so this must be populated whenever a
 * profile is fetched. Skipped if a fresh _shared cache already exists.
 */
async function ensureSharedCached({ refresh, onLog }) {
  const age = cacheAgeSecs('_shared');
  const sdir = cacheDir('_shared');
  if (!refresh && age !== null && age < HUB_CACHE_TTL_SECS &&
      fs.existsSync(path.join(sdir, 'stream_utils.py'))) {
    return;
  }
  if (fs.existsSync(_bundledHubDir)) {
    if (copyBundledDir('_shared', sdir)) {
      writeCacheMeta('_shared');
      return;
    }
  }
  // Remote: fetch the candidate file set via jsDelivr raw URLs.
  clearDir(sdir);
  for (const fname of SHARED_FILE_CANDIDATES) {
    const text = await httpGetTextOptional(JSDELIVR_RAW(`hub/_shared/${fname}`));
    if (text === null) continue;
    fs.writeFileSync(path.join(sdir, fname), text, 'utf8');
    if (onLog) onLog(`  - _shared/${fname}`);
  }
  writeCacheMeta('_shared');
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Fetch a profile directory to local cache. Returns the cache path.
 *
 * Resolution: fresh cache → bundled copy (default, zero network) → jsDelivr
 * CDN (for --refresh or a profile not in the bundle). `_shared/` is populated
 * alongside so the bridge scripts can import it.
 *
 * @param {string} name
 * @param {{refresh?: boolean, onLog?: function(string): void}} [opts]
 * @returns {Promise<string>} absolute cache path
 */
async function fetchProfile(name, opts = {}) {
  const { refresh = false, onLog = null } = opts;

  if (refresh) clearTreeCache();

  const age = cacheAgeSecs(name);
  const dir = cacheDir(name);
  const profileYaml = path.join(dir, 'profile.yaml');

  if (!refresh && age !== null && age < HUB_CACHE_TTL_SECS && fs.existsSync(profileYaml)) {
    if (onLog) onLog(`using cached profile: ${dir} (age ${Math.floor(age)}s)`);
    return dir;
  }

  // 1) Bundled fast path — zero network, the default for most users.
  if (!refresh && bundledHas(name)) {
    if (onLog) onLog(`using bundled profile: ${name}`);
    copyBundledDir(name, dir);
    writeCacheMeta(name);
    await ensureSharedCached({ refresh, onLog });
    return dir;
  }

  if (onLog) {
    if (refresh) onLog(`refreshing profile '${name}' from jsDelivr CDN...`);
    else onLog(`fetching profile '${name}' from jsDelivr CDN...`);
  }

  // 2) Remote via jsDelivr. Probe profile.yaml first.
  const probe = await httpGetTextOptional(JSDELIVR_RAW(`hub/${name}/profile.yaml`));
  if (probe === null) {
    // profile.yaml 404 → wrong name. Produce a "did you mean" hint from the
    // bundled listing (no network) or the disk-cached remote tree.
    const known = await listProfileNames();
    const suggestion = suggestCloseName(name, known);
    const hint = suggestion
      ? [`Did you mean \`${suggestion}\`?`, '', 'Available profiles:', ...known.map(n => `  - ${n}`)].join('\n')
      : ['Available profiles:', ...known.map(n => `  - ${n}`)].join('\n');
    throw new HubError(`profile '${name}' not found in hub`, { status: 404, hint });
  }

  clearDir(dir);
  fs.writeFileSync(path.join(dir, 'profile.yaml'), probe, 'utf8');
  if (onLog) onLog(`  - profile.yaml`);

  for (const fname of PROFILE_FILE_CANDIDATES) {
    if (fname === 'profile.yaml') continue;
    const text = await httpGetTextOptional(JSDELIVR_RAW(`hub/${name}/${fname}`));
    if (text === null) continue;
    fs.writeFileSync(path.join(dir, fname), text, 'utf8');
    if (onLog) onLog(`  - ${fname}`);
  }

  writeCacheMeta(name);
  await ensureSharedCached({ refresh, onLog });
  return dir;
}

/**
 * List profiles in the official hub.
 *
 * Reads from the bundled copy by default (zero network). With refresh=true
 * (or no bundle) it queries jsDelivr's data API and fetches each profile.yaml
 * for metadata.
 *
 * @param {{refresh?: boolean, onLog?: function(string): void}} [opts]
 * @returns {Promise<Array<{name: string, description: string, cli: string, tested: string}>>}
 */
async function listProfiles(opts = {}) {
  const { refresh = false, onLog = null } = opts;
  const { parseYaml } = require('./yaml.js');

  if (!refresh && fs.existsSync(_bundledHubDir)) {
    const entries = fs.readdirSync(_bundledHubDir, { withFileTypes: true })
      .filter((e) => e.isDirectory() && !e.name.startsWith('_'));
    const profiles = [];
    for (const entry of entries) {
      const yamlPath = path.join(_bundledHubDir, entry.name, 'profile.yaml');
      if (!fs.existsSync(yamlPath)) continue;
      try {
        const data = parseYaml(fs.readFileSync(yamlPath, 'utf8'));
        profiles.push({
          name: String(data.name || entry.name),
          description: String(data.description || ''),
          cli: String(data.cli || ''),
          tested: String(data.tested || 'unverified'),
        });
      } catch (e) {
        if (onLog) onLog(`warning: could not read metadata for ${entry.name}: ${e.message}`);
        profiles.push({
          name: entry.name,
          description: '(failed to read metadata)',
          cli: '',
          tested: 'unverified',
        });
      }
    }
    return profiles;
  }

  const entries = await listRemoteFiles('hub');
  const profiles = [];
  for (const entry of entries) {
    if (entry.type !== 'dir') continue;
    const name = entry.name;
    if (name.startsWith('_')) continue;
    try {
      const yamlText = await httpGetText(JSDELIVR_RAW(`hub/${name}/profile.yaml`));
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
 * Copy a profile into targetDir/<name>/, along with `_shared/` (the bridge
 * scripts import from it via a sibling path).
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
  fs.cpSync(cached, dest, { recursive: true, filter: COPY_FILTER });
  // Drop our cache meta file from the installed copy.
  const meta = path.join(dest, '.cache-meta.json');
  if (fs.existsSync(meta)) fs.unlinkSync(meta);
  // Also install `_shared/` so the bridge's `from _shared.stream_utils import`
  // resolves against targetDir.
  const sharedSrc = cacheDir('_shared');
  const sharedDest = path.join(targetDir, '_shared');
  if (fs.existsSync(sharedSrc) && !fs.existsSync(sharedDest)) {
    fs.cpSync(sharedSrc, sharedDest, { recursive: true, filter: COPY_FILTER });
  }
  if (opts.onLog) opts.onLog(`installed to: ${dest}`);
  return dest;
}

module.exports = {
  HUB_REPO,
  HUB_REF,
  HUB_CACHE_TTL_SECS,
  HubError,
  cacheRoot,
  cacheDir,
  cacheAgeSecs,
  clearTreeCache,
  setBundledHubDir,
  fetchProfile,
  listProfiles,
  showReadme,
  installProfile,
};
