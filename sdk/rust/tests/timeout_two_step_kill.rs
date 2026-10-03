//! Two-step timeout kill: SIGTERM first, consume kill_grace_secs, then
//! SIGKILL. Unix-only (Windows degrades to a direct kill per spec).

#![cfg(all(unix, feature = "yaml"))]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::sync::{Arc, Mutex};

use agentproc::{run, Profile, RunOptions};

fn write_agent(dir: &std::path::Path) -> std::path::PathBuf {
    let script = r#"#!/bin/bash
trap 'printf "%s\n" "{\"type\":\"partial\",\"text\":\"sigterm-handled\"}"; exit 0' TERM
sleep 30 &
wait
"#;
    let p = dir.join("agent.sh");
    fs::write(&p, script).unwrap();
    fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).unwrap();
    p
}

#[tokio::test]
async fn timeout_sigterm_flushes_partial_before_kill() {
    let tmp = tempfile::tempdir().unwrap();
    let agent = write_agent(tmp.path());
    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 1\nkill_grace_secs: 5\nstreaming: true\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let partials = Arc::new(Mutex::new(Vec::<String>::new()));
    let p = partials.clone();
    let opts = RunOptions::new("hi").on_partial(move |text, _| {
        p.lock().unwrap().push(text);
    });

    let result = run(&profile, opts).await.unwrap();
    assert!(result.timed_out);
    assert_eq!(result.exit_code, 124);
    assert!(
        result.reply.contains("sigterm-handled"),
        "grace SIGTERM partial not forwarded; reply was: {}",
        result.reply
    );
}

#[tokio::test]
async fn timeout_zero_grace_still_terminates() {
    let tmp = tempfile::tempdir().unwrap();
    let agent = write_agent(tmp.path());
    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 1\nkill_grace_secs: 0\nstreaming: true\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let result = run(&profile, RunOptions::new("hi")).await.unwrap();
    assert!(result.timed_out);
    assert_eq!(result.exit_code, 124);
}
