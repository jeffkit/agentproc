# AgentProc Profile Hub

Ready-to-use [AgentProc](https://agentproc.dev) profiles for popular AI agent CLIs. Each profile wraps a CLI as an AgentProc-compliant agent that any conformant bridge can drive — no bridge-specific shortcuts, no magic.

See [PERMISSIONS.md](./PERMISSIONS.md) for which CLIs support mid-turn tool authorization (`permission: true`) vs auto-approve / yolo.

## Available profiles

| Profile | CLI | Tested | Languages |
|---------|-----|--------|-----------|
| [claude-code](./claude-code/) | `claude` (Anthropic) | official | Python · Node |
| [codex](./codex/) | `codex` (OpenAI) | official | Python · Node |
| [codebuddy](./codebuddy/) | `codebuddy` (Tencent) | official | Python · Node |
| [gemini-cli](./gemini-cli/) | `gemini` (Google) | official | Python · Node |
| [cursor](./cursor/) | `agent` (Cursor Agent) | official | Python · Node |
| [qwen-code](./qwen-code/) | `qwen` (Alibaba) | community | Python · Node |
| [recursive](./recursive/) | `recursive` (self-improving Rust agent) | community | Python · Node |
| [agy](./agy/) | `agy` | community | Python · Node |
| [pi](./pi/) | `pi` (earendil-works) | community | Python · Node |
| [opencode](./opencode/) | `opencode` | community | Python · Node |
| [aider](./aider/) | `aider` | community | Python · Node |
| [kimi-code](./kimi-code/) | `kimi` (Moonshot AI) | community | Python · Node |
| [deepseek](./deepseek/) | `deepseek` (DeepSeek TUI) | community | Python · Node |
| [echo-agent](./echo-agent/) | (no CLI — hello world) | official | Python · Node · Bash |

## Coverage vs ACP Registry

The [ACP Registry](https://agentclientprotocol.com/get-started/registry) maintains a list of agents implementing the Agent Client Protocol — a neighbor protocol with significant overlap in supported CLIs. The matrix below tracks AgentProc Hub coverage of that list.

| Agent | ACP listed | Hub profile | Status |
|-------|------------|-------------|--------|
| Claude Agent | ✅ | [claude-code](./claude-code/) | ✅ official |
| Codex | ✅ | [codex](./codex/) | ✅ official |
| Gemini CLI | ✅ | [gemini-cli](./gemini-cli/) | ✅ official |
| Cursor | ✅ | [cursor](./cursor/) | ✅ official |
| Codebuddy Code | ✅ | [codebuddy](./codebuddy/) | ✅ official |
| Qwen Code | ✅ | [qwen-code](./qwen-code/) | 🟡 community (schema unverified) |
| Goose | ✅ | — | ❌ (Goose uses cargo/brew install, not npm — PR welcome) |
| GitHub Copilot | ✅ | — | ❌ (`--output-format json` JSONL schema undocumented — PR welcome) |
| Junie (JetBrains) | ✅ | — | ❌ (TUI-only; `--acp` mode integrates via ACP, not stdout — out of scope) |
| Cline | ✅ | — | ❌ (Cline is a VS Code extension, not a standalone CLI) |
| GLM Agent | ✅ | — | ❌ (community ACP wrapper, no upstream CLI to wrap) |

Status legend: ✅ verified end-to-end · 🟡 reportedly works, schema not independently verified · ❌ not yet covered.

`tested` is one of:
- **official** — verified by the AgentProc maintainers against the CLI's documented behavior.
- **community** — submitted and reportedly working, but not verified end-to-end by maintainers.
- **unverified** — submitted without verification; treated as a starting point.

## What's in each profile?

Every profile directory contains the same set of files:

```
hub/<name>/
├── profile.yaml         # AgentProc P0 profile (uses command:, not type:)
├── bridge.py            # Python bridge script that wraps the CLI
├── bridge.js            # Node.js bridge script
└── README.md            # Setup, usage, environment variables, caveats
```

Pick whichever bridge language you prefer — both produce identical AgentProc output.

### Shared bridge utilities

NDJSON-based profiles (`claude-code`, `codex`, `codebuddy`, `gemini-cli`, `qwen-code`, `cursor`, `opencode`, `kimi-code`) share subprocess + line-reading + emission logic via [`_shared/stream_utils`](./_shared/). Each bridge stays ~30 lines, supplying only `build_args()` and `parse_event()`. Plain-text one-shot profiles (`aider`, `pi`, `deepseek`, `agy`, `echo-agent`) use the shared `run_plain_cli` / `runPlainCli` helper; `recursive` (which needs cross-turn transcript state the shared helper doesn't model) keeps a bespoke bridge.

## How to use a profile

1. **Install the target CLI** (see the profile's README — each links to its CLI's installation docs).
2. **Copy** `profile.yaml` and one bridge script into your working directory.
3. **Adjust** `cwd:` in the profile to point at your project.
4. **Point your bridge** at the profile YAML. The exact command depends on your bridge.

Example with the Python SDK's bare runner (wire 0.4 — the turn object arrives on stdin as one NDJSON line):

```bash
cd hub/claude-code
echo '{"type":"turn","message":"hello","session_id":"","from_user":"u1","protocol_version":"0.4"}' | python3 bridge.py
```

You should see AgentProc protocol output on stdout (one NDJSON event per line):

```
{"type":"partial","text":"Hi! How can I help?","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
{"type":"result","text":"","session_id":"cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c"}
```

## Design principles

- **Pure P0 only.** No `type:` shortcuts, no `routing:` blocks, no bridge-specific extensions. Any conformant bridge works.
- **One profile per directory.** If you want multiple variants (e.g., different models), copy and rename the directory.
- **Bilingual bridges.** Python and Node, both maintained at parity. Bash only for `echo-agent` (it's a reference impl, not a real wrapper).
- **No secrets.** Profile YAMLs reference env vars (`${ANTHROPIC_API_KEY}`); they never embed credentials.

## Contributing

Add a new profile:

1. Create `hub/<cli-name>/` with `profile.yaml`, `bridge.py`, `bridge.js`, `README.md`.
2. **If the CLI emits NDJSON** (one JSON object per line on stdout, like claude/codex/gemini), reuse [`_shared/stream_utils`](./_shared/). Your bridge only needs `build_args()` and `parse_event()` — see `gemini-cli/bridge.py` for a minimal example.
3. **If the CLI emits plain text**, write a bespoke bridge (`subprocess.run` + emit). See `agy/bridge.py`.
4. Set `tested: unverified` in the profile metadata unless you've verified end-to-end.
5. Add an entry to the table above and (if the agent is on the ACP Registry) the coverage matrix.
6. Open a PR. A maintainer will review, possibly test, and bump `tested` accordingly.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for repo-wide conventions.

## Profile schema

```yaml
name: <kebab-case-id>           # required, matches directory name
description: <one-line>
cli: <command-name>             # the executable this wraps
cli_install: |                  # how to install the CLI itself
  npm install -g ...
agentproc:                      # the actual AgentProc P0 profile (wire 0.4)
  command: python3              # argv[0] — always a single token, never split
  args: ["{{PROFILE_DIR}}/bridge.py"]  # argv[1..]; or: ["{{PROFILE_DIR}}/bridge.js"] for node
  # cwd intentionally omitted: `hub run` defaults it to the user's
  # current directory. Bridge script is located via {{PROFILE_DIR}}.
  timeout_secs: 600
  streaming: true
  env:
    API_KEY: "${API_KEY}"       # reference existing env vars
tested: official|community|unverified
maintainer: <github-handle>
tags: [<category>, ...]
notes: |                        # optional caveats, gotchas
  ...
```

## Relationship to ilink-hub-bridge

AgentProc was extracted from [`ilink-hub-bridge`](https://github.com/jeffkit), a messaging-platform bridge with built-in `type:` handlers for `claude-code`, `cursor`, `codebuddy-code`, and others. During production use we realized the bridge↔agent contract was reusable independently — and AgentProc was born.

These hub profiles are **pure P0** re-implementations of what those `type:` handlers do internally. They exist so any bridge can speak to `claude` / `codex` / `codebuddy` / `agy` without shipping type handlers of its own. If your bridge already has `type:` shortcuts, you don't need these profiles.

## License

MIT. The hub profiles are configuration; the bridge scripts are MIT-licensed. The wrapped CLIs themselves retain their own licenses — this project does not redistribute them.
