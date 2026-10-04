//! session_id sanitisation: ids containing path separators / control
//! characters / dot-segments are rejected instead of becoming file names.

#![cfg(feature = "yaml")]

use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use agentproc::{history, run, Profile, RunOptions};

fn write_agent(dir: &std::path::Path, sid: &str) -> PathBuf {
    let script = format!(
        r#"#!/bin/bash
read -r turn
python3 -c 'import json,sys; print(json.dumps({{"type": "result", "text": "ok", "session_id": sys.argv[1]}}))' '{}'
"#,
        sid
    );
    let p = dir.join("agent.sh");
    fs::write(&p, script).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).unwrap();
    }
    p
}

async fn run_agent(sid: &str, sessions_dir: &std::path::Path) -> agentproc::RunResult {
    let tmp = tempfile::tempdir().unwrap();
    let agent = write_agent(tmp.path(), sid);
    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 15\nstreaming: false\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();
    let mut env = std::collections::HashMap::new();
    env.insert(
        "AGENTPROC_SESSION_DIR".to_string(),
        sessions_dir.display().to_string(),
    );
    let opts = RunOptions::new("hi")
        .with_cwd(sessions_dir)
        .with_env(env);
    run(&profile, opts).await.unwrap()
}

trait WithEnv {
    fn with_env(self, env: std::collections::HashMap<String, String>) -> RunOptions;
}

impl WithEnv for RunOptions {
    fn with_env(mut self, env: std::collections::HashMap<String, String>) -> RunOptions {
        self.extra_env = env;
        self
    }
}

#[tokio::test]
async fn invalid_session_ids_are_dropped() {
    let sessions = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    for bad in ["../evil", "a/b", "a\\b", "a\u{1}b", ".", ".."] {
        let r = run_agent(bad, sessions.path()).await;
        assert!(r.ok(), "[{bad:?}] error: {}", r.error);
        assert!(
            r.session_id.is_empty(),
            "[{bad:?}] invalid id adopted: {}",
            r.session_id
        );
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !outside.path().join("evil.jsonl").exists(),
        "escapee file outside sessions dir"
    );
    let stray: Vec<_> = std::fs::read_dir(sessions.path())
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.is_file())
        .collect();
    assert!(stray.is_empty(), "files written into sessions dir: {stray:?}");
}

#[tokio::test]
async fn valid_session_id_adopted() {
    let sessions = tempfile::tempdir().unwrap();
    let r = run_agent("org:proj:thread-42", sessions.path()).await;
    assert!(r.ok(), "error: {}", r.error);
    assert_eq!(r.session_id, "org:proj:thread-42");
}

#[test]
fn session_file_path_rejects_invalid_ids() {
    let dir = std::path::Path::new("/tmp/some-sessions");
    let p = history::session_file_path("../evil", Some(dir));
    assert!(
        !p.to_string_lossy().contains(".."),
        "path traversal in {:?}",
        p
    );
    let p = history::session_file_path("a/b", Some(dir));
    assert!(!p.to_string_lossy().contains("/a/"), "separator in {:?}", p);
}

#[test]
fn append_history_rejects_invalid_ids() {
    let tmp = tempfile::tempdir().unwrap();
    let entries = vec![history::HistoryEntry {
        role: "user".into(),
        content: "hi".into(),
        usage: None,
    }];
    for bad in ["../evil", "a/b", ".", "..", "a\u{1}b"] {
        let res = history::append_history(bad, &entries, Some(tmp.path()));
        assert!(res.is_err(), "[{bad:?}] append unexpectedly succeeded");
    }
    assert!(!tmp.path().join("evil.jsonl").exists());
    assert!(!tmp.path().join(".jsonl").exists());
}

#[allow(dead_code)]
fn _unused(_: PathBuf) {}
