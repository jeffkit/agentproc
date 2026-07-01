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

function authHeaders({ json = false } = {}) {
  // Optional: an explicit token raises GitHub's anonymous rate limit from
  // 60 req/hour to 5,000. We accept either GITHUB_TOKEN (the env var GitHub
  // Actions injects) or GH_TOKEN (what `gh` CLI users typically have).
  const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN || '';
  const h = { 'User-Agent': 'agentproc-cli' };
  if (json) h.Accept = 'application/vnd.github+json';
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

async function httpGetJson(url) {
  let r;
  try {
    r = await fetch(url, { headers: authHeaders({ json: true }) });
  } catch (e) {
    throw new HubError(
      `could not reach GitHub while fetching hub profile`,
      {
        status: 0,
        cause: e,
        hint: [
          'This is usually a transient network issue. Try:',
          '  1. Re-run the command (often succeeds on retry).',
          '  2. If your network requires a proxy, set HTTPS_PROXY.',
          '  3. To avoid the network entirely, run against a local checkout:',
          '       agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"',
        ].join('\n'),
      }
    );
  }
  if (!r.ok) {
    const text = await r.text().catch(() => '');
    if (r.status === 403 || r.status === 429) {
      const authed = !!(process.env.GITHUB_TOKEN || process.env.GH_TOKEN);
      throw new HubError(
        `GitHub rate-limited the hub fetch (HTTP ${r.status})`,
        {
          status: r.status,
          hint: authed
            ? [
              'Your GITHUB_TOKEN is set but still rate-limited. Wait a few minutes and retry,',
              'or run against a local checkout instead:',
              '  agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"',
              '',
              `Not sure the profile name is right? Check with: agentproc hub list`,
            ].join('\n')
            : [
              'GitHub limits anonymous hub fetches to ~60/hour. To raise this to 5,000/hour:',
              '  export GITHUB_TOKEN=$(gh auth token)   # if you have the GitHub CLI',
              '  # or set GITHUB_TOKEN to any personal access token',
              '',
              'To skip the network entirely, run against a local checkout:',
              '  git clone https://github.com/jeffkit/agentproc && cd agentproc',
              '  agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"',
              '',
              `Not sure the profile name is right? Check with: agentproc hub list`,
            ].join('\n'),
        }
      );
    }
    if (r.status === 404) {
      throw new HubError(`profile not found on GitHub (HTTP 404)`, {
        status: 404,
        hint: 'Check the profile name with `agentproc hub list`. (Typos are case-sensitive.)',
      });
    }
    throw new HubError(`GitHub returned HTTP ${r.status} for hub fetch`, {
      status: r.status,
      hint: text.slice(0, 200) || 'No additional detail from GitHub.',
    });
  }
  return r.json();
}

async function httpGetText(url) {
  const r = await fetch(url, { headers: authHeaders({ json: false }) });
  if (!r.ok) {
    // raw.githubusercontent.com is essentially unrate-limited; a failure
    // here is more likely a genuine 404 (profile file missing) than 403.
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
 * and for detecting "profile does not exist" without burning an API call.
 */
async function httpGetTextOptional(url) {
  const r = await fetch(url, { headers: authHeaders({ json: false }) });
  if (r.status === 404) return null;
  if (!r.ok) {
    throw new HubError(`fetch failed (HTTP ${r.status}) for ${url}`, {
      status: r.status,
      hint: 'Profile files should exist in the hub repo. Try `agentproc hub list` to verify.',
    });
  }
  return r.text();
}

/**
 * Fetch the entire repo tree (1 API call, returns all paths under hub/).
 * Cached two ways: in-memory for the lifetime of the process, and on disk
 * at ~/.agentproc/cache/hub/tree.json with the same 24h TTL as profiles.
 *
 * The disk cache is the important one for rate-limit relief: the GitHub
 * Trees API is the single call that rate-limits anonymous users to ~60/hr,
 * and every CLI invocation is a fresh process (so the in-memory cache never
 * survives). With the disk cache, a normal user makes at most ~1 Trees API
 * call per day regardless of how many `hub list` / `hub run` they run.
 * @returns {Promise<Array<{path: string, type: 'blob'|'tree'}>>}
 */
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
 * List top-level profile names (the directories directly under hub/).
 * Cheap: uses the same disk-cached tree as getTree(), so calling this
 * after listRemoteFiles does not cost an extra API request.
 * @returns {Promise<string[]>}
 */
async function listProfileNames() {
  const tree = await getTree();
  const seen = new Set();
  for (const e of tree) {
    if (!e.path.startsWith('hub/')) continue;
    const seg = e.path.slice('hub/'.length).split('/')[0];
    // Directories prefixed with `_` (e.g. `_shared`) hold bridge utilities,
    // not profiles — exclude them from listings and "did you mean" suggestions.
    if (seg && !seg.startsWith('_') && !seen.has(seg)) seen.add(seg);
  }
  return [...seen].sort();
}

/**
 * Lightweight "did you mean" hint using edit distance + prefix matching.
 * Returns the best candidate name, or '' if none is close enough.
 *
 * Two paths to a match:
 *   1. Prefix match — `claude` matches `claude-code`, `echo` matches
 *      `echo-agent`. This is the common typo pattern (user forgot a suffix).
 *      Only accepts an unambiguous prefix — if multiple candidates share
 *      the prefix, none is returned (better no suggestion than a wrong one).
 *   2. Edit distance — tolerate ~1/3 of the input length in edits. Catches
 *      transpositions (`calude`) and small typos (`coudex` → `codex`).
 */
function suggestCloseName(input, candidates) {
  if (!input || !candidates || candidates.length === 0) return '';

  const n = input.toLowerCase();

  // Path 1: unique prefix match.
  const prefixMatches = candidates.filter(c => c.toLowerCase().startsWith(n));
  if (prefixMatches.length === 1) return prefixMatches[0];

  // Path 2: edit distance. Threshold scales with input length:
  //   - short (≤6): allow 1 edit (typos in `agy`, `codex`)
  //   - medium (7-12): allow 2 edits (transpositions in `calude-code`)
  //   - long (>12): allow 3 edits
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
        prev[j] + 1,        // deletion
        curr[j - 1] + 1,    // insertion
        prev[j - 1] + cost  // substitution
      );
    }
    for (let j = 0; j <= n; j++) prev[j] = curr[j];
  }
  return prev[n];
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

// Every hub profile is this fixed set of files (see hub/README.md):
//   profile.yaml (required) + bridge.py + bridge.js + README.md,
// with echo-agent additionally shipping bridge.sh. We fetch them directly
// via raw.githubusercontent.com (CDN, not rate-limited) so `hub run` never
// calls the GitHub Trees API in the happy path. If a future profile adds a
// new file type, extend this list.
const PROFILE_FILE_CANDIDATES = [
  'profile.yaml', 'bridge.py', 'bridge.js', 'bridge.sh', 'README.md',
];

/**
 * Fetch a profile directory to local cache. Returns the cache path.
 *
 * Fetches files directly via raw.githubusercontent.com (CDN, not
 * rate-limited) — no GitHub Trees API call in the happy path. Only an
 * unknown profile name (profile.yaml 404) falls back to the disk-cached
 * tree to produce a "did you mean" suggestion.
 *
 * @param {string} name
 * @param {{refresh?: boolean, onLog?: function(string): void}} [opts]
 * @returns {Promise<string>} absolute cache path
 */
async function fetchProfile(name, opts = {}) {
  const { refresh = false, onLog = null } = opts;

  // On refresh, clear the tree cache so we see newly-added profiles.
  if (refresh) clearTreeCache();

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

  // Probe profile.yaml via raw URL. raw.githubusercontent.com is CDN-backed
  // and not subject to the 60/hr anonymous API limit, so this does not burn
  // rate-limit budget.
  const probe = await httpGetTextOptional(GITHUB_RAW(`hub/${name}/profile.yaml`));
  if (probe === null) {
    // profile.yaml 404 → the name is wrong. Fall back to the (disk-cached)
    // tree to produce a "did you mean" suggestion. This is the only path
    // that may call the Trees API for `hub run`, and it's cached for 24h.
    const known = await listProfileNames();
    const suggestion = suggestCloseName(name, known);
    const hint = suggestion
      ? [`Did you mean \`${suggestion}\`?`, '', 'Available profiles:', ...known.map(n => `  - ${n}`)].join('\n')
      : ['Available profiles:', ...known.map(n => `  - ${n}`)].join('\n');
    throw new HubError(`profile '${name}' not found in hub`, {
      status: 404,
      hint,
    });
  }

  // Clear cache, then download the candidate file set via raw URLs.
  if (fs.existsSync(dir)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  fs.mkdirSync(dir, { recursive: true });

  // profile.yaml already fetched via the probe.
  fs.writeFileSync(path.join(dir, 'profile.yaml'), probe, 'utf8');
  if (onLog) onLog(`  - profile.yaml`);

  for (const fname of PROFILE_FILE_CANDIDATES) {
    if (fname === 'profile.yaml') continue;
    const text = await httpGetTextOptional(GITHUB_RAW(`hub/${name}/${fname}`));
    if (text === null) continue;  // optional file not present for this profile
    fs.writeFileSync(path.join(dir, fname), text, 'utf8');
    if (onLog) onLog(`  - ${fname}`);
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
    // Skip utility directories like `_shared/` — they hold shared bridge
    // helpers, not a runnable profile (no profile.yaml).
    if (name.startsWith('_')) continue;
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
  HubError,
  cacheRoot,
  cacheDir,
  cacheAgeSecs,
  clearTreeCache,
  fetchProfile,
  listProfiles,
  showReadme,
  installProfile,
};
