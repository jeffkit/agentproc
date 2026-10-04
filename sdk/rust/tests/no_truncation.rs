//! No default reply truncation: a long result body arrives intact.

#![cfg(feature = "yaml")]

use std::fs;
use std::path::PathBuf;

use agentproc::{run, Profile, RunOptions};

fn write_agent(dir: &std::path::Path) -> PathBuf {
    let script = r#"#!/bin/bash
read -r turn
python3 - <<'PY'
import json
print(json.dumps({"type": "result", "text": "x" * 10000, "session_id": "s-long-1"}))
PY
"#;
    let p = dir.join("agent.sh");
    fs::write(&p, script).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).unwrap();
    }
    p
}

#[tokio::test]
async fn long_result_not_truncated() {
    let tmp = tempfile::tempdir().unwrap();
    let agent = write_agent(tmp.path());
    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 15\nstreaming: false\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let result = run(&profile, RunOptions::new("hi")).await.unwrap();
    assert!(result.ok(), "error: {}", result.error);
    assert_eq!(result.reply.chars().count(), 10000);
    assert!(!result.reply.contains("(truncated)"));
}
