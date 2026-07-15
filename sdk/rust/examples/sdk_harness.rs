//! sdk_harness: minimal AgentProc agent for conformance testing.
//!
//! Reads a turn object from stdin, writes canned NDJSON events to stdout.
//! Scenario is chosen via `--kind <name>`:
//!
//! - `hello` — emit one partial + result with session_id
//! - `error` — emit an error event
//! - `empty` — emit only a result with empty text
//!
//! Used by `src/conformance.rs` to validate the spawn path end-to-end.

use std::io::{self, Read};

#[derive(serde::Deserialize)]
struct Turn {
    message: String,
    #[serde(default)]
    session_id: String,
}

fn main() -> io::Result<()> {
    let mut buf = String::new();
    io::stdin().read_to_string(&mut buf)?;
    let first_line = buf.lines().next().unwrap_or("");
    let turn: Turn = serde_json::from_str(first_line).unwrap_or(Turn {
        message: String::new(),
        session_id: String::new(),
    });

    let kind = std::env::args()
        .position(|a| a == "--kind")
        .and_then(|i| std::env::args().nth(i + 1))
        .unwrap_or_else(|| "hello".to_string());

    match kind.as_str() {
        "hello" => {
            println!(
                r#"{{"type":"partial","text":"Hi "}}"#
            );
            println!(
                r#"{{"type":"partial","text":"there!"}}"#
            );
            println!(
                r#"{{"type":"result","text":"Hi there!","session_id":"harness-1"}}"#
            );
        }
        "error" => {
            println!(
                r#"{{"type":"error","message":"boom: {}"}}"#,
                turn.message
            );
        }
        "empty" => {
            println!(r#"{{"type":"result","text":"","session_id":"harness-empty"}}\"#);
            println!(r#""#);
        }
        other => {
            println!(r#"{{"type":"error","message":"unknown kind: {other}"}}"#);
        }
    }
    Ok(())
}
