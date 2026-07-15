//! Profile YAML parsing — single-profile (flat or `agentproc:`-nested) form.
//!
//! Multi-profile YAML with `routing:` is an ilink-hub / host concern and is
//! NOT handled here. This module parses the standard AgentProc profile shape:
//!
//! ```yaml
//! # Flat form (spec example):
//! command: python3
//! args: ["{{PROFILE_DIR}}/bridge.py"]
//!
//! # Hub form (nested under `agentproc:`):
//! agentproc:
//!   executor: codex          # optional in-process executor
//!   command: python3         # fallback when executor unknown / absent
//!   args: ["{{PROFILE_DIR}}/bridge.py"]
//!   timeout_secs: 600
//! ```

use std::collections::HashMap;
use std::path::Path;

use crate::error::RunnerError;

#[cfg(feature = "yaml")]
use serde::Deserialize;

/// Normalised profile — the result of parsing a YAML file or building one
/// programmatically. Field names mirror the wire-level profile schema in
/// `spec/protocol.md`.
#[derive(Debug, Clone)]
pub struct Profile {
    /// Optional in-process executor name (e.g. `"codex"`). When set and the
    /// SDK recognises it, the runner uses the executor instead of spawning
    /// `command`/`args`. When set but unrecognised, the runner warns and
    /// falls back to `command`/`args` (hard-fail if `command` is empty).
    pub executor: Option<String>,

    /// The executable (argv[0]). Required when `executor` is absent or
    /// unrecognised; ignored otherwise.
    pub command: String,
    /// Additional argv tokens (argv[1..]).
    pub args: Vec<String>,
    /// Working directory. `~` and placeholders expanded at run time.
    pub cwd: Option<String>,
    /// Profile environment block (pre-`${VAR}` expansion).
    pub env: HashMap<String, String>,
    /// Restricts `${VAR}` expansion to the listed names. `None` = expand
    /// against the full environment.
    pub env_allowlist: Option<Vec<String>>,
    pub timeout_secs: u64,
    pub kill_grace_secs: u64,
    pub max_reply_chars: usize,
    pub truncation_suffix: String,
    pub include_stderr_in_reply: bool,
    pub send_error_reply: bool,
    pub streaming: bool,
    pub permission: bool,
}

impl Default for Profile {
    fn default() -> Self {
        Self {
            executor: None,
            command: String::new(),
            args: Vec::new(),
            cwd: None,
            env: HashMap::new(),
            env_allowlist: None,
            timeout_secs: DEFAULT_TIMEOUT_SECS,
            kill_grace_secs: DEFAULT_KILL_GRACE_SECS,
            max_reply_chars: DEFAULT_MAX_REPLY_CHARS,
            truncation_suffix: DEFAULT_TRUNCATION_SUFFIX.into(),
            include_stderr_in_reply: false,
            send_error_reply: true,
            streaming: true,
            permission: false,
        }
    }
}

pub const DEFAULT_TIMEOUT_SECS: u64 = 1800;
pub const DEFAULT_KILL_GRACE_SECS: u64 = 5;
pub const DEFAULT_MAX_REPLY_CHARS: usize = 8000;
pub const DEFAULT_TRUNCATION_SUFFIX: &str = "\n\n…(truncated)";

impl Profile {
    /// Load and parse a profile YAML file from disk.
    #[cfg(feature = "yaml")]
    pub fn from_path(path: impl AsRef<Path>) -> Result<Self, RunnerError> {
        let path = path.as_ref();
        let raw = std::fs::read_to_string(path)
            .map_err(|e| RunnerError::Io(e))?;
        Self::from_yaml(&raw).map_err(|e| {
            RunnerError::Yaml(format!("{}: {}", path.display(), e))
        })
    }

    /// Parse a profile YAML string.
    #[cfg(feature = "yaml")]
    pub fn from_yaml(raw: &str) -> Result<Self, RunnerError> {
        let file: ProfileFileRaw = serde_yaml::from_str(raw)
            .map_err(|e| RunnerError::Yaml(e.to_string()))?;
        Self::from_raw(file)
    }

    /// Build a profile from the raw (possibly nested) YAML representation.
    /// Exposed for programmatic construction without the `yaml` feature.
    pub fn from_raw(raw: ProfileFileRaw) -> Result<Self, RunnerError> {
        let src = raw.agentproc.unwrap_or(raw.flat);
        Self::from_block(src)
    }

