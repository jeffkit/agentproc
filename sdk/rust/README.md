# agentproc (Rust)

[![crates.io](https://img.shields.io/crates/v/agentproc.svg)](https://crates.io/crates/agentproc)
[![docs.rs](https://docs.rs/agentproc/badge.svg)](https://docs.rs/agentproc)

Rust SDK for the [AgentProc](https://agentproc.dev) protocol — connect any
agent CLI to a messaging platform via a process-based interface.

This is the Rust sibling of the [Python](../python) and [Node](../node) SDKs.
All three implement the same wire protocol (`0.4`) and the same in-process
executor mechanism. One profile can run three ways depending on what the host
has installed: Rust executor (in-process), Node executor (in-process), or a
Python bridge script (spawn) — all producing the same observable NDJSON.

## Install

```toml
[dependencies]
agentproc = "0.10"
```

The default features pull in the built-in executor registry and YAML profile
parsing:

```toml
[dependencies]
agentproc = { version = "0.10", default-features = false, features = ["yaml"] }
```

## Quick start

```rust,no_run
use agentproc::{run, Profile, RunOptions};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let profile = Profile::from_path("profile.yaml")?;
    let result = run(&profile, RunOptions::new("hello")).await?;
    println!("{}", result.reply);
    Ok(())
}
```

## In-process executors

Set `executor:` in the profile to skip the bridge subprocess and let the
runner spawn the target CLI directly:

```yaml
agentproc:
  executor: codex          # in-process on hosts with agentproc-rs
  command: python3         # fallback for hosts without the Rust executor
  args: ["{{PROFILE_DIR}}/bridge.py"]
  timeout_secs: 600
```

Registered executors: `codex`, `claude-code`. Register your own with
[`agentproc::executors::register_executor`](https://docs.rs/agentproc/latest/agentproc/executors/fn.register_executor.html).

## License

MIT
