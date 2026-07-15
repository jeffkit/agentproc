//! In-process executor registry and built-in executors.
//!
//! An executor is a named, in-process implementation of the bridge side of
//! the AgentProc protocol — see `spec/protocol.md` §"In-process executors".
//! When a profile sets `executor: <name>` and the SDK recognises the name,
//! the runner calls the executor directly (no bridge subprocess fork) to
//! spawn the target CLI and translate its output.
//!
//! ## Two traits
//!
//! [`Executor`] is the per-CLI adapter (stateless: knows `cli_name`,
//! `install_hint`, `plain`). [`TurnHandlers`] is the per-turn pair of
//! `build_args` + `parse_event`. Stateful executors (cursor, kimi-code)
//! hold per-turn state inside their `TurnHandlers` impl; stateless ones
//! implement both traits on the same unit struct.

use std::collections::HashMap;
use std::sync::RwLock;

use once_cell::sync::Lazy;

use crate::env::SubstCtx;

/// The result of parsing one CLI stdout event. All fields optional.
#[derive(Debug, Clone, Default)]
pub struct ParseResult {
    /// A streaming chunk (forwarded as `{"type":"partial"}` when streaming).
    pub partial_text: Option<String>,
    /// Terminal reply body. `Some(None)` = explicit "no text body this event";
    /// `None` = this event does not contribute a final body.
    pub final_text: Option<Option<String>>,
    /// Session id to persist for the next turn. First non-empty value wins.
    pub session_id: Option<String>,
    /// Terminal error message (emitted as `{"type":"error"}`).
    pub error: Option<String>,
    /// Token / cost stats attached to the run result.
    pub usage: Option<serde_json::Value>,
}

impl ParseResult {
    pub fn partial(text: impl Into<String>) -> Self {
        Self { partial_text: Some(text.into()), ..Default::default() }
    }
    pub fn final_text(text: impl Into<String>) -> Self {
        Self { final_text: Some(Some(text.into())), ..Default::default() }
    }
    pub fn session(sid: impl Into<String>) -> Self {
        Self { session_id: Some(sid.into()), ..Default::default() }
    }
    pub fn error(msg: impl Into<String>) -> Self {
        Self { error: Some(msg.into()), ..Default::default() }
    }
}

/// Per-CLI executor adapter. Stateless across turns — implement
/// [`TurnHandlers`] on a separate struct (or the same one) for the per-turn
/// pair.
pub trait Executor: Send + Sync {
    fn cli_name(&self) -> &str;
    fn install_hint(&self) -> &str;
    /// `true` = CLI emits plain text on stdout (not NDJSON). The runner treats
    /// stdout as the reply body and does not call `parse_event`.
    fn plain(&self) -> bool {
        false
    }
    /// Build a fresh per-turn handlers pair. Called once per turn.
    fn make_turn(&self) -> Box<dyn TurnHandlers>;
}

/// Per-turn pair: `build_args` runs once before spawn, `parse_event` runs
/// once per stdout line. Stateful executors hold state in `self`.
pub trait TurnHandlers: Send {
    /// Build the target CLI's argv. `env` is the composed child environment
    /// (infra set + profile env + caller extras). Returning an empty vec is
    /// a hard error.
    fn build_args(
        &self,
        message: &str,
        session_id: &str,
        env: &HashMap<String, String>,
    ) -> Vec<String>;
    /// Translate one decoded JSON object from the CLI's stdout into a
    /// [`ParseResult`]. Return `None` for events the executor does not
    /// recognise. Not called when `Executor::plain()` returns `true`.
    fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult>;
}

/// Stub type returned when an executor name is not registered. Lets the
/// runner distinguish "unknown executor" from "no executor field" cleanly.
pub struct UnknownExecutor;

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

type Factory = fn() -> Box<dyn Executor>;

/// Static registry of built-in executors. Populated at first access.
static STATIC_EXECUTORS: Lazy<HashMap<&'static str, Factory>> = Lazy::new(|| {
    let mut m = HashMap::new();
    m.insert("codex", codex_factory as Factory);
    m.insert("claude-code", claude_code_factory as Factory);
    m
});

/// Dynamic registry for `register_executor` (host-supplied executors).
static DYNAMIC_EXECUTORS: Lazy<RwLock<HashMap<String, Factory>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

fn codex_factory() -> Box<dyn Executor> {
    Box::new(CodexExecutor)
}
fn claude_code_factory() -> Box<dyn Executor> {
    Box::new(ClaudeCodeExecutor)
}

/// Look up an executor by name. Checks dynamic registry first, then static.
pub fn lookup(name: &str) -> Option<Box<dyn Executor>> {
    if let Ok(dyn_map) = DYNAMIC_EXECUTORS.read() {
        if let Some(factory) = dyn_map.get(name) {
            return Some(factory());
        }
    }
    STATIC_EXECUTORS.get(name).map(|f| f())
}

