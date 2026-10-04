//! permission: true on the spawn path — permission_request events drive the
//! stdin response channel (allow via on_permission, deny when unwired).

#![cfg(feature = "yaml")]

use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use agentproc::{run, Profile, RunOptions};

fn write_agent(dir: &std::path::Path) -> PathBuf {
    let script = r#"#!/bin/bash
read -r turn
printf '%s\n' '{"type":"permission_request","request_id":"req-1","tool_name":"Bash","input":{"command":"ls"},"session_id":"s-perm-1"}'
read -r resp
printf '%s\n' "GOT:$resp" >&2
printf '%s\n' '{"type":"result","text":"done","session_id":"s-perm-1"}'
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
async fn permission_request_with_callback_writes_allow_response() {
    let tmp = tempfile::tempdir().unwrap();
    let agent = write_agent(tmp.path());
    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 10\npermission: true\nstreaming: false\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let decisions = Arc::new(Mutex::new(Vec::<(String, String)>::new()));
    let d = decisions.clone();
    let mut opts = RunOptions::new("hi");
    opts.on_permission = Some(Arc::new(move |req| {
        let req = req.clone();
        let d = d.clone();
        Box::pin(async move {
            d.lock().unwrap().push((req.request_id.clone(), req.tool_name.clone()));
            agentproc::PermissionDecision::allow()
        })
    }));

    let result = run(&profile, opts).await.unwrap();
    assert!(result.ok(), "error: {}", result.error);
    assert!(!result.timed_out);
    assert_eq!(result.exit_code, 0);
    assert_eq!(result.reply, "done");
    assert_eq!(result.session_id, "s-perm-1");
    let d = decisions.lock().unwrap();
    assert_eq!(d.len(), 1);
    assert_eq!(d[0].0, "req-1");
    assert_eq!(d[0].1, "Bash");
}

#[tokio::test]
async fn permission_request_without_callback_writes_deny() {
    let tmp = tempfile::tempdir().unwrap();
    let agent = write_agent(tmp.path());
    let yaml = format!(
        "command: /bin/bash\nargs: [\"{}\"]\ntimeout_secs: 10\npermission: true\nstreaming: false\n",
        agent.display()
    );
    let profile = Profile::from_yaml(&yaml).unwrap();

    let stderr_lines = Arc::new(Mutex::new(Vec::<String>::new()));
    let s = stderr_lines.clone();
    let mut opts = RunOptions::new("hi");
    opts.on_stderr = Some(Arc::new(move |chunk| {
        s.lock().unwrap().push(chunk.to_string());
    }));

    let result = run(&profile, opts).await.unwrap();
    assert!(result.ok(), "error: {}", result.error);
    assert!(!result.timed_out);
    assert_eq!(result.reply, "done");

    let joined = stderr_lines.lock().unwrap().join("");
    let got = joined
        .lines()
        .find(|l| l.starts_with("GOT:"))
        .unwrap_or_else(|| panic!("agent did not receive a permission response; stderr: {joined:?}"));
    let body = got.trim_start_matches("GOT:").trim();
    let v: serde_json::Value = serde_json::from_str(body)
        .unwrap_or_else(|e| panic!("response was not JSON ({e}): {body:?} (stderr {joined:?})"));
    assert_eq!(v["type"], "permission_response");
    assert_eq!(v["request_id"], "req-1");
    assert_eq!(v["behavior"], "deny");
}
