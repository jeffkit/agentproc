//! Session history persistence.
//!
//! Each session is a newline-delimited JSON file at
//! `<session_dir>/<session_id>.jsonl`. Mirrors the Python / Node SDK
//! `loadHistory` / `appendHistory` / `sessionFilePath` surface.

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HistoryEntry {
    pub role: String,
    pub content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<serde_json::Value>,
}

/// Resolve the JSONL file for a session id.
pub fn session_file_path(session_id: &str, session_dir: Option<&Path>) -> PathBuf {
    let dir = session_dir
        .map(|p| p.to_path_buf())
        .or_else(default_session_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    dir.join(format!("{session_id}.jsonl"))
}

fn default_session_dir() -> Option<PathBuf> {
    std::env::var("AGENTPROC_SESSION_DIR")
        .ok()
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var("HOME").ok().map(|h| PathBuf::from(h).join(".agentproc").join("sessions"))
        })
}

/// Load all entries for a session. Returns an empty vec if the file does not
/// exist (new session).
pub fn load_history(session_id: &str, session_dir: Option<&Path>) -> Vec<HistoryEntry> {
    let path = session_file_path(session_id, session_dir);
    let Ok(file) = File::open(&path) else {
        return Vec::new();
    };
    let reader = BufReader::new(file);
    let mut out = Vec::new();
    for line in reader.lines().flatten() {
        if let Ok(entry) = serde_json::from_str::<HistoryEntry>(&line) {
            out.push(entry);
        }
    }
    out
}

/// Append entries to a session's JSONL file, creating the file and parent
/// directory if needed.
pub fn append_history(
    session_id: &str,
    entries: &[HistoryEntry],
    session_dir: Option<&Path>,
) -> std::io::Result<()> {
    if entries.is_empty() {
        return Ok(());
    }
    let path = session_file_path(session_id, session_dir);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)?;
    for entry in entries {
        let line = serde_json::to_string(entry)?;
        writeln!(file, "{line}")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn append_and_load_roundtrip() {
        let tmp = TempDir::new().unwrap();
        let dir = tmp.path();
        let entries = vec![
            HistoryEntry {
                role: "user".into(),
                content: "hi".into(),
                usage: None,
            },
            HistoryEntry {
                role: "assistant".into(),
                content: "hello".into(),
                usage: Some(serde_json::json!({"output_tokens": 5})),
            },
        ];
        append_history("s1", &entries, Some(dir)).unwrap();
        let loaded = load_history("s1", Some(dir));
        assert_eq!(loaded.len(), 2);
        assert_eq!(loaded[0].content, "hi");
        assert_eq!(loaded[1].role, "assistant");
    }

    #[test]
    fn load_missing_returns_empty() {
        let tmp = TempDir::new().unwrap();
        let loaded = load_history("nope", Some(tmp.path()));
        assert!(loaded.is_empty());
    }
}
