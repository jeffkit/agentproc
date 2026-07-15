//! # agentproc
//!
//! Rust SDK for the AgentProc protocol — connect any agent CLI to a messaging
//! platform via a process-based interface.
//!
//! The SDK is the Rust sibling of the Python and Node SDKs, implementing the
//! same wire protocol (`0.4`) and the same in-process executor mechanism
//! (profile `executor:` field). One profile can run three ways depending on
//! what the host has installed: Rust executor (in-process), Node executor
//! (in-process), or a Python bridge script (spawn) — all producing the same
//! observable NDJSON.
//!
//! ## Quick start
//!
//! ```no_run
//! # #[cfg(feature = "yaml")] {
//! use agentproc::{run, Profile, RunOptions};
//!
//! # async fn demo() -> Result<(), Box<dyn std::error::Error>> {
//! let profile = Profile::from_path("profile.yaml")?;
//! let result = run(&profile, RunOptions::new("hello")).await?;
//! println!("{}", result.reply);
//! # Ok(())
//! # }
//! # }
//! ```
//!
//! See `spec/protocol.md` in the agentproc repository for the protocol
//! specification, including the in-process executor contract.

mod env;

pub use env::{build_base_env, expand_env_ref, expand_env_ref_with_allowlist, substitute, SubstCtx};
mod error;
pub mod history;
mod protocol;
mod profile;
mod runner;

#[cfg(feature = "executors")]
pub mod executors;

#[cfg(test)]
mod conformance;

pub use error::{protocol_error, ProtocolError, RunnerError};
pub use protocol::{
    parse_event, read_turn, Attachment, AgentEvent, PartialRole, PermissionBehavior,
    PermissionRequest, PermissionResponse, TurnInput, TurnObject, PROTOCOL_VERSION,
};
pub use profile::Profile;
pub use runner::{run, RunOptions, RunResult};

#[cfg(feature = "executors")]
pub use executors::{
    executor_names, Executor, ParseResult, TurnHandlers, UnknownExecutor,
};

/// Alias for an async permission decision callback's return type.
///
/// `on_permission` on [`RunOptions`] returns this so the runner can `await`
/// a user's allow/deny decision without holding a synchronous lock.
pub type PermissionFuture = std::pin::Pin<Box<dyn std::future::Future<Output = PermissionDecision> + Send>>;

/// The decision returned by `on_permission`: allow (optionally with an
/// updated tool input) or deny (with a reason).
#[derive(Debug, Clone)]
pub enum PermissionDecision {
    /// Allow the tool call. `updated_input`, when `Some`, overrides the
    /// original tool input (e.g. Claude Code's `updatedInput`).
    Allow { updated_input: Option<serde_json::Value> },
    /// Deny the tool call with a human-readable reason.
    Deny { message: String },
}

impl PermissionDecision {
    pub fn allow() -> Self {
        Self::Allow { updated_input: None }
    }
    pub fn deny(reason: impl Into<String>) -> Self {
        Self::Deny { message: reason.into() }
    }
}
