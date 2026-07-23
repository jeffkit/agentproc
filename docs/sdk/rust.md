# Rust SDK

The Rust SDK is the third official AgentProc SDK, kept at observable parity with the [Python](./python) and [Node](./node) SDKs. All three implement the same wire protocol (`0.4`) and the same in-process executor mechanism.

Unlike the Python and Node SDKs — which ship a `create_profile` handler API for writing agent scripts — the Rust SDK is **host/bridge-oriented**: it provides a `run()` runner for driving profiles programmatically from a Rust application (e.g. an IM bridge, a CI bot). It also ships the in-process executor registry so a Rust host can spawn target CLIs directly without a bridge subprocess.

[![crates.io](https://img.shields.io/crates/v/agentproc.svg)](https://crates.io/crates/agentproc)
[![docs.rs](https://docs.rs/agentproc/badge.svg)](https://docs.rs/agentproc)

## Install

```toml
[dependencies]
agentproc = "0.11"
```

The default features pull in the built-in executor registry, YAML profile parsing, and multimodal attachment download:

```toml
[dependencies]
# Minimal runner only — no executors, no YAML, no multimodal fetch
agentproc = { version = "0.11", default-features = false }
```

Available features (all on by default):

| Feature | What it pulls in |
|---------|------------------|
| `executors` | Built-in executor registry (`codex`, `claude-code`, `gemini-cli`, …) |
| `yaml` | `Profile::from_path` / serde_yaml parsing — drop to build `&Profile` programmatically |
| `multimodal` | Image/PDF base64 download for executors that feed content blocks to their CLI |

## Quick start — drive a profile

```rust
use agentproc::{run, Profile, RunOptions};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let profile = Profile::from_path("profile.yaml")?;
    let result = run(&profile, RunOptions::new("what is this codebase?")).await?;
    println!("{}", result.reply);
    println!("session: {}", result.session_id);
    Ok(())
}
```

## Streaming

Pass an `on_partial` callback to forward chunks in real time when the profile's `streaming: true`:

```rust
use agentproc::{run, Profile, RunOptions};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let profile = Profile::from_path("profile.yaml")?;
    let opts = RunOptions::new("explain this codebase")
        .on_partial(|chunk| {
            print!("{chunk}");
            std::io::Result::<()>::Ok(())
        });
    let result = run(&profile, opts).await?;
    println!("\nsession: {}", result.session_id);
    Ok(())
}
```

## Multi-turn sessions

Feed the previous `result.session_id` back into the next turn:

```rust
use agentproc::{run, Profile, RunOptions};

async fn turn(profile: &Profile, message: &str, session_id: &str) -> anyhow::Result<String> {
    let opts = RunOptions::new(message).session_id(session_id);
    let result = run(profile, opts).await?;
    Ok(result.session_id)
}
```

The runner persists the first non-empty `session_id` and surfaces it on `RunResult`; pass it back for continuity.

## In-process executors

Set `executor:` in the profile to skip the bridge subprocess and let the runner spawn the target CLI directly:

```yaml
agentproc:
  executor: codex          # in-process on hosts with the Rust SDK
  command: python3         # fallback for hosts without the Rust executor
  args: ["{{PROFILE_DIR}}/bridge.py"]
  timeout_secs: 600
```

Built-in executor names (with the `executors` feature):

```rust
use agentproc::executors::executor_names;

println!("{:?}", executor_names());
// ["claude-code", "codebuddy", "codex", "cursor", "gemini-cli", "grok-build", ...]
```

Register your own executor at runtime via [`agentproc::executors::register_executor`](https://docs.rs/agentproc/latest/agentproc/executors/fn.register_executor.html).

## `RunResult` fields

| Field | Type | Description |
|-------|------|-------------|
| `reply` | `String` | Assembled reply body (empty when streaming forwarded all chunks) |
| `session_id` | `String` | First valid session id from any event; `""` if none |
| `error` | `String` | Error message from a `{"type":"error"}` event; `""` if none |
| `exit_code` | `i32` | Agent process exit code (124 = timeout) |
| `timed_out` | `bool` | Whether the run was killed by timeout |
| `usage` | `Option<Usage>` | Token/cost stats from the terminal event; `None` when absent |
| `duration_ms` | `u64` | Wall-clock duration of the run |

Common `usage` keys (all optional): `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `reasoning_tokens`, `duration_ms`, `cost_usd`.

## Permission channel

For CLIs that support mid-turn tool authorization (e.g. Claude Code), set `permission: true` on the profile and supply an `on_permission` callback. The runner awaits your `PermissionDecision` — `Allow` (optionally with `updated_input`) or `Deny` (with a reason) — before forwarding it to the agent:

```rust
use agentproc::{run, Profile, RunOptions, PermissionDecision};

async fn decide(req: PermissionRequest) -> PermissionDecision {
    // prompt a human, log, or auto-approve by policy
    PermissionDecision::allow()
}
```

## Local testing

Drive a profile through the same path the hub uses:

```bash
agentproc --profile ./myagent.yaml --prompt "hello"
```

Or write the turn object to stdin the way the bridge does:

```bash
echo '{"type":"turn","message":"hello","session_id":"","protocol_version":"0.4"}' | cargo run --example run_profile
```

## API reference

Full API docs are published to [docs.rs/agentproc](https://docs.rs/agentproc). The canonical runner implementation lives in [`sdk/rust/src/runner.rs`](https://github.com/jeffkit/agentproc/blob/main/sdk/rust/src/runner.rs).
