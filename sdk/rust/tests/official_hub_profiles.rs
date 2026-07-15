//! Verify every official hub profile that declares `executor:` resolves to a
//! known Rust executor. This is the cross-SDK parity guarantee: a profile
//! fetched from hub/ runs in-process on any host with agentproc-rs.

#![cfg(feature = "yaml")]

use std::path::PathBuf;

use agentproc::Profile;

fn hub_dir() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .and_then(|p| p.parent())
        .map(|root| root.join("hub"))
        .expect("could not locate repo root")
}

#[test]
fn every_hub_executor_profile_resolves_to_known_rust_executor() {
    let hub = hub_dir();
    // Profiles expected to declare a known executor (mirror executors.js registry).
    let expected = [
        "codex",
        "claude-code",
        "codebuddy",
        "cursor",
        "gemini-cli",
        "kimi-code",
        "opencode",
        "qwen-code",
        "agy",
        "aider",
        "deepseek",
        "pi",
    ];

    for name in expected {
        let path = hub.join(name).join("profile.yaml");
        assert!(path.exists(), "missing hub profile: {}", path.display());
        let profile = Profile::from_path(&path)
            .unwrap_or_else(|e| panic!("parse {}: {e}", path.display()));
        assert!(
            profile.executor_known(),
            "profile `{name}` declares executor `{:?}` but no Rust executor is registered for it",
            profile.executor
        );
    }
}

#[test]
fn echo_agent_and_recursive_have_no_executor() {
    // These two intentionally stay on the spawn path (no in-process executor).
    let hub = hub_dir();
    for name in ["echo-agent", "recursive"] {
        let path = hub.join(name).join("profile.yaml");
        let profile = Profile::from_path(&path).expect("parse");
        assert!(
            profile.executor.is_none(),
            "`{name}` should not declare executor (it has a bespoke run loop)"
        );
    }
}
