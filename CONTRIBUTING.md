# Contributing to AgentProc

Thanks for your interest in improving AgentProc.

> **If you're an AI coding agent**, read [`AGENTS.md`](./AGENTS.md) instead — it's the same information optimized for tools like Claude Code and Cursor. This file is for human contributors.

## Project layout

- `spec/` — the protocol specification. EN at `protocol.md`, ZH at `protocol.zh.md`. These are the source of truth; everything else follows.
- `sdk/python/`, `sdk/node/` — reference SDKs that implement the spec.
- `examples/` — minimal agent scripts in bash / Python / Node.
- `hub/` — drop-in AgentProc profiles for popular AI CLIs (14 profiles: claude-code, codex, codebuddy, gemini-cli, cursor, qwen-code, opencode, kimi-code, recursive, agy, aider, pi, deepseek, echo-agent). Each profile is a directory with `profile.yaml`, `bridge.py`, `bridge.js`, `README.md`.
- `docs/` — VitePress documentation site, English root and `zh/` mirror.
- `docs/public/` — static files served at the site root (`llms.txt`, `llms-full.txt`, `robots.txt`).
- `AGENTS.md` — guidance for AI coding agents working in this repo.

## Adding a profile to the hub

See [`hub/README.md`](./hub/README.md) for the schema and contribution flow. Briefly: each profile is a self-contained directory under `hub/<name>/` with a pure-P0 `profile.yaml`, a `bridge.py` and `bridge.js` at parity, and a `README.md`. Set `tested: unverified` unless you've verified end-to-end.

## Spec changes

The spec is versioned. Changes that add new env vars, new protocol lines, or change the meaning of existing ones require a version bump and a CHANGELOG entry. Note that the wire protocol version (currently `0.4`) and the SDK package version (currently `0.10.1`) are tracked independently — see CHANGELOG.md for the three-track versioning model.

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

Three version tracks are maintained independently (see `CHANGELOG.md` for details):

- **Wire protocol** — currently `"0.4"` (the string in the `protocol_version` field). Changes only when the bytes on stdin/stdout change.
- **Spec document revision** — currently `1.2`. Tracks editorial changes to `spec/protocol.md`.
- **SDK package version** — currently `0.10.1`. Both Python and Node packages share the same version.

To cut a release:

1. Bump `sdk/python/pyproject.toml` `version` and `sdk/node/package.json` `version` to the new SDK version.
2. If the wire protocol changed, update the `PROTOCOL_VERSION` constant in `sdk/node/src/runner.js` and `sdk/python/src/agentproc/runner.py`.
3. If `spec/protocol.md` changed substantively, bump its `**Document revision:**` field.
4. Update `CHANGELOG.md` with a new section.
5. Open a PR. Once merged, tag `vX.Y.Z` — the publish workflow will pick it up.

## Commit messages

Conventional Commits style: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`. Scope is optional.

## License

By contributing you agree that your contributions are licensed under the project's MIT license.
