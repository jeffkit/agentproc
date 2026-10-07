//! Bounded stdio drain: an agent that exits while a surviving grandchild still
//! holds the inherited stdout pipe must not keep the turn pending.
//! Unix-only (`sleep &` + process-group semantics).

#![cfg(all(unix, feature = "yaml"))]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::time::{Duration, Instant};

use agentproc::{run, Profile, RunOptions};

/// Generous ceiling: the fix returns in ~1 s (one drain grace), the bug hangs
/// the full `timeout_secs: 30`.
const HANG_CEILING: Duration = Duration::from_secs(10);

fn write_agent(dir: &Path) -> std::path::PathBuf {
    let script = r#"#!/bin/bash
sleep 30 &
printf '%s\n' '{"type":"result","text":"done"}'
exit 0
"#;
    let p = dir.join("agent.sh");
    fs::write(&p, script).unwrap();
    fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).unwrap();
    p
}

#[tokio::test]
async fn spawn_path_does_not_hang_on_surviving_grandchild() {
    let tmp = tempfile::tempdir().unwrap();
    let agent = write_agent(tmp.path());
    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 30\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let started = Instant::now();
    let result = run(&profile, RunOptions::new("hi")).await.unwrap();
    let elapsed = started.elapsed();
    let _ = std::process::Command::new("pkill")
        .args(["-f", "sleep 30"])
        .status();

    assert!(
        elapsed < HANG_CEILING,
        "run took {elapsed:?}: the stdout drain was not bounded"
    );
    assert!(!result.timed_out, "turn should not have hit the 30 s timeout");
    assert_eq!(result.exit_code, 0);
    assert_eq!(result.reply, "done");
}
