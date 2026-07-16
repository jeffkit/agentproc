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
    /// Build a fresh per-turn handlers pair. `ctx` carries turn-level config
    /// the executor needs to pick its argv shape — currently whether the
    /// profile enables the permission channel (Claude uses a bidirectional
    /// `--input-format stream-json` + `--permission-prompt-tool stdio` path
    /// when this is true).
    fn make_turn(&self, ctx: &TurnCtx) -> Box<dyn TurnHandlers>;
}

/// Turn-level context passed to [`Executor::make_turn`].
#[derive(Debug, Clone, Default)]
pub struct TurnCtx {
    /// Mirrors `profile.permission`. Executors that have a native tool-auth
    /// channel (Claude) switch to their permission-aware argv when true.
    pub permission: bool,
}

/// Per-turn pair: `build_args` runs once before spawn, `parse_event` runs
/// once per stdout line. Stateful executors hold state in `self`.
#[async_trait::async_trait]
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
    /// For plain executors that mint/reuse a session id in `build_args`
    /// (e.g. `agy --conversation <id>`): return that id so the runner can
    /// populate `RunResult.session_id` after the process exits. NDJSON
    /// executors derive session id from stdout events and leave this as
    /// `None`. Called once after the turn ends.
    fn get_session_id(&self) -> Option<String> {
        None
    }

    // ----- permission channel (optional, only when profile.permission = true) -----

    /// When `profile.permission` is true and the runner takes the in-process
    /// path, this is written to the CLI's stdin once, before the stdout loop
    /// starts. CLIs that drive tool-authorisation via a bidirectional stream
    /// (e.g. Claude `--input-format stream-json`) need this to deliver the
    /// user's message. Returns `None` for CLIs that take the message via argv
    /// (the common case) — the runner then writes nothing.
    ///
    /// `attachments` lets the executor format multimodal content blocks
    /// (image/document base64) when the CLI expects them in the initial
    /// stream-json user message. Async because multimodal requires downloading
    /// the attachment bytes from `att.url`.
    async fn build_initial_stdin(
        &mut self,
        _message: &str,
        _session_id: &str,
        _attachments: &[crate::Attachment],
    ) -> Option<String> {
        None
    }

    /// Inspect a stdout line and decide whether it is a tool-authorisation
    /// request in this CLI's native protocol (e.g. Claude `control_request`
    /// with subtype `can_use_tool`). When it is, return the translated
    /// AgentProc [`PermissionRequest`] plus the original raw event value
    /// (stashed by the runner and passed back to [`write_permission_response`]
    /// so the executor can fill CLI-specific fields like Claude's
    /// `updatedInput` from the original tool input).
    ///
    /// Returning `None` means "not a permission request — feed to
    /// `parse_event` as usual". Default: no CLI-native permission protocol,
    /// the executor emits standard AgentProc `{"type":"permission_request"}`
    /// events directly (handled by `parse_event`).
    fn permission_request_from_event(
        &mut self,
        _event: &serde_json::Value,
    ) -> Option<(crate::PermissionRequest, serde_json::Value)> {
        None
    }

    /// Translate the runner's [`PermissionDecision`] back into the NDJSON line
    /// to write to the CLI's stdin. `original_event` is the raw event that
    /// triggered this permission request (the same value returned by
    /// [`permission_request_from_event`]). Return `None` to let the runner
    /// emit the standard AgentProc `{"type":"permission_response"}` frame —
    /// used by CLIs that already speak AgentProc on stdin.
    fn write_permission_response(
        &self,
        _decision: &crate::PermissionDecision,
        _original_event: &serde_json::Value,
    ) -> Option<String> {
        None
    }
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
    m.insert("codebuddy", codebuddy_factory as Factory);
    m.insert("cursor", cursor_factory as Factory);
    m.insert("gemini-cli", gemini_cli_factory as Factory);
    m.insert("grok-build", grok_build_factory as Factory);
    m.insert("kimi-code", kimi_code_factory as Factory);
    m.insert("opencode", opencode_factory as Factory);
    m.insert("qwen-code", qwen_code_factory as Factory);
    m.insert("agy", agy_factory as Factory);
    m.insert("aider", aider_factory as Factory);
    m.insert("deepseek", deepseek_factory as Factory);
    m.insert("pi", pi_factory as Factory);
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
fn codebuddy_factory() -> Box<dyn Executor> {
    Box::new(CodebuddyExecutor)
}
fn cursor_factory() -> Box<dyn Executor> {
    Box::new(CursorExecutor)
}
fn gemini_cli_factory() -> Box<dyn Executor> {
    Box::new(GeminiCliExecutor)
}
fn grok_build_factory() -> Box<dyn Executor> {
    Box::new(GrokBuildExecutor)
}
fn kimi_code_factory() -> Box<dyn Executor> {
    Box::new(KimiCodeExecutor)
}
fn opencode_factory() -> Box<dyn Executor> {
    Box::new(OpencodeExecutor)
}
fn qwen_code_factory() -> Box<dyn Executor> {
    Box::new(QwenCodeExecutor)
}
fn agy_factory() -> Box<dyn Executor> {
    Box::new(AgyExecutor)
}
fn aider_factory() -> Box<dyn Executor> {
    Box::new(AiderExecutor)
}
fn deepseek_factory() -> Box<dyn Executor> {
    Box::new(DeepseekExecutor)
}
fn pi_factory() -> Box<dyn Executor> {
    Box::new(PiExecutor)
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
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(CodexTurn)
    }
}

struct CodexTurn;

