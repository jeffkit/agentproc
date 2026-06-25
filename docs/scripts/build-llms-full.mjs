#!/usr/bin/env node
/**
 * Build /llms-full.txt by concatenating the canonical markdown sources.
 *
 * Strategy: read the raw markdown from the repo (spec + SDK sources + key
 * examples + llms.txt for navigation), prepend a small table of contents,
 * and write to docs/public/llms-full.txt so VitePress copies it to the
 * site root verbatim.
 *
 * Run with: `node scripts/build-llms-full.mjs` (called by `pnpm build`)
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');
const DOCS_PUBLIC = resolve(__dirname, '..', 'public');

// Files to include, in display order. Paths are relative to repo root.
// Each entry is [sourcePath, displayTitle].
const SECTIONS = [
  ['spec/protocol.md', 'Protocol Specification'],
  ['docs/sdk/python.md', 'Python SDK'],
  ['docs/sdk/node.md', 'Node.js SDK'],
  ['docs/cli/index.md', 'agentproc CLI'],
  ['docs/guide/getting-started.md', 'Quick Start'],
  ['docs/guide/what-is-agentproc.md', 'What is AgentProc?'],
  ['docs/hub/index.md', 'Profile Hub'],
  ['docs/examples/claude.md', 'Example: Connect the claude CLI'],
  ['docs/examples/bare.md', 'Example: Bare script (no SDK)'],
];

const SITE = 'https://agentproc.dev';

function header(title, sourcePath) {
  const url = sourcePath.startsWith('spec/')
    ? `${SITE}/spec/`
    : `${SITE}/${sourcePath.replace(/^docs\//, '').replace(/\.md$/, '')}`;
  return `\n\n---\n\n# ${title}\n\n> Source: ${url}  ·  Repository path: \`${sourcePath}\`\n`;
}

function build() {
  const parts = [];
  parts.push(`# AgentProc — Full Documentation (for LLMs)\n`);
  parts.push(`> Single-file dump of all AgentProc documentation, suitable for ingestion by language models. Human-readable site: ${SITE}\n`);
  parts.push(`> Protocol version: 0.1 · Package version: 0.1.1 · Generated from commit in repo.\n`);

  parts.push(`\n## Table of contents\n`);
  SECTIONS.forEach(([_, title]) => parts.push(`- ${title}`));
  parts.push('');

  for (const [sourcePath, title] of SECTIONS) {
    const full = join(REPO_ROOT, sourcePath);
    let content;
    try {
      content = readFileSync(full, 'utf8');
    } catch (e) {
      process.stderr.write(`warning: could not read ${full}: ${e.message}\n`);
      continue;
    }
    parts.push(header(title, sourcePath));
    parts.push(content.trim());
  }

  parts.push(`\n\n---\n\n## End of document\n`);
  parts.push(`Canonical URL: ${SITE}/llms-full.txt\n`);

  const out = parts.join('\n');
  mkdirSync(DOCS_PUBLIC, { recursive: true });
  const outPath = join(DOCS_PUBLIC, 'llms-full.txt');
  writeFileSync(outPath, out, 'utf8');

  const sizeKB = (Buffer.byteLength(out, 'utf8') / 1024).toFixed(1);
  process.stdout.write(`wrote ${outPath} (${sizeKB} KB, ${out.split('\n').length} lines)\n`);
}

build();
