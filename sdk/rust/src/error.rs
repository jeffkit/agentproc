//! Error types for the agentproc SDK.

use thiserror::Error;

/// Raised by an agent when it wants to signal a protocol-level error to the
/// bridge (emitted as `{"type":"error"}` on stdout). Mirrors
/// `ProtocolError` in the Python / Node SDKs.
#[derive(Debug, Error)]
#[error("{message}")]
pub struct ProtocolError {
    pub message: String,
}

impl ProtocolError {
    pub fn new(message: impl Into<String>) -> Self {
        Self { message: message.into() }
    }
}

/// Construct a [`ProtocolError`] — convenience for agent handlers.
pub fn protocol_error(message: impl Into<String>) -> ProtocolError {
    ProtocolError::new(message)
}

/// Runner / profile / executor errors. All SDK fallible APIs return this.
#[derive(Debug, Error)]
pub enum RunnerError {
    #[error("profile is invalid: {0}")]
    InvalidProfile(String),

    #[error("profile.command must be a non-empty string")]
    EmptyCommand,

    #[error("profile.executor `{0}` is not registered; known executors: {1}")]
    UnknownExecutor(String, String),

    #[error("profile.args must be a list")]
    InvalidArgs,

    #[error("profile.env must be a mapping")]
    InvalidEnv,

    #[error("profile.env_allowlist must be a list")]
    InvalidAllowlist,

    #[error("placeholder substitution rejected an unsafe value: {0}")]
    Placeholder(String),

    #[error("failed to spawn `{cli}`: {source}")]
    Spawn { cli: String, #[source] source: std::io::Error },

    #[error("agent timed out after {secs}s")]
    Timeout { secs: u64 },

    #[error("profile YAML parse error: {0}")]
    Yaml(String),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
}

impl From<crate::env::PlaceholderError> for RunnerError {
    fn from(e: crate::env::PlaceholderError) -> Self {
        RunnerError::Placeholder(e.to_string())
    }
}
