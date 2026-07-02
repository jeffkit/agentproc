'use strict';
/**
 * Copy the canonical hub/ directory (at the repo root) into this npm package
 * as `hub/`, so `agentproc hub run` / `hub list` can read profiles with zero
 * network. Run by `prepublishOnly` before `npm publish` / `npm pack`.
 *
 * Excludes Python bytecode (__pycache__, *.pyc). Cross-platform (Node 18+,
 * uses fs.cpSync). Zero dependencies.
 */
const fs = require('node:fs');
const path = require('node:path');

const src = path.resolve(__dirname, '..', '..', '..', 'hub');
const dest = path.resolve(__dirname, '..', 'hub');

if (!fs.existsSync(src)) {
  console.error(`prepare-hub: source hub/ not found at ${src}`);
  process.exit(1);
}
if (fs.existsSync(dest)) fs.rmSync(dest, { recursive: true, force: true });
fs.cpSync(src, dest, {
  recursive: true,
  filter: (s) => {
    const base = path.basename(s);
    if (base === '__pycache__' || base.endsWith('.pyc')) return false;
    return true;
  },
});
console.log(`prepare-hub: copied ${src} -> ${dest}`);
