# AgentProc Profile Hub

Ready-to-use [AgentProc](https://agentproc.dev) profiles for popular AI agent CLIs. Each profile wraps a CLI as an AgentProc-compliant agent that any conformant bridge can drive — no bridge-specific shortcuts, no magic.

## Available profiles

| Profile | CLI | Tested | Languages |
|---------|-----|--------|-----------|
| [claude-code](./claude-code/) | `claude` (Anthropic) | official | Python · Node |
| [codex](./codex/) | `codex` (OpenAI) | official | Python · Node |
| [agy](./agy/) | `agy` | community | Python · Node |
| [codebuddy](./codebuddy/) | `codebuddy` (Tencent) | official | Python · Node |
| [echo-agent](./echo-agent/) | (no CLI — hello world) | official | Python · Node · Bash |

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

## How to use a profile

1. **Install the target CLI** (see the profile's README — each links to its CLI's installation docs).
2. **Copy** `profile.yaml` and one bridge script into your working directory.
3. **Adjust** `cwd:` in the profile to point at your project.
4. **Point your bridge** at the profile YAML. The exact command depends on your bridge.

Example with the Python SDK's bare runner:

```bash
cd hub/claude-code
AGENT_MESSAGE="hello" AGENT_STREAMING="1" python3 bridge.py
```

You should see AgentProc protocol output on stdout:

```
AGENT_PARTIAL:"Hi! How can I help?"
AGENT_SESSION:cli-sess-9f3a2c1e-4b8d-4a2f-b6c1-2e8d4f5a7b9c
```

## Design principles

- **Pure P0 only.** No `type:` shortcuts, no `routing:` blocks, no bridge-specific extensions. Any conformant bridge works.
- **One profile per directory.** If you want multiple variants (e.g., different models), copy and rename the directory.
- **Bilingual bridges.** Python and Node, both maintained at parity. Bash only for `echo-agent` (it's a reference impl, not a real wrapper).
- **No secrets.** Profile YAMLs reference env vars (`${ANTHROPIC_API_KEY}`); they never embed credentials.

## Contributing

Add a new profile:

1. Create `hub/<cli-name>/` with `profile.yaml`, `bridge.py`, `bridge.js`, `README.md`.
2. Set `tested: unverified` in the profile metadata unless you've verified end-to-end.
3. Add an entry to the table above.
4. Open a PR. A maintainer will review, possibly test, and bump `tested` accordingly.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for repo-wide conventions.

## Profile schema

```yaml
name: <kebab-case-id>           # required, matches directory name
description: <one-line>
cli: <command-name>             # the executable this wraps
cli_install: |                  # how to install the CLI itself
  npm install -g ...
agentproc:                      # the actual AgentProc P0 profile
  command: python3 {{PROFILE_DIR}}/bridge.py  # or: node {{PROFILE_DIR}}/bridge.js
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
