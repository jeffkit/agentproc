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

/// Executor (in-process) path conformance — reads `spec/conformance/executors.json`
/// and drives it through the public `run()` with a registered fake executor
/// (printf-backed `build_args` + the fixture's shared rule-table `parse_event`),
/// asserting the full RunResult. Same fixture, same expectations as
/// `sdk/python/tests/test_conformance.py` and `sdk/node/src/conformance.test.js`.
#[cfg(feature = "executors")]
mod executor_scenarios {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use crate::executors::{register_executor, Executor, TurnCtx, TurnHandlers};
    use crate::{run, ParseResult, Profile, RunOptions};

    /// `register_executor` takes a plain `fn` factory (no captured state), so
    /// the scenario currently under test travels through this static.
    static LINES: Mutex<Vec<serde_json::Value>> = Mutex::new(Vec::new());

    struct FakeHandlers {
        lines: Vec<serde_json::Value>,
    }

    impl TurnHandlers for FakeHandlers {
        fn build_args(
            &self,
            _message: &str,
            _session_id: &str,
            _env: &HashMap<String, String>,
        ) -> Vec<String> {
            let joined = self
                .lines
                .iter()
                .map(|l| l.to_string())
                .collect::<Vec<_>>()
                .join("\n");
            vec!["printf".to_string(), "%s\\n".to_string(), joined]
        }

        /// The fixture's shared rule table (see `executors.json` `_comment`).
        fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult> {
            match event.get("type").and_then(|v| v.as_str())? {
                "partial" => Some(ParseResult::partial(
                    event.get("text").and_then(|v| v.as_str()).unwrap_or(""),
                )),
                "result" => {
                    let mut r = ParseResult::final_text(
                        event.get("text").and_then(|v| v.as_str()).unwrap_or(""),
                    );
                    if let Some(sid) = event.get("session_id").and_then(|v| v.as_str()) {
                        r.session_id = Some(sid.to_string());
                    }
                    if let Some(u) = event.get("usage") {
                        r.usage = Some(u.clone());
                    }
                    Some(r)
                }
                "error" => {
                    let mut r = ParseResult::error(
                        event.get("message").and_then(|v| v.as_str()).unwrap_or(""),
                    );
                    if let Some(sid) = event.get("session_id").and_then(|v| v.as_str()) {
                        r.session_id = Some(sid.to_string());
                    }
                    if let Some(u) = event.get("usage") {
                        r.usage = Some(u.clone());
                    }
                    Some(r)
                }
                _ => None,
            }
        }
    }

    struct FakeExecutor;

    impl Executor for FakeExecutor {
        fn cli_name(&self) -> &str {
            "conformance-fake-cli"
        }
        fn install_hint(&self) -> &str {
            ""
        }
        fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
            Box::new(FakeHandlers {
                lines: LINES.lock().unwrap().clone(),
            })
        }
    }

    fn fake_factory() -> Box<dyn Executor> {
        Box::new(FakeExecutor)
    }

    #[tokio::test]
    async fn executor_path_matches_executors_json() {
        let path = super::conformance_dir().join("executors.json");
        let raw = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        let file: serde_json::Value =
            serde_json::from_str(&raw).unwrap_or_else(|e| panic!("parse {}: {e}", path.display()));
        let scenarios = file["scenarios"].as_array().cloned().unwrap_or_default();
        // Sanity: an emptied or mis-pathed fixture must fail, not pass vacuously.
        assert!(
            scenarios.len() >= 13,
            "expected >= 13 executor scenarios, got {}",
            scenarios.len()
        );

        register_executor("conformance-fake-exec", fake_factory);
        let mut divergences: Vec<String> = Vec::new();

        for sc in &scenarios {
            let name = sc["name"].as_str().unwrap_or("?");
            let exp = &sc["expect"];
            *LINES.lock().unwrap() = sc["lines"].as_array().cloned().unwrap_or_default();

            let mut profile = Profile::default();
            profile.executor = Some("conformance-fake-exec".to_string());
            profile.streaming = sc["streaming"].as_bool().unwrap_or(true);

            let partials = Arc::new(Mutex::new(Vec::<String>::new()));
            let sink = partials.clone();
            let opts = RunOptions::new("hello").on_partial(move |text, _sid| {
                sink.lock().unwrap().push(text);
            });

            let result = run(&profile, opts)
                .await
                .unwrap_or_else(|e| panic!("{name}: run failed: {e}"));

            let checks: [(&str, String, String); 4] = [
                ("reply", result.reply.clone(), exp["reply"].as_str().unwrap_or("").to_string()),
                (
                    "session_id",
                    result.session_id.clone(),
                    exp["session_id"].as_str().unwrap_or("").to_string(),
                ),
                ("error", result.error.clone(), exp["error"].as_str().unwrap_or("").to_string()),
                (
                    "exit_code",
                    result.exit_code.to_string(),
                    exp["exit_code"].as_i64().unwrap_or(0).to_string(),
                ),
            ];
            for (field, got, want) in checks {
                if got != want {
                    divergences.push(format!("{name}: {field}: got {got:?} want {want:?}"));
                }
            }

            let want_usage = if exp["usage"].is_null() {
                None
            } else {
                Some(exp["usage"].clone())
            };
            if result.usage != want_usage {
                divergences.push(format!(
                    "{name}: usage: got {:?} want {want_usage:?}",
                    result.usage
                ));
            }

            let got_partials = partials.lock().unwrap().clone();
            let want_partials: Vec<String> = exp["partials"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .map(|v| v.as_str().unwrap_or("").to_string())
                        .collect()
                })
                .unwrap_or_default();
            if got_partials != want_partials {
                divergences.push(format!(
                    "{name}: partials: got {got_partials:?} want {want_partials:?}"
                ));
            }
        }

        assert!(
            divergences.is_empty(),
            "Rust executor path diverges from executors.json:\n  {}",
            divergences.join("\n  ")
        );
    }
}