#[async_trait::async_trait]
impl TurnHandlers for CodexTurn {
    fn build_args(
        &self,
        message: &str,
        session_id: &str,
        env: &HashMap<String, String>,
    ) -> Vec<String> {
        // Mirrors node sdk: `codex exec [--json] [resume --json <id>] <msg> [-c model="..."]`
        let model = env.get("CODEX_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty());
        let mut args: Vec<String> = vec![CodexExecutor.cli_name().to_string(), "exec".into()];
        if !session_id.is_empty() {
            args.push("resume".into());
            args.push("--json".into());
            args.push(session_id.to_string());
            args.push(message.to_string());
        } else {
            args.push("--json".into());
            args.push(message.to_string());
        }
        if let Some(m) = model {
            args.push("-c".into());
            args.push(format!("model=\"{m}\""));
        }
        args
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
                let text = item.get("text").and_then(|v| v.as_str()).unwrap_or("");
                if text.is_empty() {
                    None
                } else {
                    Some(ParseResult::partial(text))
                }
            }
            "turn.failed" => {
                let msg = event
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("codex turn failed");
                Some(ParseResult::error(msg))
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
    fn make_turn(&self, ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(ClaudeCodeTurn { permission: ctx.permission })
    }
}

struct ClaudeCodeTurn {
    permission: bool,
}

impl ClaudeCodeTurn {
    fn disallow_arg(env: &HashMap<String, String>) -> String {
        env.get("CLAUDE_DISALLOW_TOOLS")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "AskUserQuestion".to_string())
    }
}

#[async_trait::async_trait]
impl TurnHandlers for ClaudeCodeTurn {
    fn build_args(
        &self,
        message: &str,
        session_id: &str,
        env: &HashMap<String, String>,
    ) -> Vec<String> {
        if self.permission {
            // Bidirectional stream-json + permission tool. The user message is
            // delivered via stdin (build_initial_stdin), not argv.
            let mut args: Vec<String> = vec![
                ClaudeCodeExecutor.cli_name().to_string(),
                "--print".into(),
                "--output-format".into(),
                "stream-json".into(),
                "--input-format".into(),
                "stream-json".into(),
                "--verbose".into(),
                "--permission-prompt-tool".into(),
                "stdio".into(),
                "--permission-mode".into(),
                "default".into(),
            ];
            let disallow = Self::disallow_arg(env);
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
        } else {
            // Unattended: message via argv, skip permissions.
            let mut args: Vec<String> = vec![
                ClaudeCodeExecutor.cli_name().to_string(),
                "-p".into(),
                message.to_string(),
                "--output-format".into(),
                "stream-json".into(),
                "--dangerously-skip-permissions".into(),
            ];
            let disallow = Self::disallow_arg(env);
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

    // ----- permission channel (permission mode only) -----

    async fn build_initial_stdin(
        &mut self,
        message: &str,
        session_id: &str,
        attachments: &[crate::Attachment],
    ) -> Option<String> {
        if !self.permission {
            return None;
        }
        // Text-only turn → plain string content. Multimodal → array of
        // content blocks (text + image/document base64).
        let content: serde_json::Value = if attachments.is_empty() {
            serde_json::json!(message)
        } else {
            #[cfg(feature = "multimodal")]
            {
                let mut blocks: Vec<serde_json::Value> =
                    vec![serde_json::json!({ "type": "text", "text": message })];
                for att in attachments {
                    match att.kind.as_str() {
                        "image" => match download_image_as_base64(&att.url).await {
                            Ok((media_type, data)) => blocks.push(serde_json::json!({
                                "type": "image",
                                "source": { "type": "base64", "media_type": media_type, "data": data }
                            })),
                            Err(e) => {
                                blocks.push(serde_json::json!({
                                    "type": "text",
                                    "text": format!("[image download failed: {e}]")
                                }));
                            }
                        },
                        "file" => match download_document_as_base64(&att.url).await {
                            Ok((media_type, data)) => blocks.push(serde_json::json!({
                                "type": "document",
                                "source": { "type": "base64", "media_type": media_type, "data": data }
                            })),
                            Err(e) => {
                                blocks.push(serde_json::json!({
                                    "type": "text",
                                    "text": format!("[document download failed: {e}]")
                                }));
                            }
                        },
                        other => {
                            blocks.push(serde_json::json!({
                                "type": "text",
                                "text": format!("[unsupported attachment kind `{other}`: only image and file accepted]")
                            }));
                        }
                    }
                }
                serde_json::json!(blocks)
            }
            #[cfg(not(feature = "multimodal"))]
            {
                // multimodal feature disabled — forward urls as text so the
                // turn still completes rather than dropping the message.
                let mut blocks: Vec<serde_json::Value> =
                    vec![serde_json::json!({ "type": "text", "text": message })];
                for att in attachments {
                    blocks.push(serde_json::json!({
                        "type": "text",
                        "text": format!("[attachment: {} {}]", att.kind, att.url)
                    }));
                }
                serde_json::json!(blocks)
            }
        };
        let user_message = serde_json::json!({
            "type": "user",
            "message": { "role": "user", "content": content },
            "parent_tool_use_id": serde_json::Value::Null,
            "session_id": session_id,
        });
        serde_json::to_string(&user_message).ok()
    }

    fn permission_request_from_event(
        &mut self,
        event: &serde_json::Value,
    ) -> Option<(crate::PermissionRequest, serde_json::Value)> {
        let etype = event.get("type")?.as_str()?;
        if etype != "control_request" {
            return None;
        }
        let request = event.get("request")?;
        if request.get("subtype").and_then(|s| s.as_str()) != Some("can_use_tool") {
            return None;
        }
        let request_id = event.get("request_id")?.as_str()?.trim();
        if request_id.is_empty() {
            return None;
        }
        let tool_name = request
            .get("tool_name")
            .and_then(|t| t.as_str())
            .or_else(|| request.get("display_name").and_then(|t| t.as_str()))
            .unwrap_or("tool");
        let input = request.get("input").cloned().unwrap_or_else(|| serde_json::json!({}));
        let mut req = crate::PermissionRequest {
            request_id: request_id.to_string(),
            tool_name: tool_name.to_string(),
            input,
            description: None,
            tool_use_id: None,
            session_id: None,
        };
        if let Some(desc) = request.get("description").and_then(|d| d.as_str()) {
            if !desc.is_empty() {
                req.description = Some(desc.to_string());
            }
        }
        if let Some(tuid) = request.get("tool_use_id").and_then(|d| d.as_str()) {
            if !tuid.is_empty() {
                req.tool_use_id = Some(tuid.to_string());
            }
        }
        Some((req, event.clone()))
    }

    fn write_permission_response(
        &self,
        decision: &crate::PermissionDecision,
        original_event: &serde_json::Value,
    ) -> Option<String> {
        if !self.permission {
            return None;
        }
        let request = original_event.get("request")?;
        let request_id = original_event
            .get("request_id")
            .and_then(|r| r.as_str())
            .unwrap_or("");
        let original_input = request.get("input").cloned().unwrap_or_else(|| serde_json::json!({}));
        let response = match decision {
            crate::PermissionDecision::Allow { updated_input } => {
                let updated = updated_input.clone().unwrap_or(original_input);
                serde_json::json!({
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {
                            "behavior": "allow",
                            "updatedInput": updated,
                        },
                    },
                })
            }
            crate::PermissionDecision::Deny { message } => {
                serde_json::json!({
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {
                            "behavior": "deny",
                            "message": message,
                        },
                    },
                })
            }
        };
        serde_json::to_string(&response).ok()
    }
}

// ---------------------------------------------------------------------------
// codebuddy
// ---------------------------------------------------------------------------

/// Tencent CodeBuddy Code CLI (`codebuddy -p --output-format stream-json`).
/// NDJSON, stateless. Schema-compatible with claude-code.
pub struct CodebuddyExecutor;

impl Executor for CodebuddyExecutor {
    fn cli_name(&self) -> &str {
        "codebuddy"
    }
    fn install_hint(&self) -> &str {
        "See your internal CodeBuddy installation docs."
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(CodebuddyTurn)
    }
}

struct CodebuddyTurn;

#[async_trait::async_trait]
impl TurnHandlers for CodebuddyTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            CodebuddyExecutor.cli_name().to_string(),
            "-p".into(),
            message.to_string(),
            "--output-format".into(),
            "stream-json".into(),
            "--dangerously-skip-permissions".into(),
        ];
        let disallow = env
            .get("CODEBUDDY_DISALLOW_TOOLS")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "AskUserQuestion".to_string());
        if !disallow.is_empty() {
            args.push("--disallowedTools".into());
            args.push(disallow);
        }
        if let Some(model) = env.get("CODEBUDDY_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        if !session_id.is_empty() {
            args.push("-r".into());
            args.push(session_id.to_string());
        }
        args
    }

    fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult> {
        let etype = event.get("type")?.as_str()?;
        match etype {
            "assistant" => {
                let text = extract_assistant_text(&event)?;
                if text.is_empty() {
                    None
                } else {
                    Some(ParseResult::partial(text))
                }
            }
            "result" => {
                let session_id = event.get("session_id").and_then(|v| v.as_str()).map(String::from);
                if event.get("is_error").and_then(|v| v.as_bool()) == Some(true) {
                    let msg = event.get("result").and_then(|v| v.as_str()).unwrap_or("codebuddy reported an error");
                    let mut r = ParseResult::error(msg);
                    r.session_id = session_id;
                    return Some(r);
                }
                let text = event.get("result").and_then(|v| v.as_str()).unwrap_or("");
                Some(ParseResult {
                    final_text: Some(if text.is_empty() { None } else { Some(text.to_string()) }),
                    session_id,
                    ..Default::default()
                })
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// cursor
// ---------------------------------------------------------------------------

/// Cursor Agent CLI (`agent -p --output-format stream-json`). NDJSON,
/// stateful: cursor emits a duplicate full-text assistant event at the end of
/// a streamed turn, so parseEvent tracks accumulated text to suppress it.
pub struct CursorExecutor;

impl Executor for CursorExecutor {
    fn cli_name(&self) -> &str {
        "agent"
    }
    fn install_hint(&self) -> &str {
        "Install: brew install cursor-agent  (then run `agent login`)"
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(CursorTurn { accumulated: Vec::new() })
    }
}

struct CursorTurn {
    accumulated: Vec<String>,
}

#[async_trait::async_trait]
impl TurnHandlers for CursorTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            CursorExecutor.cli_name().to_string(),
            "-p".into(),
            message.to_string(),
            "--output-format".into(),
            "stream-json".into(),
            "--stream-partial-output".into(),
        ];
        let force = env.get("CURSOR_FORCE").map(|s| s.as_str()).unwrap_or("1");
        if force == "1" {
            args.push("--yolo".into());
        }
        if let Some(model) = env.get("CURSOR_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
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
                let sid = event.get("session_id")?.as_str()?;
                if sid.is_empty() {
                    None
                } else {
                    Some(ParseResult::session(sid))
                }
            }
            "assistant" => {
                let text = extract_assistant_text(&event)?;
                if text.is_empty() {
                    return None;
                }
                // Suppress the duplicate full-text event cursor emits at turn end.
                if text == self.accumulated.join("") {
                    return None;
                }
                self.accumulated.push(text.clone());
                Some(ParseResult::partial(text))
            }
            "result" => {
                let session_id = event.get("session_id").and_then(|v| v.as_str()).map(String::from);
                let is_err = event.get("is_error").and_then(|v| v.as_bool()) == Some(true)
                    || event.get("subtype").and_then(|v| v.as_str()) == Some("error");
                if is_err {
                    let msg = event.get("result").and_then(|v| v.as_str()).unwrap_or("cursor agent reported an error");
                    let mut r = ParseResult::error(msg);
                    r.session_id = session_id;
                    return Some(r);
                }
                let text = event.get("result").and_then(|v| v.as_str()).unwrap_or("");
                Some(ParseResult {
                    final_text: Some(if text.is_empty() { None } else { Some(text.to_string()) }),
                    session_id,
                    ..Default::default()
                })
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// gemini-cli
// ---------------------------------------------------------------------------

/// Google Gemini CLI (`gemini -p --output-format stream-json`). NDJSON,
/// stateless.
pub struct GeminiCliExecutor;

impl Executor for GeminiCliExecutor {
    fn cli_name(&self) -> &str {
        "gemini"
    }
    fn install_hint(&self) -> &str {
        "Install: npm install -g @google/gemini-cli"
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(GeminiCliTurn)
    }
}

struct GeminiCliTurn;

#[async_trait::async_trait]
impl TurnHandlers for GeminiCliTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            GeminiCliExecutor.cli_name().to_string(),
            "-p".into(),
            message.to_string(),
            "--output-format".into(),
            "stream-json".into(),
            "--yolo".into(),
        ];
        if env.get("GEMINI_SANDBOX").map(|s| s.trim().to_lowercase()).as_deref() == Some("false") {
            args.push("--sandbox".into());
            args.push("false".into());
        }
        if let Some(model) = env.get("GEMINI_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
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
            "init" => {
                let sid = event.get("session_id")?.as_str()?;
                if sid.is_empty() {
                    None
                } else {
                    Some(ParseResult::session(sid))
                }
            }
            "message" => {
                if event.get("role").and_then(|v| v.as_str()) != Some("assistant") {
                    return None;
                }
                let text = event.get("content").and_then(|v| v.as_str()).unwrap_or("");
                if text.is_empty() {
                    return None;
                }
                if event.get("delta").and_then(|v| v.as_bool()) == Some(true) {
                    Some(ParseResult::partial(text))
                } else {
                    Some(ParseResult::final_text(text))
                }
            }
            "error" => {
                if event.get("severity").and_then(|v| v.as_str()) == Some("error") {
                    let msg = event.get("message").and_then(|v| v.as_str()).unwrap_or("gemini reported an error");
                    Some(ParseResult::error(msg))
                } else {
                    None
                }
            }
            "result" if event.get("status").and_then(|v| v.as_str()) == Some("error") => {
                let msg = event
                    .get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("gemini turn failed");
                Some(ParseResult::error(msg))
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// grok-build
// ---------------------------------------------------------------------------

/// xAI Grok Build (`grok -p --output-format streaming-json`). NDJSON,
/// stateful: coalesces token-sized text into Claude-like blocks; keeps the
/// full body for streaming:false.
pub struct GrokBuildExecutor;

const GROK_SOFT_CHARS: usize = 40;
const GROK_HARD_CHARS: usize = 80;

fn grok_should_flush(buf: &str) -> bool {
    if buf.is_empty() {
        return false;
    }
    // Count Unicode scalars (matches JS/Python string length for CJK).
    let n = buf.chars().count();
    if n >= GROK_HARD_CHARS {
        return true;
    }
    let last = buf.chars().last().unwrap();
    if last == '\n' {
        return true;
    }
    if "。！？；.!?;".contains(last) && n >= GROK_SOFT_CHARS {
        return true;
    }
    false
}

impl Executor for GrokBuildExecutor {
    fn cli_name(&self) -> &str {
        "grok"
    }
    fn install_hint(&self) -> &str {
        "Install: curl -fsSL https://x.ai/cli/install.sh | bash"
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(GrokBuildTurn {
            full: Vec::new(),
            pending: String::new(),
        })
    }
}

struct GrokBuildTurn {
    full: Vec<String>,
    pending: String,
}

impl GrokBuildTurn {
    fn flush_pending(&mut self) -> Option<String> {
        if self.pending.is_empty() {
            return None;
        }
        Some(std::mem::take(&mut self.pending))
    }
}

#[async_trait::async_trait]
impl TurnHandlers for GrokBuildTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            GrokBuildExecutor.cli_name().to_string(),
            "-p".into(),
            message.to_string(),
            "--output-format".into(),
            "streaming-json".into(),
            "--always-approve".into(),
            "--no-auto-update".into(),
        ];
        if let Some(model) = env.get("GROK_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("-m".into());
            args.push(model.to_string());
        }
        if !session_id.is_empty() {
            args.push("-r".into());
            args.push(session_id.to_string());
        }
        args
    }

    fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult> {
        let etype = event.get("type")?.as_str()?;
        match etype {
            "text" => {
                let data = event.get("data").and_then(|v| v.as_str()).unwrap_or("");
                if data.is_empty() {
                    return None;
                }
                self.full.push(data.to_string());
                self.pending.push_str(data);
                if grok_should_flush(&self.pending) {
                    let chunk = self.flush_pending()?;
                    Some(ParseResult::partial(chunk))
                } else {
                    None
                }
            }
            "thought" => None,
            "end" => {
                let session_id = event
                    .get("sessionId")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .map(String::from);
                let leftover = self.flush_pending();
                Some(ParseResult {
                    partial_text: leftover,
                    final_text: Some(Some(self.full.join(""))),
                    session_id,
                    ..Default::default()
                })
            }
            "error" => {
                let session_id = event
                    .get("sessionId")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .map(String::from);
                self.pending.clear();
                let msg = event
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("grok reported an error");
                let mut r = ParseResult::error(msg);
                r.session_id = session_id;
                Some(r)
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// kimi-code
// ---------------------------------------------------------------------------

/// Moonshot Kimi CLI (`kimi --print --output-format=stream-json`). NDJSON,
/// stateful: kimi always generates/reuses a session id embedded in CLI args,
/// shared between build_args and parse_event.
pub struct KimiCodeExecutor;

impl Executor for KimiCodeExecutor {
    fn cli_name(&self) -> &str {
        "kimi"
    }
    fn install_hint(&self) -> &str {
        "See https://moonshotai.github.io/kimi-cli for installation"
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(KimiCodeTurn {
            session_id: std::cell::Cell::new(None),
        })
    }
}

struct KimiCodeTurn {
    /// Set in build_args (the id is minted there per Node), read in
    /// parse_event / get_session_id. Cell gives interior mutability under
    /// the `&self` build_args signature.
    session_id: std::cell::Cell<Option<String>>,
}

#[async_trait::async_trait]
impl TurnHandlers for KimiCodeTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let id = if session_id.is_empty() {
            uuid::Uuid::new_v4().to_string()
        } else {
            session_id.to_string()
        };
        self.session_id.set(Some(id.clone()));
        let mut args: Vec<String> = vec![
            KimiCodeExecutor.cli_name().to_string(),
            "--print".into(),
            "-p".into(),
            message.to_string(),
            "--output-format=stream-json".into(),
            "--session".into(),
            id,
        ];
        if let Some(model) = env.get("KIMI_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        args
    }

    fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult> {
        if event.get("role").and_then(|v| v.as_str()) != Some("assistant") {
            return None;
        }
        let content = event.get("content").and_then(|v| v.as_str()).unwrap_or("");
        if content.is_empty() {
            return None;
        }
        Some(ParseResult {
            partial_text: Some(content.to_string()),
            final_text: Some(Some(content.to_string())),
            session_id: self.session_id.take(),
            ..Default::default()
        })
    }
}

// ---------------------------------------------------------------------------
// opencode
// ---------------------------------------------------------------------------

/// opencode CLI (`opencode run --format json`). NDJSON, stateless.
pub struct OpencodeExecutor;

impl Executor for OpencodeExecutor {
    fn cli_name(&self) -> &str {
        "opencode"
    }
    fn install_hint(&self) -> &str {
        "Install: npm install -g opencode-ai  (or: curl -fsSL https://opencode.ai/install | bash)"
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(OpencodeTurn)
    }
}

struct OpencodeTurn;

#[async_trait::async_trait]
impl TurnHandlers for OpencodeTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            OpencodeExecutor.cli_name().to_string(),
            "run".into(),
            message.to_string(),
            "--auto".into(),
            "--format".into(),
            "json".into(),
        ];
        if !session_id.is_empty() {
            args.push("--session".into());
            args.push(session_id.to_string());
        }
        if let Some(model) = env.get("OPENCODE_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        args
    }

    fn parse_event(&mut self, event: serde_json::Value) -> Option<ParseResult> {
        let etype = event.get("type")?.as_str()?;
        let session_id = event.get("sessionID").and_then(|v| v.as_str()).map(String::from);
        match etype {
            "text" => {
                let text = event.get("part").and_then(|p| p.get("text")).and_then(|v| v.as_str()).unwrap_or("");
                if !text.is_empty() {
                    let mut r = ParseResult::partial(text);
                    r.session_id = session_id;
                    Some(r)
                } else if let Some(sid) = session_id {
                    Some(ParseResult { session_id: Some(sid), ..Default::default() })
                } else {
                    None
                }
            }
            "step_start" | "step_finish" | "tool_use" => {
                session_id.map(|sid| ParseResult { session_id: Some(sid), ..Default::default() })
            }
            "error" => {
                let msg = event
                    .get("part")
                    .and_then(|p| p.get("message"))
                    .and_then(|v| v.as_str())
                    .or_else(|| event.get("error").and_then(|e| e.get("message")).and_then(|v| v.as_str()))
                    .unwrap_or("opencode reported an error");
                let mut r = ParseResult::error(msg);
                r.session_id = session_id;
                Some(r)
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// qwen-code
// ---------------------------------------------------------------------------

/// Alibaba Qwen Code CLI (`qwen -p --output-format stream-json`). NDJSON,
/// stateless. Schema-compatible with gemini-cli.
pub struct QwenCodeExecutor;

impl Executor for QwenCodeExecutor {
    fn cli_name(&self) -> &str {
        "qwen"
    }
    fn install_hint(&self) -> &str {
        "Install: npm install -g @qwen-code/qwen-code"
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(QwenCodeTurn)
    }
}

struct QwenCodeTurn;

#[async_trait::async_trait]
impl TurnHandlers for QwenCodeTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            QwenCodeExecutor.cli_name().to_string(),
            "-p".into(),
            message.to_string(),
            "--output-format".into(),
            "stream-json".into(),
            "--yolo".into(),
        ];
        if env.get("QWEN_SANDBOX").map(|s| s.trim().to_lowercase()).as_deref() == Some("false") {
            args.push("--sandbox".into());
            args.push("false".into());
        }
        if let Some(model) = env.get("QWEN_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
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
            "init" => {
                let sid = event.get("session_id")?.as_str()?;
                if sid.is_empty() {
                    None
                } else {
                    Some(ParseResult::session(sid))
                }
            }
            "message" => {
                if event.get("role").and_then(|v| v.as_str()) != Some("assistant") {
                    return None;
                }
                let text = event.get("content").and_then(|v| v.as_str()).unwrap_or("");
                if text.is_empty() {
                    return None;
                }
                if event.get("delta").and_then(|v| v.as_bool()) == Some(true) {
                    Some(ParseResult::partial(text))
                } else {
                    Some(ParseResult::final_text(text))
                }
            }
            "error" => {
                if event.get("severity").and_then(|v| v.as_str()) == Some("error") {
                    let msg = event.get("message").and_then(|v| v.as_str()).unwrap_or("qwen reported an error");
                    Some(ParseResult::error(msg))
                } else {
                    None
                }
            }
            "result" if event.get("status").and_then(|v| v.as_str()) == Some("error") => {
                let msg = event
                    .get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("qwen turn failed");
                Some(ParseResult::error(msg))
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// Plain-text executors
// ---------------------------------------------------------------------------

/// Google Antigravity `agy` CLI. Plain text + stateful: agy supports
/// `--conversation <id>` for resume, so buildArgs mints/reuses the id and
/// get_session_id surfaces it for RunResult.
pub struct AgyExecutor;

impl Executor for AgyExecutor {
    fn cli_name(&self) -> &str {
        "agy"
    }
    fn install_hint(&self) -> &str {
        "See the agy project for installation instructions."
    }
    fn plain(&self) -> bool {
        true
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(AgyTurn {
            session_id: std::cell::Cell::new(None),
        })
    }
}

struct AgyTurn {
    /// Minted/reused in build_args, surfaced via get_session_id so the runner
    /// can populate RunResult.session_id for plain executors.
    session_id: std::cell::Cell<Option<String>>,
}

#[async_trait::async_trait]
impl TurnHandlers for AgyTurn {
    fn build_args(&self, message: &str, session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let id = if session_id.is_empty() {
            uuid::Uuid::new_v4().to_string()
        } else {
            session_id.to_string()
        };
        self.session_id.set(Some(id.clone()));
        let mut args: Vec<String> = vec![
            AgyExecutor.cli_name().to_string(),
            "--print".into(),
            message.to_string(),
            "--conversation".into(),
            id,
        ];
        if env.get("AGY_DANGEROUSLY_SKIP_PERMISSIONS").map(|s| s.as_str()).unwrap_or("1") == "1" {
            args.push("--dangerously-skip-permissions".into());
        }
        if let Some(model) = env.get("AGY_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        args
    }

    fn parse_event(&mut self, _event: serde_json::Value) -> Option<ParseResult> {
        None
    }

    fn get_session_id(&self) -> Option<String> {
        self.session_id.take()
    }
}

/// aider CLI. Plain text, stateless.
pub struct AiderExecutor;

impl Executor for AiderExecutor {
    fn cli_name(&self) -> &str {
        "aider"
    }
    fn install_hint(&self) -> &str {
        "Install: pip install aider-chat"
    }
    fn plain(&self) -> bool {
        true
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(AiderTurn)
    }
}

struct AiderTurn;

#[async_trait::async_trait]
impl TurnHandlers for AiderTurn {
    fn build_args(&self, message: &str, _session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            AiderExecutor.cli_name().to_string(),
            "--message".into(),
            message.to_string(),
            "--yes-always".into(),
            "--no-show-release-notes".into(),
            "--no-stream".into(),
        ];
        if let Some(model) = env.get("AIDER_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        args
    }

    fn parse_event(&mut self, _event: serde_json::Value) -> Option<ParseResult> {
        None
    }
}

/// DeepSeek CLI. Plain text, stateless.
pub struct DeepseekExecutor;

impl Executor for DeepseekExecutor {
    fn cli_name(&self) -> &str {
        "deepseek"
    }
    fn install_hint(&self) -> &str {
        "Install from https://deepseek.com/downloads or: brew install deepseek"
    }
    fn plain(&self) -> bool {
        true
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(DeepseekTurn)
    }
}

struct DeepseekTurn;

#[async_trait::async_trait]
impl TurnHandlers for DeepseekTurn {
    fn build_args(&self, message: &str, _session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            DeepseekExecutor.cli_name().to_string(),
            "exec".into(),
            "-p".into(),
            message.to_string(),
        ];
        if let Some(model) = env.get("DEEPSEEK_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        args
    }

    fn parse_event(&mut self, _event: serde_json::Value) -> Option<ParseResult> {
        None
    }
}

/// earendil-works `pi` coding agent. Plain text, stateless.
pub struct PiExecutor;

impl Executor for PiExecutor {
    fn cli_name(&self) -> &str {
        "pi"
    }
    fn install_hint(&self) -> &str {
        "Install: npm install -g @earendil-works/pi-coding-agent"
    }
    fn plain(&self) -> bool {
        true
    }
    fn make_turn(&self, _ctx: &TurnCtx) -> Box<dyn TurnHandlers> {
        Box::new(PiTurn)
    }
}

struct PiTurn;

#[async_trait::async_trait]
impl TurnHandlers for PiTurn {
    fn build_args(&self, message: &str, _session_id: &str, env: &HashMap<String, String>) -> Vec<String> {
        let mut args: Vec<String> = vec![
            PiExecutor.cli_name().to_string(),
            "-p".into(),
            message.to_string(),
            "--approve".into(),
        ];
        if env.get("PI_NO_EXTENSIONS").map(|s| s.as_str()) != Some("0") {
            args.push("--no-extensions".into());
        }
        if let Some(model) = env.get("PI_MODEL").map(|s| s.trim()).filter(|s| !s.is_empty()) {
            args.push("--model".into());
            args.push(model.to_string());
        }
        args
    }

    fn parse_event(&mut self, _event: serde_json::Value) -> Option<ParseResult> {
        None
    }
}

// ---------------------------------------------------------------------------
// shared helpers
// ---------------------------------------------------------------------------

/// Anthropic Messages API limits for multimodal content blocks.
#[cfg(feature = "multimodal")]
const ANTHROPIC_MAX_IMAGE_BYTES: usize = 5 * 1024 * 1024;
#[cfg(feature = "multimodal")]
const ANTHROPIC_MAX_DOCUMENT_BYTES: usize = 32 * 1024 * 1024;

/// Download an image and return `(media_type, base64_data)`. media_type is
/// taken from the response Content-Type when it starts with `image/`,
/// otherwise defaults to `image/jpeg`. Fails fast when the body exceeds the
/// Anthropic 5 MB image limit during streaming so we don't keep downloading.
#[cfg(feature = "multimodal")]
async fn download_image_as_base64(url: &str) -> Result<(String, String), String> {
    use base64::Engine;
    use futures_util::StreamExt;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("build reqwest client: {e}"))?;
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("download image from {url}: {e}"))?;
    if !response.status().is_success() {
        return Err(format!("image download failed: HTTP {} for {url}", response.status()));
    }
    let media_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.split(';').next().unwrap_or(s).trim().to_string())
        .filter(|s| s.starts_with("image/"))
        .unwrap_or_else(|| "image/jpeg".to_string());

    let mut buf = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("read image chunk: {e}"))?;
        if buf.len() + chunk.len() > ANTHROPIC_MAX_IMAGE_BYTES {
            return Err(format!(
                "image too large: exceeds Anthropic limit ({ANTHROPIC_MAX_IMAGE_BYTES} bytes)"
            ));
        }
        buf.extend_from_slice(&chunk);
    }
    if buf.is_empty() {
        return Err(format!("image download returned empty body for {url}"));
    }
    Ok((media_type, base64::engine::general_purpose::STANDARD.encode(&buf)))
}

/// Download a document and return `(media_type, base64_data)`. Only
/// `application/pdf` and `text/plain` are accepted (Anthropic document block
/// constraint); any other Content-Type fails fast. Limit: 32 MB.
#[cfg(feature = "multimodal")]
async fn download_document_as_base64(url: &str) -> Result<(String, String), String> {
    use base64::Engine;
    use futures_util::StreamExt;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .map_err(|e| format!("build reqwest client: {e}"))?;
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("download document from {url}: {e}"))?;
    if !response.status().is_success() {
        return Err(format!("document download failed: HTTP {} for {url}", response.status()));
    }
    let raw_media_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.split(';').next().unwrap_or(s).trim().to_string())
        .unwrap_or_default();
    let media_type = match raw_media_type.as_str() {
        "application/pdf" => "application/pdf".to_string(),
        "text/plain" => "text/plain".to_string(),
        other => {
            return Err(format!(
                "unsupported document media_type: {other:?} (only application/pdf and text/plain accepted); url: {url}"
            ));
        }
    };

    let mut buf = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("read document chunk: {e}"))?;
        if buf.len() + chunk.len() > ANTHROPIC_MAX_DOCUMENT_BYTES {
            return Err(format!(
                "document too large: exceeds Anthropic limit ({ANTHROPIC_MAX_DOCUMENT_BYTES} bytes)"
            ));
        }
        buf.extend_from_slice(&chunk);
    }
    if buf.is_empty() {
        return Err(format!("document download returned empty body for {url}"));
    }
    Ok((media_type, base64::engine::general_purpose::STANDARD.encode(&buf)))
}

