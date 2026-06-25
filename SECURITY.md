# Security Policy

## Reporting a vulnerability

Email the maintainer at bbmyth@gmail.com with a description of the issue and, if possible, a minimal reproduction. Please do not open a public GitHub issue for security reports.

You should receive an initial response within 72 hours.

## Scope

The AgentProc protocol itself, the reference Python and Node SDKs in this repository, and the documentation site are all in scope.

Bridge implementations and third-party agents are **out of scope** — those are separate projects maintained elsewhere.

## Trust model

AgentProc assumes the bridge and the agent process are co-located on a trusted host. The protocol is **not** designed for use across a trust boundary:

- Environment variables are visible to any process the agent spawns.
- stdout is plaintext; the protocol does not authenticate the agent to the bridge.
- The `command` field in a profile is executed directly; do not load profiles from untrusted sources.

If you need to run an untrusted agent, sandbox the entire process (container, VM, or similar) rather than relying on the protocol.

## Disclosure

Once a fix is released, we will publish a GitHub Security Advisory crediting the reporter (unless they prefer to remain anonymous).
