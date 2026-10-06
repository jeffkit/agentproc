//! Conformance tests — read `spec/conformance/*.json` and assert the Rust
//! implementation produces the same classifications as Python / Node.
//!
//! These are compile-time `#[cfg(test)]` modules; they do not ship in the
//! published crate. The JSON fixtures are the single source of truth —
//! translating them to Rust test cases by hand would drift.

#![cfg(test)]

use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct CasesFile {
    cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
struct Case {
    line: String,
    expect: Expect,
}

#[derive(Debug, Deserialize)]
struct Expect {
    kind: String,
    #[serde(default)]
    value: serde_json::Value,
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    session_id: Option<String>,
}

/// The conformance cases assert a `classify_line`-shaped result. The Rust
/// API uses `parse_event` returning `Option<AgentEvent>` — we project that
/// into the {kind, value, role, session_id} shape the fixtures expect so the
/// same JSON drives all three SDKs.
#[derive(Debug, PartialEq)]
struct Classified {
    kind: String,
    value: serde_json::Value,
    role: Option<String>,
    session_id: Option<String>,
}

fn classify(line: &str) -> Classified {
    use crate::protocol::{parse_event, AgentEvent, PartialRole};
    match parse_event(line) {
        Some(AgentEvent::Partial { text, role, session_id }) => Classified {
            kind: "partial".into(),
            value: serde_json::Value::String(text),
            role: role.map(|r| match r {
                PartialRole::Output => "output".into(),
                PartialRole::Thinking => "thinking".into(),
            }),
            session_id,
        },
        Some(AgentEvent::Result { text, session_id, .. }) => Classified {
            kind: "result".into(),
            value: serde_json::Value::String(text),
            role: None,
            session_id,
        },
        Some(AgentEvent::Error { message, session_id, .. }) => Classified {
            kind: "error".into(),
            value: serde_json::Value::String(message),
            role: None,
            session_id,
        },
        Some(AgentEvent::PermissionRequest(_)) => {
            // The fixture compares the raw decoded object (type + fields the
            // agent sent). Re-serialising our PermissionRequest struct would
            // drop `type` and add nulls, so echo the original JSON.
            let value: serde_json::Value =
                serde_json::from_str(line.trim()).unwrap_or(serde_json::Value::Null);
            Classified {
                kind: "permission_request".into(),
                value,
                role: None,
                session_id: None,
            }
        }
        None => Classified {
            kind: "malformed".into(),
            value: serde_json::Value::String(line.into()),
            role: None,
            session_id: None,
        },
    }
}

/// Path to the spec/conformance directory. Resolved relative to the crate
/// root (CARGO_MANIFEST_DIR = sdk/rust), so the tests work whether run from
/// the crate dir or the repo root.
fn conformance_dir() -> std::path::PathBuf {
    let manifest = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    // sdk/rust -> sdk -> repo root
    manifest
        .parent()
        .and_then(|p| p.parent())
        .map(|root| root.join("spec").join("conformance"))
        .expect("could not locate repo root from CARGO_MANIFEST_DIR")
}

#[test]
fn cases_json_matches_node_classification() {
    let path = conformance_dir().join("cases.json");
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    let file: CasesFile = serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("parse {}: {e}", path.display()));

    let mut checked = 0;
    for case in &file.cases {
        let got = classify(&case.line);
        assert_eq!(got.kind, case.expect.kind, "line: {}", case.line);
        assert_eq!(got.value, case.expect.value, "value mismatch: {}", case.line);
        assert_eq!(got.role, case.expect.role, "role mismatch: {}", case.line);
        assert_eq!(
            got.session_id, case.expect.session_id,
            "session_id mismatch: {}",
            case.line
        );
        checked += 1;
    }
    // Sanity: the suite must actually contain cases, not silently be empty.
    assert!(checked > 20, "expected >20 conformance cases, got {checked}");
}

// ---------------------------------------------------------------------------
// posture_cases — in-process executor permission posture
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct PostureFile {
    auto_approve_flags: Vec<String>,
    posture_cases: Vec<PostureCase>,
}

#[derive(Debug, Deserialize)]
struct PostureCase {
    name: String,
    executor: String,
    #[serde(default)]
    permission: bool,
    #[serde(default)]
    env: std::collections::HashMap<String, String>,
    expect: PostureExpect,
}

#[derive(Debug, Deserialize)]
struct PostureExpect {
    #[serde(default)]
    error: bool,
    #[serde(default)]
    refused: bool,
    #[serde(default)]
    argv_contains: Vec<String>,
    #[serde(default)]
    argv_excludes: Vec<String>,
    // `reply` / `exit_zero` need a real spawn; the Python and Node drivers
    // cover them. This driver asserts only the refusal decision and the argv
    // of a non-refused case.
}

#[test]
fn posture_cases_match_rust_permission_gate() {
    let path = conformance_dir().join("cases.json");
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    let file: PostureFile = serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("parse {}: {e}", path.display()));

    let embedded: Vec<String> = crate::executors::AUTO_APPROVE_FLAGS
        .iter()
        .map(|s| s.to_string())
        .collect();
    assert_eq!(
        file.auto_approve_flags, embedded,
        "embedded AUTO_APPROVE_FLAGS must match auto_approve_flags item for item"
    );

    let mut checked = 0;
    for case in &file.posture_cases {
        let exec = crate::executors::lookup(&case.executor)
            .unwrap_or_else(|| panic!("{}: unknown executor `{}`", case.name, case.executor));
        let handlers = exec.make_turn(&crate::executors::TurnCtx { permission: case.permission });
        let argv = handlers.build_args("", "", &case.env);

        let auto_approve = match case.env.get("AGENTPROC_AUTO_APPROVE") {
            Some(value) => {
                let v = value.trim().to_ascii_lowercase();
                v != "0" && v != "false"
            }
            None => true,
        };
        let refusal = crate::runner::posture_refusal(
            exec.cli_name(),
            exec.supports_permission(),
            case.permission,
            auto_approve,
            &argv,
        );

        let expects_refusal = case.expect.error || case.expect.refused;
        assert_eq!(
            refusal.is_some(),
            expects_refusal,
            "{}: refusal decision ({refusal:?})",
            case.name
        );
        if refusal.is_some() {
            // No argv exists for a refused turn — the token lists are not checked.
            checked += 1;
            continue;
        }
        for token in &case.expect.argv_contains {
            assert!(
                argv.iter().any(|a| a == token),
                "{}: {token} missing from {argv:?}",
                case.name
            );
        }
        for token in &case.expect.argv_excludes {
            assert!(
                !argv.iter().any(|a| a == token),
                "{}: {token} present in {argv:?}",
                case.name
            );
        }
        checked += 1;
    }
    assert!(checked > 10, "expected >10 posture cases, got {checked}");
}
