# AgentProc Go SDK

The Go implementation of the AgentProc protocol (wire `0.4`), kept at
observable parity with the [Python](../python), [Node](../node), and
[Rust](../rust) SDKs — verified by the shared
[`spec/conformance/`](../../spec/conformance/) fixture suite, which this
package's tests drive directly (`cases.json`, `scenarios.json`,
`executors.json`, `sdk.json`, `diagnostics.json`, and the posture matrix).

```bash
go get github.com/jeffkit/agentproc/sdk/go
```

## Bridge side — run a profile

```go
package main

import (
	"context"
	"fmt"

	agentproc "github.com/jeffkit/agentproc/sdk/go"
)

func main() {
	result, err := agentproc.Run(context.Background(), map[string]any{
		"command": "python3",
		"args":    []string{"{{PROFILE_DIR}}/bridge.py"},
		"env":     map[string]string{"MY_API_KEY": "${MY_API_KEY}"},
		"cwd":     "/path/to/workspace",
	}, &agentproc.RunOptions{
		Message:   "hello",
		SessionID: "", // previous session id, "" = new session
		ProfileDir: "/path/to/profile-dir",
		OnPartial: func(text string, role *string) {
			fmt.Println("partial:", text)
		},
	})
	if err != nil {
		panic(err) // profile validation error
	}
	fmt.Println(result.Reply, result.SessionID, result.ExitCode)
}
```

The runner implements the full spawn-path contract: three-layer child env
(infra set → profile env with `${VAR}` expansion + `env_allowlist` → extras),
`{{SESSION_ID}}` / `{{PROFILE_DIR}}` placeholders, earliest-expiry time limit
(`timeout_secs` / `budget_secs` / `deadline`), SIGTERM → `kill_grace_secs` →
SIGKILL against the whole **process group**, the bounded post-exit drain
(a grandchild holding the pipe cannot hang the turn), exit-code
normalisation (`128 + signal`), error-event precedence, first-non-empty
session ids with wire validation, and the spawn-path permission channel.

Cooperative cancel: cancel the `context.Context` — the runner performs the
graded kill and returns `ExitCancelled` (125) with `TimedOut=false`.

## Agent side — write an agent

```go
package main

import (
	"os"

	agentproc "github.com/jeffkit/agentproc/sdk/go"
)

func main() {
	handler := func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
		if err := ctx.SendPartial("thinking…", "thinking"); err != nil {
			return nil, err
		}
		return &agentproc.AgentResult{Response: "answer", SessionID: "s-1"}, nil
	}
	os.Exit(agentproc.Serve(handler))
}
```

`Serve` reads the turn object from stdin, calls the handler once per turn,
and emits the NDJSON events. Return `&agentproc.ProtocolError{...}` for a
fatal, user-visible error (emitted as an `error` event + exit 1); use
`ctx.SendError` for a non-terminal error (the handler may still return a
body, which the bridge discards per the spec). `ctx.SendPermissionRequest` /
`ctx.ReadPermissionResponse` drive the optional permission channel.

## In-process executors

Like the other SDKs, the runner can execute a profile through a named
in-process executor — spawning the target CLI directly, no bridge
subprocess. The built-in registry mirrors the Python/Node one (claude-code
with the stdio permission channel, codex, cursor, gemini-cli, qwen-code,
codebuddy, opencode, kimi-code, grok-build, agy, aider, deepseek, dsh, pi),
and applications can register their own:

```go
func init() {
	agentproc.RegisterExecutor("my-cli", func() agentproc.Executor {
		return &MyCLIExecutor{}
	})
}
```

Stateful executors implement `TurnFactory` (`MakeTurn()` per turn); the
stdin prompt channel is the `InitialStdinBuilder` interface; plain-text CLIs
declare `Plain() = true` and surface a minted session id via
`SessionIDGetter`. The runner refuses to spawn (error + non-zero exit, never
a silent `--dangerously-skip-permissions`) when `permission: true` meets an
executor without `SupportsPermission()`, or when `AGENTPROC_AUTO_APPROVE` is
off and the argv carries an auto-approve flag.

## Workspace run lock

For hosts that can be hard-killed (SIGKILL / OOM) — worker runtimes, long-
lived bridges — set `RunOptions.RunLockKey` (usually the workspace absolute
path). After spawn the runner records pid + argv + the host's own pid under
`~/.agentproc/run-locks/` (the "last will"); on normal completion the lock is
cleared after the group kill. A takeover calls `CleanupStaleRun(key, grace)`
**before** starting a new agent on the same workspace: dead pid → stale,
release; live pid with verified identity (group leader + matching command)
and a dead spawner → orphan, the whole group is killed, then release; a live
spawner → `RunLockBusy` (never kill a healthy agent whose host is still
running — the issue #9 rule); unverifiable → `RunLockBusy`, human decision.

## CLI

`go install github.com/jeffkit/agentproc/sdk/go/cmd/agentproc@latest` builds
the `agentproc` binary (port of the Python/Node CLI):

```bash
agentproc --profile ./hub/echo-agent/profile.yaml --prompt "hi" [--cwd dir] [--raw] [--no-stream]
agentproc hub list
agentproc hub run claude-code -p "explain this" --env ANTHROPIC_API_KEY=$KEY
agentproc hub show codex
agentproc hub install agy
```

Output semantics match the reference CLIs: protocol events on stderr (NDJSON),
the reply body on stdout, `agentproc:session:<id>` / `agentproc:error:<msg>`
trailers, exit 0 / 1 / 2 (usage) / 130 (interrupted — the agent group is
killed first). Interactive permission profiles prompt y/N on a TTY and deny
without one.

## Scope notes

- **Hub divergence:** the Go CLI has no bundled profile copy (a Go module
  cannot embed files outside its subtree) — `hub` commands resolve via the
  jsDelivr CDN and cache at `~/.agentproc/cache/hub/` (24h TTL), so first
  use needs network. `AGENTPROC_HUB_REF` pins an exact upstream version and
  invalidates the cache per ref.
- Modules: `gopkg.in/yaml.v3` (profile parsing) is the only dependency.

## Tests

```bash
go test ./...   # includes the shared conformance fixtures
```
