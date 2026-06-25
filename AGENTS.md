# AGENTS.md

Guidance for AI coding agents (Claude Code, Cursor, Aider, etc.) working in this repository.

## What this repo is

AgentProc is a minimal protocol specification that defines how a messaging-platform bridge talks to an agent process. The repo contains:

- The protocol spec (source of truth)
- Two reference SDKs (Python and Node.js) that implement the spec
- Examples (bare scripts + claude CLI wrappers)
- A VitePress documentation site

The protocol is the source of truth. **Everything else follows.** If you're changing SDK behavior, first check whether the spec already covers it; if not, the spec needs updating too.

## Repo layout

```
agentproc/
├── spec/                    # Protocol spec — the source of truth
│   ├── protocol.md          # English (canonical)
│   └── protocol.zh.md       # Chinese mirror (keep in sync)
├── sdk/
│   ├── python/              # `agentproc` on PyPI
│   │   ├── src/agentproc/   # the package
│   │   └── tests/           # pytest
│   └── node/                # `agentproc` on npm
│       └── src/             # the package (.js + .d.ts + .test.js)
├── examples/
│   ├── bash/                # echo agent (smoke test)
│   ├── python/              # claude_bridge.py
│   └── node/                # claude_bridge.js
├── hub/                     # drop-in profiles for real AI CLIs
│   ├── README.md            # hub conventions and schema
│   ├── claude-code/         # each profile: profile.yaml + bridge.py + bridge.js + README.md
│   ├── codex/
│   ├── codebuddy/
│   ├── agy/
│   └── echo-agent/
├── docs/                    # VitePress site (agentproc.dev)
│   ├── public/              # static files served at root (llms.txt, robots.txt)
│   ├── guide/  sdk/  hub/  examples/  spec/   # English content
│   └── zh/                  # Chinese mirror
├── .github/workflows/       # test.yml, publish.yml, docs.yml
├── CHANGELOG.md             # version history
├── CONTRIBUTING.md          # for human contributors (mostly mirrors this file)
└── AGENTS.md                # this file
```

## Hub profiles

`hub/<name>/` directories each contain a pure-P0 AgentProc profile for a real AI CLI. Both bridge scripts (Python and Node) MUST stay at parity — if you fix a bug in one, fix it in the other. See `hub/README.md` for the schema.

## The two SDKs MUST stay at parity

If you add a feature to one SDK, add the equivalent to the other and update both test suites. Naming convention:

| Python | Node.js |
|--------|---------|
| `create_profile` | `createProfile` |
| `AgentContext` (dataclass) | `ctx` (plain object) |
| `ctx.send_partial` | `ctx.sendPartial` |
| `ctx.send_error` | `ctx.sendError` |
| `ctx.session_id` | `ctx.sessionId` |
| `ctx.from_user` | `ctx.fromUser` |
| `ctx.image_url` | `ctx.imageUrl` |
| `ctx.file_url` | `ctx.fileUrl` |
| `ctx.protocol_version` | `ctx.protocolVersion` |
| `load_history` / `append_history` / `session_file_path` | `loadHistory` / `appendHistory` / `sessionFilePath` |
| `raise ProtocolError(msg)` | `throw await sdk.protocolError(msg)` |

## How to run tests

```bash
# Node
cd sdk/node
node --test src/index.test.js

# Python
cd sdk/python
PYTHONPATH=src pytest -q tests/
```

The CI matrix is Node 18 / 20 / 22 / 24 and Python 3.9 / 3.10 / 3.11 / 3.12 / 3.13. Don't use language features beyond the minimum supported version.

## How to build docs locally

```bash
cd docs
pnpm install
pnpm dev       # live preview at http://localhost:5173
pnpm build     # outputs to docs/.vitepress/dist/
```

The VitePress `base` is `/` because we serve from `agentproc.dev` root. Don't change it back to `/agentproc/` unless you know the domain setup has changed.

## Spec changes require a version bump

The protocol, the Python package, and the Node package are versioned in lockstep at `0.x.y`. Changes that add new env vars, new protocol lines, or change the meaning of existing ones require a version bump and a CHANGELOG entry.

Files that MUST be updated together when bumping:

- `spec/protocol.md` — `**Version:**` field at the top
- `spec/protocol.zh.md` — `**版本：**` field at the top
- `sdk/python/pyproject.toml` — `version`
- `sdk/node/package.json` — `version`
- `CHANGELOG.md` — new section
- `sdk/python/src/agentproc/__init__.py` — `PROTOCOL_VERSION` constant (only on minor bumps, e.g. `0.1` → `0.2`)
- `sdk/node/src/index.js` — `PROTOCOL_VERSION` constant (same)

Editorial changes (clarifications, rewording, new examples) don't require a version bump.

## English and Chinese must mirror

Every doc page under `docs/` has a mirror under `docs/zh/`. The spec is `spec/protocol.md` + `spec/protocol.zh.md`. If you only speak one language, open the PR anyway and flag the translation for someone else — don't let the missing translation block a substantive fix.

## Commit message style

Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`, `chore:`. Scope optional: `fix(sdk/node): ...`.

## What NOT to do

- **Don't add features the spec doesn't allow.** If you need a new env var or protocol line, change the spec first.
- **Don't skip the test suite when changing SDK behavior.** CI gates the publish workflow — broken tests block releases.
- **Don't change `docs/.vitepress/config.ts` `base` from `/`.** The site is served at `agentproc.dev` root.
- **Don't add a scoped package name** (`@agentproc/...`). The npm package name is `agentproc` (flat), matching the PyPI name.
- **Don't rewrite the spec's Design Rationale or Comparison sections casually.** Those reflect deliberate decisions documented after research.

## LLM-friendly entry points

If you're an LLM agent arriving at this repo for the first time, also read:

- `https://agentproc.dev/llms.txt` — short navigation README for LLMs
- `https://agentproc.dev/llms-full.txt` — full docs in one file
- `CONTRIBUTING.md` — for human contributors (overlaps with this file)
