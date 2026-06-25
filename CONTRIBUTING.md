# Contributing to AgentProc

Thanks for your interest in improving AgentProc.

> **If you're an AI coding agent**, read [`AGENTS.md`](./AGENTS.md) instead — it's the same information optimized for tools like Claude Code and Cursor. This file is for human contributors.

## Project layout

- `spec/` — the protocol specification. EN at `protocol.md`, ZH at `protocol.zh.md`. These are the source of truth; everything else follows.
- `sdk/python/`, `sdk/node/` — reference SDKs that implement the spec.
- `examples/` — minimal agent scripts in bash / Python / Node.
- `docs/` — VitePress documentation site, English root and `zh/` mirror.
- `docs/public/` — static files served at the site root (`llms.txt`, `llms-full.txt`, `robots.txt`).
- `AGENTS.md` — guidance for AI coding agents working in this repo.

## Spec changes

The spec is versioned. Changes that add new env vars, new protocol lines, or change the meaning of existing ones require a version bump (e.g. `0.1` → `0.2`) and a CHANGELOG entry.

Editorial changes (clarifications, rewording, new examples) do not require a version bump but should still get a CHANGELOG line.

Keep `spec/protocol.md` and `spec/protocol.zh.md` in sync. If you only speak one language, open the PR anyway and flag the translation for someone else.

## SDK changes

Both SDKs should be at parity. If you add a feature to one, add the equivalent to the other and update both test suites.

### Running tests

```bash
# Node SDK
cd sdk/node
node --test src/index.test.js

# Python SDK
cd sdk/python
pip install pytest
PYTHONPATH=src pytest -q tests/
```

CI runs the same on every push and PR, across Node 18/20/22/24 and Python 3.9–3.13.

## Docs changes

The docs site lives under `docs/` (English) and `docs/zh/` (Chinese). Run locally with:

```bash
cd docs
pnpm install
pnpm dev
```

The English and Chinese sites should mirror each other. Sidebar config is in `docs/.vitepress/config.ts`.

## Versioning

The protocol version, Python package version, and Node package version are all kept in lockstep at `0.x.y`. To cut a release:

1. Update `spec/protocol.md` and `spec/protocol.zh.md` `Version:` field.
2. Update `sdk/python/pyproject.toml` `version`.
3. Update `sdk/node/package.json` `version`.
4. Update `CHANGELOG.md`.
5. Open a PR. Once merged, tag `vX.Y.Z` — the publish workflow will pick it up.

## Commit messages

Conventional Commits style: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`. Scope is optional.

## License

By contributing you agree that your contributions are licensed under the project's MIT license.
