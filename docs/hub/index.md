# Profile Hub

Ready-to-use AgentProc profiles for popular AI agent CLIs. Each profile wraps a CLI as an AgentProc-compliant agent that any conformant bridge can drive — no bridge-specific shortcuts, no magic.

The hub lives in the repository at [`hub/`](https://github.com/jeffkit/agentproc/tree/main/hub). This page is the entry point; individual profile READMEs open directly on GitHub.

## Available profiles

| Profile | CLI | Tested | Languages |
|---------|-----|--------|-----------|
| [claude-code](https://github.com/jeffkit/agentproc/tree/main/hub/claude-code) | `claude` (Anthropic) | official | Python · Node |
| [codex](https://github.com/jeffkit/agentproc/tree/main/hub/codex) | `codex` (OpenAI) | official | Python · Node |
| [codebuddy](https://github.com/jeffkit/agentproc/tree/main/hub/codebuddy) | `codebuddy` (Tencent) | official | Python · Node |
| [agy](https://github.com/jeffkit/agentproc/tree/main/hub/agy) | `agy` | community | Python · Node |
| [echo-agent](https://github.com/jeffkit/agentproc/tree/main/hub/echo-agent) | (no CLI) | official | Python · Node · Bash |

The `tested` badge means:

- **official** — verified by AgentProc maintainers against the CLI's documented behavior.
- **community** — submitted and reportedly working; not verified end-to-end by maintainers.
- **unverified** — submitted without verification.

## Quick start

1. Pick a profile from the table above and open its README on GitHub.
2. Copy `profile.yaml` and one bridge script (`bridge.py` or `bridge.js`) into your project.
3. Adjust `cwd:` and any auth env vars in the profile.
4. Point your messaging bridge at the profile YAML.

For example, to wire up `claude-code`:

```bash
cp hub/claude-code/profile.yaml     ./profile.yaml
cp hub/claude-code/bridge.py        ./bridge.py
# edit cwd: in profile.yaml to point at your project
```

Local sanity check (no messaging bridge needed):

```bash
AGENT_MESSAGE="hello" \
AGENT_SESSION_ID="" \
AGENT_STREAMING="1" \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
python3 bridge.py
```

Expected:

```
AGENT_PARTIAL:"Hi! How can I help?"
AGENT_SESSION:13c2f6ec-1f97-42c4-be9e-9475129e243c
```

## What's in a profile?

```
hub/<name>/
├── profile.yaml         # AgentProc P0 profile (uses command:, not type:)
├── bridge.py            # Python bridge script
├── bridge.js            # Node.js bridge script
└── README.md            # Setup, usage, caveats
```

The bridge script is what actually translates the wrapped CLI's output (NDJSON, plain text, whatever) into the AgentProc sentinel-prefixed stdout protocol. Both Python and Node versions are maintained at parity — pick whichever fits your stack.

## Design principles

- **Pure P0 only.** No `type:` shortcuts, no `routing:` blocks, no bridge-specific extensions. Any conformant bridge works.
- **One profile per directory.** If you want multiple variants (different models, different prompts), copy and rename the directory.
- **Bilingual bridges.** Python and Node, both maintained at parity. Bash only for `echo-agent` (a reference impl, not a real wrapper).
- **No secrets.** Profile YAMLs reference env vars (`${ANTHROPIC_API_KEY}`); they never embed credentials.

## Contributing a new profile

1. Create `hub/<cli-name>/` with `profile.yaml`, `bridge.py`, `bridge.js`, `README.md`.
2. Set `tested: unverified` in the profile metadata unless you've verified end-to-end.
3. Add an entry to the table in [`hub/README.md`](https://github.com/jeffkit/agentproc/blob/main/hub/README.md).
4. Open a PR. A maintainer will review, possibly test, and bump `tested` accordingly.

See [`CONTRIBUTING.md`](https://github.com/jeffkit/agentproc/blob/main/CONTRIBUTING.md) for repo-wide conventions.

## Profile schema

```yaml
name: <kebab-case-id>           # required, matches directory name
description: <one-line>
cli: <command-name>             # the executable this wraps
cli_install: |                  # how to install the CLI itself
  npm install -g ...
agentproc:                      # the actual AgentProc P0 profile
  command: python3 ./bridge.py  # or: node ./bridge.js
  cwd: ~/your-project
  timeout_secs: 600
  streaming: true
  env:
    API_KEY: "${API_KEY}"
tested: official | community | unverified
maintainer: <github-handle>
tags: [<category>, ...]
notes: |                        # optional caveats, gotchas
  ...
```

## Relationship to ilink-hub-bridge

AgentProc was extracted from [`ilink-hub-bridge`](https://github.com/jeffkit), a messaging-platform bridge with built-in `type:` handlers for `claude-code`, `cursor`, `codebuddy-code`, and others. During production use we realized the bridge↔agent contract was reusable independently — and AgentProc was born.

These hub profiles are **pure P0** re-implementations of what those `type:` handlers do internally. They exist so any bridge can speak to `claude` / `codex` / `codebuddy` / `agy` without shipping type handlers of its own. If your bridge already has `type:` shortcuts, you don't need these profiles.