    fn from_block(src: ProfileBlock) -> Result<Self, RunnerError> {
        let command = src.command.unwrap_or_default();
        let args = src.args.unwrap_or_default();
        let env = src.env.unwrap_or_default();

        if command.trim().is_empty() && src.executor.is_none() {
            return Err(RunnerError::EmptyCommand);
        }

        Ok(Profile {
            executor: src.executor,
            command,
            args,
            cwd: src.cwd,
            env,
            env_allowlist: src.env_allowlist,
            timeout_secs: src
                .timeout_secs
                .unwrap_or(DEFAULT_TIMEOUT_SECS),
            kill_grace_secs: src
                .kill_grace_secs
                .unwrap_or(DEFAULT_KILL_GRACE_SECS),
            max_reply_chars: src
                .max_reply_chars
                .unwrap_or(DEFAULT_MAX_REPLY_CHARS),
            truncation_suffix: src
                .truncation_suffix
                .unwrap_or_else(|| DEFAULT_TRUNCATION_SUFFIX.into()),
            include_stderr_in_reply: src.include_stderr_in_reply.unwrap_or(false),
            send_error_reply: src.send_error_reply.unwrap_or(true),
            streaming: src.streaming.unwrap_or(true),
            permission: src.permission.unwrap_or(false),
        })
    }

    /// True when this profile requests the in-process executor path and the
    /// SDK recognises the name.
    #[cfg(feature = "executors")]
    pub fn executor_known(&self) -> bool {
        match &self.executor {
            Some(name) => crate::executors::lookup(name).is_some(),
            None => false,
        }
    }

    #[cfg(not(feature = "executors"))]
    pub fn executor_known(&self) -> bool {
        false
    }
}

/// Raw YAML representation: either flat (top-level fields) or nested under
/// `agentproc:`. The hub profile form nests execution config under
/// `agentproc:` and keeps metadata (`name`, `description`, ...) as siblings.
#[derive(Debug, Default)]
#[cfg_attr(feature = "yaml", derive(Deserialize))]
#[cfg_attr(feature = "yaml", serde(default))]
pub struct ProfileFileRaw {
    /// The nested `agentproc:` block, when the profile uses hub form.
    #[cfg_attr(feature = "yaml", serde(default))]
    pub agentproc: Option<ProfileBlock>,
    /// Flat top-level fields (when not using `agentproc:` nesting).
    #[cfg_attr(feature = "yaml", serde(flatten))]
    pub flat: ProfileBlock,
}

#[derive(Debug, Default, Clone)]
#[cfg_attr(feature = "yaml", derive(Deserialize))]
#[cfg_attr(feature = "yaml", serde(default))]
pub struct ProfileBlock {
    pub executor: Option<String>,
    pub command: Option<String>,
    pub args: Option<Vec<String>>,
    pub cwd: Option<String>,
    pub env: Option<HashMap<String, String>>,
    pub env_allowlist: Option<Vec<String>>,
    pub timeout_secs: Option<u64>,
    pub kill_grace_secs: Option<u64>,
    pub max_reply_chars: Option<usize>,
    pub truncation_suffix: Option<String>,
    pub include_stderr_in_reply: Option<bool>,
    pub send_error_reply: Option<bool>,
    pub streaming: Option<bool>,
    pub permission: Option<bool>,
}

#[cfg(all(test, feature = "yaml"))]
mod tests {
    use super::*;

    #[test]
    fn parse_flat_profile() {
        let yaml = "command: ./agent\nargs: [\"hi\"]\ntimeout_secs: 30\n";
        let p = Profile::from_yaml(yaml).unwrap();
        assert_eq!(p.command, "./agent");
        assert_eq!(p.args, vec!["hi".to_string()]);
        assert_eq!(p.timeout_secs, 30);
        assert!(p.executor.is_none());
    }

    #[test]
    fn parse_nested_agentproc_block() {
        let yaml = "\
name: codex
description: demo
agentproc:
  executor: codex
  command: python3
  args: [\"bridge.py\"]
  timeout_secs: 600
  streaming: true
";
        let p = Profile::from_yaml(yaml).unwrap();
        assert_eq!(p.executor.as_deref(), Some("codex"));
        assert_eq!(p.command, "python3");
        assert_eq!(p.args, vec!["bridge.py".to_string()]);
        assert_eq!(p.timeout_secs, 600);
        // Unknown top-level metadata (name/description) is ignored.
    }

    #[test]
    fn empty_command_without_executor_errors() {
        let yaml = "timeout_secs: 30\n";
        assert!(matches!(Profile::from_yaml(yaml), Err(RunnerError::EmptyCommand)));
    }

    #[test]
    fn empty_command_ok_when_executor_set() {
        // executor + no command is valid at parse time (runner decides if
        // the executor name is known; hard-fail only happens if unknown too).
        let yaml = "agentproc:\n  executor: codex\n";
        let p = Profile::from_yaml(yaml).unwrap();
        assert_eq!(p.executor.as_deref(), Some("codex"));
        assert!(p.command.is_empty());
    }

    #[test]
    fn defaults_applied() {
        let yaml = "command: ./a\n";
        let p = Profile::from_yaml(yaml).unwrap();
        assert_eq!(p.timeout_secs, DEFAULT_TIMEOUT_SECS);
        assert_eq!(p.kill_grace_secs, DEFAULT_KILL_GRACE_SECS);
        assert_eq!(p.max_reply_chars, DEFAULT_MAX_REPLY_CHARS);
        assert!(p.streaming);
        assert!(p.send_error_reply);
        assert!(!p.permission);
    }
}