/// All known executor names (static + dynamic), sorted for stable display.
pub fn executor_names() -> Vec<String> {
    let mut names: Vec<String> = STATIC_EXECUTORS.keys().map(|s| s.to_string()).collect();
    if let Ok(dyn_map) = DYNAMIC_EXECUTORS.read() {
        names.extend(dyn_map.keys().cloned());
    }
    names.sort();
    names
}

/// Register a custom executor at runtime. Subsequent `lookup(name)` calls
/// return this factory's product. Hosts (e.g. ilink-hub) use this to plug in
/// their own executors without forking the crate.
pub fn register_executor(name: &str, factory: Factory) {
    let mut map = DYNAMIC_EXECUTORS.write().unwrap();
    map.insert(name.to_string(), factory);
}

// ---------------------------------------------------------------------------
// codex
// ---------------------------------------------------------------------------

/// OpenAI Codex CLI (`codex exec --json`). Stateless NDJSON executor.
pub struct CodexExecutor;

impl Executor for CodexExecutor {
    fn cli_name(&self) -> &str {
        "codex"
    }
    fn install_hint(&self) -> &str {
        "Install: npm install -g @openai/codex"
    }
    fn make_turn(&self) -> Box<dyn TurnHandlers> {
        Box::new(CodexTurn)
    }
}

struct CodexTurn;

impl TurnHandlers for CodexTurn {
    fn build_args(
        &self,
        message: &str,
        session_id: &str,
        _env: &HashMap<String, String>,
    ) -> Vec<String> {
        // codex exec [resume <session_id>] <message> --dangerously-bypass-approvals-and-sandbox --json
        let mut args: Vec<String> = vec!["exec".into()];
        if !session_id.is_empty() {
            args.push("resume".into());
            args.push(session_id.to_string());
        }
        args.push(message.to_string());
        args.push("--dangerously-bypass-approvals-and-sandbox".into());
        args.push("--json".into());
        // argv[0] is the CLI binary name, prepended by the runner before spawn.
        // Executors here return argv[1..] for clarity; the runner prepends
        // cli_name. See runner::run_via_executor.
        let mut full = vec![CodexExecutor.cli_name().to_string()];
        full.extend(args);
        full
    }

    fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult> {
        let etype = event.get("type")?.as_str()?;
        match etype {
            "thread.started" => {
                let sid = event.get("thread_id")?.as_str()?;
                if sid.is_empty() {
                    None
                } else {
                    Some(ParseResult::session(sid))
                }
            }
            "item.completed" => {
                let item = event.get("item")?;
                if item.get("type").and_then(|v| v.as_str()) != Some("agent_message") {
                    return None;
                }
                let text = item.get("text")?.as_str()?;
                if text.trim().is_empty() {
                    None
                } else {
                    Some(ParseResult::partial(text))
                }
            }
            "turn.completed" => {
                // Terminal; optionally carry usage. No body text here — the
                // accumulated partials form the reply.
                let usage = event.get("usage").cloned();
                Some(ParseResult { usage, ..Default::default() })
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// claude-code
// ---------------------------------------------------------------------------

/// Anthropic Claude Code CLI (`claude -p --output-format stream-json`).
/// Stateless NDJSON executor.
pub struct ClaudeCodeExecutor;

impl Executor for ClaudeCodeExecutor {
    fn cli_name(&self) -> &str {
        "claude"
    }
    fn install_hint(&self) -> &str {
        "Install: npm install -g @anthropic-ai/claude-code"
    }
    fn make_turn(&self) -> Box<dyn TurnHandlers> {
        Box::new(ClaudeCodeTurn)
    }
}

struct ClaudeCodeTurn;

impl TurnHandlers for ClaudeCodeTurn {
    fn build_args(
        &self,
        message: &str,
        session_id: &str,
        env: &HashMap<String, String>,
    ) -> Vec<String> {
        let mut args: Vec<String> = vec![
            ClaudeCodeExecutor.cli_name().to_string(),
            "-p".into(),
            message.to_string(),
            "--output-format".into(),
            "stream-json".into(),
            "--dangerously-skip-permissions".into(),
        ];
        let disallow = env
            .get("CLAUDE_DISALLOW_TOOLS")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "AskUserQuestion".to_string());
        if !disallow.is_empty() {
            args.push("--disallowed-tools".into());
            args.push(disallow);
        }
        if let Some(model) = env.get("CLAUDE_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        if !session_id.is_empty() {
            args.push("--resume".into());
            args.push(session_id.to_string());
        }
        args
    }

    fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult> {
        let etype = event.get("type")?.as_str()?;
        match etype {
            "system" if event.get("subtype").and_then(|v| v.as_str()) == Some("init") => {
                let sid = event.get("session_id").and_then(|v| v.as_str())?;
                if sid.is_empty() {
                    None
                } else {
                    Some(ParseResult::session(sid))
                }
            }
            "assistant" => {
                let content = event.get("message").and_then(|m| m.get("content"))?;
                let text: String = content
                    .as_array()?
                    .iter()
                    .filter(|b| b.get("type").and_then(|v| v.as_str()) == Some("text"))
                    .filter_map(|b| b.get("text").and_then(|v| v.as_str()))
                    .collect();
                if text.is_empty() {
                    None
                } else {
                    Some(ParseResult::partial(text))
                }
            }
            "result" => {
                let session_id = event.get("session_id").and_then(|v| v.as_str()).map(String::from);
                if event.get("is_error").and_then(|v| v.as_bool()) == Some(true) {
                    let msg = event
                        .get("result")
                        .and_then(|v| v.as_str())
                        .unwrap_or("claude reported an error");
                    let mut r = ParseResult::error(msg);
                    r.session_id = session_id;
                    return Some(r);
                }
                let text = event.get("result").and_then(|v| v.as_str()).unwrap_or("");
                Some(ParseResult {
                    final_text: Some(if text.is_empty() { None } else { Some(text.to_string()) }),
                    session_id,
                    usage: event.get("usage").cloned(),
                    ..Default::default()
                })
            }
            _ => None,
        }
    }
}

/// Helper for executors that need to substitute placeholders into pre-built
/// argv. Exposed for custom executors; built-in executors do not use it.
pub fn substitute_argv(argv: &[String], ctx: &SubstCtx) -> Vec<String> {
    argv.iter().map(|a| crate::env::substitute(a, ctx)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn lookup_known_executors() {
        assert!(lookup("codex").is_some());
        assert!(lookup("claude-code").is_some());
        assert!(lookup("nope").is_none());
    }

    #[test]
    fn executor_names_includes_builtins() {
        let names = executor_names();
        assert!(names.contains(&"codex".to_string()));
        assert!(names.contains(&"claude-code".to_string()));
    }

    #[test]
    fn codex_build_args_first_turn() {
        let ex = CodexExecutor;
        let h = ex.make_turn();
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        assert_eq!(args[0], "codex");
        assert!(args.iter().any(|a| a == "exec"));
        assert!(args.iter().any(|a| a == "hi"));
        assert!(args.iter().any(|a| a == "--json"));
        assert!(!args.iter().any(|a| a == "resume"));
    }

    #[test]
    fn codex_build_args_resume() {
        let ex = CodexExecutor;
        let h = ex.make_turn();
        let env = HashMap::new();
        let args = h.build_args("hi", "thread-1", &env);
        assert!(args.iter().any(|a| a == "resume"));
        assert!(args.iter().any(|a| a == "thread-1"));
    }

    #[test]
    fn codex_parse_thread_started() {
        let mut h = CodexExecutor.make_turn();
        let r = h.parse_event(json!({
            "type": "thread.started",
            "thread_id": "t1"
        }));
        assert_eq!(r.unwrap().session_id.as_deref(), Some("t1"));
    }

    #[test]
    fn codex_parse_agent_message_partial() {
        let mut h = CodexExecutor.make_turn();
        let r = h.parse_event(json!({
            "type": "item.completed",
            "item": { "type": "agent_message", "text": "hello" }
        }));
        assert_eq!(r.unwrap().partial_text.as_deref(), Some("hello"));
    }

    #[test]
    fn codex_parse_unknown_event_none() {
        let mut h = CodexExecutor.make_turn();
        assert!(h.parse_event(json!({"type": "noise"})).is_none());
    }

    #[test]
    fn claude_code_build_args_with_model() {
        let ex = ClaudeCodeExecutor;
        let h = ex.make_turn();
        let mut env = HashMap::new();
        env.insert("CLAUDE_MODEL".to_string(), "claude-sonnet-4-6".to_string());
        let args = h.build_args("hi", "", &env);
        assert_eq!(args[0], "claude");
        assert!(args.iter().any(|a| a == "claude-sonnet-4-6"));
    }

    #[test]
    fn claude_code_parse_init_session() {
        let mut h = ClaudeCodeExecutor.make_turn();
        let r = h.parse_event(json!({
            "type": "system",
            "subtype": "init",
            "session_id": "cli-sess-1"
        }));
        assert_eq!(r.unwrap().session_id.as_deref(), Some("cli-sess-1"));
    }

    #[test]
    fn claude_code_parse_result_final_text() {
        let mut h = ClaudeCodeExecutor.make_turn();
        let r = h.parse_event(json!({
            "type": "result",
            "result": "final answer",
            "session_id": "s1"
        }));
        let r = r.unwrap();
        assert_eq!(r.final_text.unwrap().unwrap(), "final answer");
        assert_eq!(r.session_id.as_deref(), Some("s1"));
    }

    #[test]
    fn claude_code_parse_result_error() {
        let mut h = ClaudeCodeExecutor.make_turn();
        let r = h.parse_event(json!({
            "type": "result",
            "is_error": true,
            "result": "rate limited"
        }));
        assert_eq!(r.unwrap().error.as_deref(), Some("rate limited"));
    }
}
