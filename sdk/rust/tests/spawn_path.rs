//! End-to-end test of the spawn path: load a profile that points at the
//! `sdk_harness` example, run one turn, assert the NDJSON events arrive as
//! expected.

#![cfg(all(feature = "yaml", feature = "executors"))]

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use agentproc::{run, Profile, RunOptions};

fn harness_path() -> PathBuf {
    // examples are built under target/<profile>/examples/. When tests run
    // the profile is usually "debug". We probe debug first, release second.
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target");
    let debug = base.join("debug").join("examples").join("sdk_harness");
    if debug.exists() {
        return debug;
    }
    base.join("release").join("examples").join("sdk_harness")
}

#[tokio::test]
async fn spawn_path_runs_sdk_harness_hello() {
    let harness = harness_path();
    let yaml = format!(
        "command: {}\nargs: [\"--kind\", \"hello\"]\ntimeout_secs: 10\nstreaming: true\n",
        harness.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let partials = Arc::new(Mutex::new(Vec::<String>::new()));
    let partials_clone = partials.clone();
    let opts = RunOptions::new("hi")
        .on_partial(move |text, _| partials_clone.lock().unwrap().push(text))
        .on_session(|_sid| {});

    let result = run(&profile, opts).await.unwrap();
    assert!(result.ok(), "error: {}", result.error);
    assert_eq!(result.exit_code, 0);
    assert_eq!(result.session_id, "harness-1");
    // Streaming forwarded both partials; reply accumulates them.
    let got = result.reply;
    assert!(got.contains("Hi "), "reply was: {got}");
    assert!(got.contains("there!"), "reply was: {got}");
    let partials = partials.lock().unwrap();
    assert_eq!(partials.len(), 2, "expected 2 partials, got {partials:?}");
}

#[tokio::test]
async fn spawn_path_runs_sdk_harness_error() {
    let harness = harness_path();
    let yaml = format!(
        "command: {}\nargs: [\"--kind\", \"error\"]\ntimeout_secs: 10\n",
        harness.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let result = run(&profile, RunOptions::new("boom")).await.unwrap();
    assert!(!result.error.is_empty(), "expected an error event");
    assert!(result.error.contains("boom"), "error: {}", result.error);
}

#[tokio::test]
async fn unknown_executor_without_command_hard_fails() {
    let yaml = "agentproc:\n  executor: does-not-exist\n";
    let profile = Profile::from_yaml(&yaml).unwrap();
    let result = run(&profile, RunOptions::new("hi")).await;
    assert!(result.is_err(), "expected hard failure for unknown executor + no command");
    let err = result.unwrap_err().to_string();
    assert!(err.contains("does-not-exist"), "error should name the executor: {err}");
}

#[tokio::test]
async fn known_executor_resolves_to_in_process_path() {
    // We can't run a real codex/claude here, but we can assert the profile
    // recognises a known executor name at the config level.
    let yaml = "agentproc:\n  executor: codex\n  command: echo\n";
    let profile = Profile::from_yaml(&yaml).unwrap();
    assert!(profile.executor_known(), "codex should be a known executor");
    assert_eq!(profile.executor.as_deref(), Some("codex"));
}
