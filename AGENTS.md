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
│   └── node/                # `agentproc` on npm (SDK + CLI)
│       └── src/             # index.js (SDK), cli.js (CLI), runner.js (core), tests
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
│   ├── guide/  sdk/  cli/  hub/  examples/  spec/   # English content
│   └── zh/                  # Chinese mirror
├── .github/workflows/       # test.yml, publish.yml, docs.yml
├── CHANGELOG.md             # version history
├── CONTRIBUTING.md          # for human contributors (mostly mirrors this file)
└── AGENTS.md                # this file
```

## Hub profiles

`hub/<name>/` directories each contain a pure-P0 AgentProc profile for a real AI CLI. Each profile ships a Python bridge (`bridge.py`) and a Node bridge (`bridge.js`) that wrap the same underlying CLI, plus the shared `profile.yaml` and `README.md`. The two bridges SHOULD stay at parity at the **observable** level (same NDJSON event → same AgentProc output for the same CLI), not the code level — they are free to differ in implementation detail (stderr buffering, error-message wording, control flow). If you fix a *spec-relevant* bug in one, fix it in the other; if you change a friendly-hint string in one, the other does not have to match. See `hub/README.md` for the schema.

## The two SDKs aim to mirror, not to stay in lockstep

The Python and Node SDKs are independent implementations of the same spec. They SHOULD agree on **observable behaviour** (same profile + message → same `reply` / `sessionId` / `error` / `exitCode`; same env injection; same timeout/SIGTERM semantics), verified by the shared `spec/conformance/` suite. They are NOT required to mirror at the code level — implementation details (how stderr is buffered, what friendly error-hint regexes a runner uses, threading vs event-loop) are each SDK's own business.

Naming convention (kept as a convenience for users who switch between the two, not a hard contract):

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

If you add a *spec-relevant* feature to one SDK, add the equivalent observable behaviour to the other and extend the conformance suite. A change that only affects one SDK's implementation detail (e.g. a better friendly-error regex) does not require touching the other.

## The Node SDK also ships the CLI

`sdk/node/src/` contains three modules with distinct roles:

- `index.js` — the SDK (call from code: `createProfile(handler)`)
- `runner.js` — the canonical bridge-side engine (`run(profile, options)`); the **spec in code form**
- `cli.js` — thin wrapper that turns `runner.js` into a command-line tool

If you change bridge-side behavior in `runner.js` (e.g. how stdout lines are classified, how timeouts fire, how env vars inject), you are changing the canonical implementation of the spec — bump the version, update CHANGELOG, and update `spec/protocol.md` if the behavior is spec-relevant. Tests in `runner.test.js` must keep passing.

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

The spec document, the Python package, and the Node package each carry their own version (see `CHANGELOG.md` for the three tracks). Changes that add new env vars, new protocol lines, or change the meaning of existing ones require an SDK version bump and a CHANGELOG entry.

Files that MUST be updated together when bumping the SDK package version:

- `spec/protocol.md` — `**Version:**` field at the top (only if the wire protocol itself changes; the wire version is `0.1` and is separate from the doc revision)
- `spec/protocol.zh.md` — `**版本：**` field at the top (same)
- `sdk/python/pyproject.toml` — `version`
- `sdk/node/package.json` — `version`
- `CHANGELOG.md` — new section

The `PROTOCOL_VERSION` constant (the wire string `0.1`) has a single source of truth per SDK: `sdk/python/src/agentproc/runner.py` and `sdk/node/src/runner.js`. The package entry points (`__init__.py` / `index.js`) **re-export** it from the runner — do not copy the literal into the entry point. The wire version only bumps on a minor (e.g. `0.1` → `0.2`) when the bytes on stdin/stdout actually change; most SDK releases keep `0.1`.

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
- **Don't hand-roll a YAML parser.** The Node SDK depends on `js-yaml`; profile YAML parsing goes through it. (The Python SDK uses the standard library where it can and ships a bundled hub copy.) A "zero-dependency" profile parser was retired after it silently broke `streaming: false  # comment` by not stripping inline comments — don't bring it back.

## LLM-friendly entry points

If you're an LLM agent arriving at this repo for the first time, also read:

- `https://agentproc.dev/llms.txt` — short navigation README for LLMs
- `https://agentproc.dev/llms-full.txt` — full docs in one file
- `CONTRIBUTING.md` — for human contributors (overlaps with this file)