/// Extract concatenated text from a claude-code-style `assistant` event's
/// `message.content` array (filtering for `type == "text"` blocks).
fn extract_assistant_text(event: &serde_json::Value) -> Option<String> {
    let content = event.get("message").and_then(|m| m.get("content"))?;
    let arr = content.as_array()?;
    let text: String = arr
        .iter()
        .filter(|b| b.get("type").and_then(|v| v.as_str()) == Some("text"))
        .filter_map(|b| b.get("text").and_then(|v| v.as_str()))
        .collect();
    Some(text)
}

/// Helper for executors that need to substitute placeholders into pre-built
/// argv. Exposed for custom executors; built-in executors do not use it.
/// Returns a [`PlaceholderError`](crate::env::PlaceholderError) when a value
/// contains an unsafe byte (SEC-003).
pub fn substitute_argv(
    argv: &[String],
    ctx: &SubstCtx,
) -> Result<Vec<String>, crate::env::PlaceholderError> {
    argv.iter()
        .map(|a| crate::env::substitute(a, ctx))
        .collect()
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
        let h = ex.make_turn(&TurnCtx::default());
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
        let h = ex.make_turn(&TurnCtx::default());
        let env = HashMap::new();
        let args = h.build_args("hi", "thread-1", &env);
        assert!(args.iter().any(|a| a == "resume"));
        assert!(args.iter().any(|a| a == "thread-1"));
    }

    #[test]
    fn codex_parse_thread_started() {
        let mut h = CodexExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({
            "type": "thread.started",
            "thread_id": "t1"
        }));
        assert_eq!(r.unwrap().session_id.as_deref(), Some("t1"));
    }

    #[test]
    fn codex_parse_agent_message_partial() {
        let mut h = CodexExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({
            "type": "item.completed",
            "item": { "type": "agent_message", "text": "hello" }
        }));
        assert_eq!(r.unwrap().partial_text.as_deref(), Some("hello"));
    }

    #[test]
    fn codex_parse_unknown_event_none() {
        let mut h = CodexExecutor.make_turn(&TurnCtx::default());
        assert!(h.parse_event(json!({"type": "noise"})).is_none());
    }

    #[test]
    fn claude_code_build_args_with_model() {
        let ex = ClaudeCodeExecutor;
        let h = ex.make_turn(&TurnCtx::default());
        let mut env = HashMap::new();
        env.insert("CLAUDE_MODEL".to_string(), "claude-sonnet-4-6".to_string());
        let args = h.build_args("hi", "", &env);
        assert_eq!(args[0], "claude");
        assert!(args.iter().any(|a| a == "claude-sonnet-4-6"));
    }

    #[test]
    fn claude_code_parse_init_session() {
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({
            "type": "system",
            "subtype": "init",
            "session_id": "cli-sess-1"
        }));
        assert_eq!(r.unwrap().session_id.as_deref(), Some("cli-sess-1"));
    }

    #[test]
    fn claude_code_parse_result_final_text() {
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx::default());
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
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({
            "type": "result",
            "is_error": true,
            "result": "rate limited"
        }));
        assert_eq!(r.unwrap().error.as_deref(), Some("rate limited"));
    }

    // ----- claude-code permission mode -----

    #[test]
    fn claude_code_permission_mode_uses_bidirectional_argv() {
        let ctx = TurnCtx { permission: true };
        let h = ClaudeCodeExecutor.make_turn(&ctx);
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        // No -p <message> (message goes via stdin), no --dangerously-skip.
        assert!(!args.iter().any(|a| a == "--dangerously-skip-permissions"));
        assert!(args.iter().any(|a| a == "--permission-prompt-tool"));
        assert!(args.iter().any(|a| a == "stdio"));
        assert!(args.iter().any(|a| a == "--input-format"));
        assert!(args.iter().any(|a| a == "stream-json"));
    }

    #[test]
    fn claude_code_unattended_mode_omits_permission_flags() {
        let h = ClaudeCodeExecutor.make_turn(&TurnCtx::default());
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        assert!(args.iter().any(|a| a == "--dangerously-skip-permissions"));
        assert!(!args.iter().any(|a| a == "--permission-prompt-tool"));
    }

    #[tokio::test]
    async fn claude_code_build_initial_stdin_only_in_permission_mode() {
        // Unattended: no initial stdin.
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx::default());
        assert_eq!(h.build_initial_stdin("hi", "", &[]).await, None);

        // Permission: SDKUserMessage JSON line.
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx { permission: true });
        let line = h.build_initial_stdin("hi", "sess-1", &[]).await.unwrap();
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["type"], "user");
        assert_eq!(v["message"]["role"], "user");
        assert_eq!(v["message"]["content"], "hi");
        assert_eq!(v["session_id"], "sess-1");
    }

    #[test]
    fn claude_code_control_request_translates_to_permission_request() {
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx { permission: true });
        let event = json!({
            "type": "control_request",
            "request_id": "req-42",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "rm -rf /"},
                "description": "delete everything"
            }
        });
        let (req, original) = h.permission_request_from_event(&event).unwrap();
        assert_eq!(req.request_id, "req-42");
        assert_eq!(req.tool_name, "Bash");
        assert_eq!(req.input["command"], "rm -rf /");
        assert_eq!(req.description.as_deref(), Some("delete everything"));
        // original event is stashed for the response writer.
        assert_eq!(original["request_id"], "req-42");
    }

    #[test]
    fn claude_code_non_can_use_tool_control_request_ignored() {
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx { permission: true });
        let event = json!({
            "type": "control_request",
            "request_id": "req-1",
            "request": {"subtype": "other"}
        });
        assert!(h.permission_request_from_event(&event).is_none());
    }

    #[test]
    fn claude_code_write_allow_response_carries_updated_input() {
        let h = ClaudeCodeExecutor.make_turn(&TurnCtx { permission: true });
        let original = json!({
            "type": "control_request",
            "request_id": "req-7",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}}
        });
        let decision = crate::PermissionDecision::Allow { updated_input: None };
        let line = h.write_permission_response(&decision, &original).unwrap();
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["type"], "control_response");
        assert_eq!(v["response"]["request_id"], "req-7");
        assert_eq!(v["response"]["response"]["behavior"], "allow");
        // updated_input defaults to original input when none provided.
        assert_eq!(v["response"]["response"]["updatedInput"]["command"], "ls");
    }

    #[test]
    fn claude_code_write_deny_response_carries_message() {
        let h = ClaudeCodeExecutor.make_turn(&TurnCtx { permission: true });
        let original = json!({
            "type": "control_request",
            "request_id": "req-9",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}}
        });
        let decision = crate::PermissionDecision::deny("not allowed");
        let line = h.write_permission_response(&decision, &original).unwrap();
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["response"]["response"]["behavior"], "deny");
        assert_eq!(v["response"]["response"]["message"], "not allowed");
    }

    #[tokio::test]
    async fn claude_code_permission_methods_noop_in_unattended_mode() {
        let mut h = ClaudeCodeExecutor.make_turn(&TurnCtx::default());
        assert_eq!(h.build_initial_stdin("hi", "", &[]).await, None);
        let event = json!({"type": "control_request", "request_id": "r", "request": {"subtype": "can_use_tool"}});
        // request detection is independent of mode (it inspects the event),
        // but write_permission_response returns None in unattended mode.
        let decision = crate::PermissionDecision::allow();
        assert_eq!(h.write_permission_response(&decision, &event), None);
    }

    // ----- registry completeness -----

    #[test]
    fn all_thirteen_executors_registered() {
        let names = executor_names();
        for expected in [
            "codex",
            "claude-code",
            "codebuddy",
            "cursor",
            "gemini-cli",
            "grok-build",
            "kimi-code",
            "opencode",
            "qwen-code",
            "agy",
            "aider",
            "deepseek",
            "pi",
        ] {
            assert!(
                names.contains(&expected.to_string()),
                "executor `{expected}` not registered; have: {names:?}"
            );
        }
        assert_eq!(names.len(), 13, "expected exactly 13 executors, got {}", names.len());
    }

    #[test]
    fn lookup_returns_all_thirteen() {
        for name in ["codex", "claude-code", "codebuddy", "cursor", "gemini-cli", "grok-build",
            "kimi-code", "opencode", "qwen-code", "agy", "aider", "deepseek", "pi"]
        {
            assert!(lookup(name).is_some(), "lookup({name}) returned None");
        }
    }

    // ----- codebuddy -----

    #[test]
    fn codebuddy_build_args_basic() {
        let h = CodebuddyExecutor.make_turn(&TurnCtx::default());
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        assert_eq!(args[0], "codebuddy");
        assert!(args.iter().any(|a| a == "stream-json"));
        assert!(args.iter().any(|a| a == "--dangerously-skip-permissions"));
    }

    #[test]
    fn codebuddy_parse_assistant_partial() {
        let mut h = CodebuddyExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({
            "type": "assistant",
            "message": { "content": [{"type": "text", "text": "hello"}] }
        }));
        assert_eq!(r.unwrap().partial_text.as_deref(), Some("hello"));
    }

    #[test]
    fn codebuddy_parse_result_final() {
        let mut h = CodebuddyExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({
            "type": "result",
            "result": "done",
            "session_id": "s1"
        }));
        let r = r.unwrap();
        assert_eq!(r.final_text.unwrap().unwrap(), "done");
        assert_eq!(r.session_id.as_deref(), Some("s1"));
    }

    // ----- cursor (stateful: suppresses duplicate) -----

    #[test]
    fn cursor_suppresses_duplicate_full_text() {
        let mut h = CursorExecutor.make_turn(&TurnCtx::default());
        // First assistant event streams "Hi ".
        let r1 = h.parse_event(json!({
            "type": "assistant",
            "message": { "content": [{"type": "text", "text": "Hi "}] }
        }));
        assert_eq!(r1.unwrap().partial_text.as_deref(), Some("Hi "));
        // Final full-text event: "Hi there!" — not a duplicate, forwarded.
        let r2 = h.parse_event(json!({
            "type": "assistant",
            "message": { "content": [{"type": "text", "text": "Hi there!"}] }
        }));
        assert_eq!(r2.unwrap().partial_text.as_deref(), Some("Hi there!"));
        // If cursor re-emits text equal to the accumulated join, it's the
        // duplicate full-text event cursor emits at turn end → suppressed.
        // accumulated is ["Hi ", "Hi there!"] → join = "Hi Hi there!"
        let r3 = h.parse_event(json!({
            "type": "assistant",
            "message": { "content": [{"type": "text", "text": "Hi Hi there!"}] }
        }));
        assert!(r3.is_none(), "duplicate full-text event should be suppressed");
    }

    // ----- gemini-cli -----

    #[test]
    fn gemini_parse_delta_vs_final() {
        let mut h = GeminiCliExecutor.make_turn(&TurnCtx::default());
        let delta = h.parse_event(json!({
            "type": "message", "role": "assistant", "content": "chunk", "delta": true
        }));
        assert!(delta.unwrap().partial_text.is_some());

        let final_evt = h.parse_event(json!({
            "type": "message", "role": "assistant", "content": "full", "delta": false
        }));
        assert!(final_evt.unwrap().final_text.is_some());
    }

    // ----- grok-build -----

    #[test]
    fn grok_build_args_and_parse() {
        let mut h = GrokBuildExecutor.make_turn(&TurnCtx::default());
        let env = HashMap::from([("GROK_MODEL".to_string(), "grok-4.5".to_string())]);
        let args = h.build_args("hi", "sess-1", &env);
        assert_eq!(args[0], "grok");
        assert!(args.iter().any(|a| a == "streaming-json"));
        assert!(args.iter().any(|a| a == "--always-approve"));
        assert!(args.iter().any(|a| a == "-r"));
        assert!(args.iter().any(|a| a == "sess-1"));
        assert!(args.iter().any(|a| a == "-m"));
        assert!(args.iter().any(|a| a == "grok-4.5"));

        assert!(h.parse_event(json!({"type": "thought", "data": "x"})).is_none());
        // Short tokens buffer — no mid-stream partial until end.
        assert!(h.parse_event(json!({"type": "text", "data": "hello"})).is_none());
        assert!(h.parse_event(json!({"type": "text", "data": " world"})).is_none());
        let end = h.parse_event(json!({
            "type": "end",
            "sessionId": "sess-1",
            "stopReason": "EndTurn"
        })).unwrap();
        assert_eq!(end.session_id.as_deref(), Some("sess-1"));
        assert_eq!(end.partial_text.as_deref(), Some("hello world"));
        assert_eq!(end.final_text, Some(Some("hello world".to_string())));
    }

    #[test]
    fn grok_build_coalesces_on_hard_limit() {
        let mut h = GrokBuildExecutor.make_turn(&TurnCtx::default());
        // 80 'a's → hard flush.
        let chunk = "a".repeat(80);
        let r = h.parse_event(json!({"type": "text", "data": chunk}));
        assert_eq!(r.unwrap().partial_text.as_deref().map(|s| s.len()), Some(80));
    }

    // ----- kimi-code (stateful session id) -----

    #[test]
    fn kimi_mints_session_id_when_empty() {
        let h = KimiCodeExecutor.make_turn(&TurnCtx::default());
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        // --session <id> present, id is a uuid (36 chars)
        let session_idx = args.iter().position(|a| a == "--session").unwrap();
        let id = &args[session_idx + 1];
        assert_eq!(id.len(), 36);
    }

    #[test]
    fn kimi_reuses_inbound_session_id() {
        let h = KimiCodeExecutor.make_turn(&TurnCtx::default());
        let env = HashMap::new();
        let args = h.build_args("hi", "existing-id", &env);
        let session_idx = args.iter().position(|a| a == "--session").unwrap();
        assert_eq!(args[session_idx + 1], "existing-id");
    }

    // ----- opencode -----

    #[test]
    fn opencode_parse_text_partial() {
        let mut h = OpencodeExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({
            "type": "text",
            "sessionID": "oc-1",
            "part": { "text": "hello" }
        }));
        let r = r.unwrap();
        assert_eq!(r.partial_text.as_deref(), Some("hello"));
        assert_eq!(r.session_id.as_deref(), Some("oc-1"));
    }

    // ----- qwen-code -----

    #[test]
    fn qwen_parse_init_session() {
        let mut h = QwenCodeExecutor.make_turn(&TurnCtx::default());
        let r = h.parse_event(json!({"type": "init", "session_id": "q-1"}));
        assert_eq!(r.unwrap().session_id.as_deref(), Some("q-1"));
    }

    // ----- plain executors -----

    #[test]
    fn agy_plain_returns_session_id() {
        let h = AgyExecutor.make_turn(&TurnCtx::default());
        assert!(AgyExecutor.plain());
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        assert!(args.iter().any(|a| a == "--conversation"));
        // get_session_id surfaces the minted id after build_args.
        let sid = h.get_session_id();
        assert!(sid.is_some());
        assert_eq!(sid.unwrap().len(), 36);
    }

    #[test]
    fn agy_reuses_inbound_conversation_id() {
        let h = AgyExecutor.make_turn(&TurnCtx::default());
        let env = HashMap::new();
        let args = h.build_args("hi", "conv-42", &env);
        let conv_idx = args.iter().position(|a| a == "--conversation").unwrap();
        assert_eq!(args[conv_idx + 1], "conv-42");
        assert_eq!(h.get_session_id().as_deref(), Some("conv-42"));
    }

    #[test]
    fn aider_build_args_basic() {
        let h = AiderExecutor.make_turn(&TurnCtx::default());
        assert!(AiderExecutor.plain());
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        assert_eq!(args[0], "aider");
        assert!(args.iter().any(|a| a == "--yes-always"));
        assert!(args.iter().any(|a| a == "--no-stream"));
        // aider has no session continuity
        assert_eq!(h.get_session_id(), None);
    }

    #[test]
    fn deepseek_build_args_basic() {
        let h = DeepseekExecutor.make_turn(&TurnCtx::default());
        assert!(DeepseekExecutor.plain());
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        assert_eq!(args[0], "deepseek");
        assert!(args.iter().any(|a| a == "exec"));
        assert!(args.iter().any(|a| a == "-p"));
    }

    #[test]
    fn pi_build_args_basic() {
        let h = PiExecutor.make_turn(&TurnCtx::default());
        assert!(PiExecutor.plain());
        let env = HashMap::new();
        let args = h.build_args("hi", "", &env);
        assert_eq!(args[0], "pi");
        assert!(args.iter().any(|a| a == "--approve"));
        assert!(args.iter().any(|a| a == "--no-extensions"));
    }

    #[test]
    fn pi_respects_no_extensions_zero() {
        let h = PiExecutor.make_turn(&TurnCtx::default());
        let mut env = HashMap::new();
        env.insert("PI_NO_EXTENSIONS".to_string(), "0".to_string());
        let args = h.build_args("hi", "", &env);
        // PI_NO_EXTENSIONS=0 → do NOT add --no-extensions
        assert!(!args.iter().any(|a| a == "--no-extensions"));
    }
}
