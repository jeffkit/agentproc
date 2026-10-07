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

// issue #10 — spec: 130 = SIGINT, 143 = SIGTERM. A bash `kill -TERM $$`
// child dies by signal; s.code() is None and must not unwrap_or(0) to success.
#[cfg(unix)]
#[tokio::test]
async fn killed_by_sigterm_normalised_to_143() {
    let yaml = "command: /bin/bash\nargs: [\"-c\", \"kill -TERM $$\"]\ntimeout_secs: 10\n".to_string();
    let profile = Profile::from_yaml(&yaml).unwrap();
    let result = run(&profile, RunOptions::new("hi")).await.unwrap();
    assert_eq!(result.exit_code, 143, "exit_code: {}", result.exit_code);
}

#[cfg(unix)]
#[tokio::test]
async fn killed_by_sigint_normalised_to_130() {
    let yaml = "command: /bin/bash\nargs: [\"-c\", \"kill -INT $$\"]\ntimeout_secs: 10\n".to_string();
    let profile = Profile::from_yaml(&yaml).unwrap();
    let result = run(&profile, RunOptions::new("hi")).await.unwrap();
    assert_eq!(result.exit_code, 130, "exit_code: {}", result.exit_code);
}

// issue #19 — on_partial's second argument is the partial's `role` (raw string,
// `None` when absent), not the session id.
#[cfg(unix)]
#[tokio::test]
async fn on_partial_second_arg_is_the_partial_role() {
    use std::fs;

    let tmp = tempfile::tempdir().unwrap();
    let agent = tmp.path().join("agent.sh");
    fs::write(
        &agent,
        "#!/bin/bash\n\
read -r turn\n\
printf '%s\\n' '{\"type\":\"partial\",\"text\":\"thinking chunk\",\"role\":\"thinking\"}'\n\
printf '%s\\n' '{\"type\":\"partial\",\"text\":\"plain chunk\"}'\n\
printf '%s\\n' '{\"type\":\"partial\",\"text\":\"plan chunk\",\"role\":\"plan\"}'\n\
printf '%s\\n' '{\"type\":\"result\",\"text\":\"\"}'\n",
    )
    .unwrap();
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&agent, fs::Permissions::from_mode(0o755)).unwrap();
    }

    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 15\nstreaming: true\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let seen: Arc<Mutex<Vec<(String, Option<String>)>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = seen.clone();
    let opts = RunOptions::new("hi").on_partial(move |text, role| {
        sink.lock().unwrap().push((text, role));
    });

    let result = run(&profile, opts).await.unwrap();
    assert!(result.ok(), "error: {}", result.error);

    let seen = seen.lock().unwrap().clone();
    assert_eq!(
        seen,
        vec![
            ("thinking chunk".to_string(), Some("thinking".to_string())),
            ("plain chunk".to_string(), None),
            // Unknown roles are forwarded as-is (no lossy enum).
            ("plan chunk".to_string(), Some("plan".to_string())),
        ],
        "on_partial's second argument must be the partial role"
    );
}
