//! AgentProc wire protocol 0.4 — NDJSON in both directions.
//!
//! - **stdin**: the bridge writes exactly one [`TurnObject`] line, then EOF
//!   (unless `permission: true`, in which case stdin stays open for
//!   [`PermissionResponse`] frames).
//! - **stdout**: the agent emits one JSON object per line, distinguished by a
//!   `type` field from a closed vocabulary: `partial` / `result` / `error` /
//!   `permission_request`. Unknown or malformed lines are logged and ignored.

use serde::{Deserialize, Serialize};

/// Wire-protocol version string carried in the turn object. Opaque and
/// non-comparable per the spec — agents MUST NOT order or range-check it.
pub const PROTOCOL_VERSION: &str = "0.4";

/// One element of the turn object's `attachments` array.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Attachment {
    pub kind: String,
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub filename: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub mime_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub size: Option<u64>,
}

/// The turn object the bridge writes to the agent's stdin as a single NDJSON
/// line before the process reads its first byte.
#[derive(Debug, Clone, Serialize)]
pub struct TurnObject {
    #[serde(rename = "type")]
    pub event_type: &'static str,
    pub message: String,
    pub session_id: String,
    pub from_user: String,
    pub protocol_version: &'static str,
    pub session_name: String,
    pub attachments: Vec<Attachment>,
    /// Included (true) only when the profile enables the permission channel.
    #[serde(skip_serializing_if = "is_false")]
    pub permission: bool,
}

impl TurnObject {
    /// Build a turn object. `permission` is emitted on the wire only when true.
    pub fn new(
        message: impl Into<String>,
        session_id: impl Into<String>,
        session_name: impl Into<String>,
        from_user: impl Into<String>,
        attachments: Vec<Attachment>,
        permission: bool,
    ) -> Self {
        Self {
            event_type: "turn",
            message: message.into(),
            session_id: session_id.into(),
            from_user: from_user.into(),
            protocol_version: PROTOCOL_VERSION,
            session_name: session_name.into(),
            attachments,
            permission,
        }
    }

    /// Serialize as a single NDJSON line (no trailing newline).
    pub fn to_ndjson(&self) -> serde_json::Result<String> {
        serde_json::to_string(self)
    }
}

/// The turn object as read by an agent from its stdin (deserialized).
///
/// Tolerant of missing optional fields (`session_name` defaults to `"default"`,
/// `attachments` to `[]`, `permission` to `false`).
#[derive(Debug, Clone, Deserialize, Default)]
pub struct TurnInput {
    #[serde(rename = "type", default)]
    pub event_type: Option<String>,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default = "default_session_name")]
    pub session_name: String,
    #[serde(default)]
    pub from_user: String,
    #[serde(default)]
    pub attachments: Vec<Attachment>,
    #[serde(default)]
    pub permission: bool,
    #[serde(default)]
    pub protocol_version: String,
}

fn default_session_name() -> String {
    "default".to_string()
}

impl TurnInput {
    /// Whether this turn carries any user content (text or attachments).
    pub fn has_content(&self) -> bool {
        !self.message.is_empty() || !self.attachments.is_empty()
    }
}

/// Read exactly one NDJSON line (the turn object) from any reader.
///
/// Returns `None` on EOF or malformed JSON. Agents that read from stdin can
/// pass `std::io::stdin()` here; tests pass a `&[u8]`.
pub fn read_turn<R: std::io::BufRead>(mut reader: R) -> Option<TurnInput> {
    let mut line = String::new();
    if reader.read_line(&mut line).ok()? == 0 {
        return None;
    }
    serde_json::from_str::<TurnInput>(line.trim()).ok()
}

/// Distinguish assistant output from reasoning/thinking text on `partial` events.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum PartialRole {
    #[default]
    Output,
    Thinking,
}

/// A tool-permission request emitted by the agent (only when `permission: true`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PermissionRequest {
    pub request_id: String,
    pub tool_name: String,
    #[serde(default)]
    pub input: serde_json::Value,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub tool_use_id: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
}

/// Deserialize a string-or-null field leniently: missing or non-string
/// values become `String::new()`. Mirrors the Node SDK's `.text || ""`
/// / `.message || ""` coercion so malformed-typed events are still classified
/// (as partial/result/error with empty body) rather than dropped.
fn lenient_string<'de, D: serde::Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    let opt: Option<serde_json::Value> = serde::Deserialize::deserialize(d)?;
    match opt {
        Some(serde_json::Value::String(s)) => Ok(s),
        Some(_) | None => Ok(String::new()),
    }
}

/// Deserialize an optional session_id, treating non-string values as None.
fn lenient_session_id<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<String>, D::Error> {
    let opt: Option<serde_json::Value> = serde::Deserialize::deserialize(d)?;
    Ok(opt.and_then(|v| v.as_str().map(|s| s.to_string())))
}

/// Deserialize an optional role string, treating non-string values as None.
fn lenient_role<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<PartialRole>, D::Error> {
    let opt: Option<serde_json::Value> = serde::Deserialize::deserialize(d)?;
    match opt.and_then(|v| v.as_str().map(String::from)) {
        Some(s) if s == "thinking" => Ok(Some(PartialRole::Thinking)),
        Some(s) if s == "output" => Ok(Some(PartialRole::Output)),
        _ => Ok(None),
    }
}

/// A parsed stdout event from the agent.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum AgentEvent {
    Partial {
        #[serde(default, deserialize_with = "lenient_string")]
        text: String,
        #[serde(default, deserialize_with = "lenient_role")]
        role: Option<PartialRole>,
        #[serde(default, deserialize_with = "lenient_session_id")]
        session_id: Option<String>,
    },
    Result {
        #[serde(default, deserialize_with = "lenient_string")]
        text: String,
        #[serde(default, deserialize_with = "lenient_session_id")]
        session_id: Option<String>,
        #[serde(default)]
        usage: Option<serde_json::Value>,
    },
    Error {
        #[serde(default, deserialize_with = "lenient_string")]
        message: String,
        #[serde(default, deserialize_with = "lenient_session_id")]
        session_id: Option<String>,
        #[serde(default)]
        usage: Option<serde_json::Value>,
    },
    PermissionRequest(PermissionRequest),
}

impl AgentEvent {
    /// Non-empty `session_id` carried on this event, if any.
    pub fn session_id(&self) -> Option<&str> {
        let sid = match self {
            Self::Partial { session_id, .. }
            | Self::Result { session_id, .. }
            | Self::Error { session_id, .. } => session_id.as_deref(),
            Self::PermissionRequest(req) => req.session_id.as_deref(),
        };
        sid.filter(|s| !s.is_empty())
    }
}

/// Parse one stdout line into a typed [`AgentEvent`].
///
/// Returns `None` for unknown `type` values, non-object JSON, or malformed
/// JSON — the caller SHOULD log a warning and ignore the line per the spec.
pub fn parse_event(line: &str) -> Option<AgentEvent> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }
    let value: serde_json::Value = serde_json::from_str(trimmed).ok()?;
    if !value.is_object() {
        return None;
    }
    serde_json::from_value::<AgentEvent>(value).ok()
}

/// A permission response the bridge writes to the agent's stdin as one NDJSON
/// line (only when `permission: true`).
#[derive(Debug, Clone, Serialize)]
pub struct PermissionResponse {
    #[serde(rename = "type")]
    pub event_type: &'static str,
    pub request_id: String,
    pub behavior: PermissionBehavior,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub updated_input: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

impl PermissionResponse {
    pub fn allow(request_id: impl Into<String>) -> Self {
        Self {
            event_type: "permission_response",
            request_id: request_id.into(),
            behavior: PermissionBehavior::Allow,
            updated_input: None,
            message: None,
        }
    }

    pub fn deny(request_id: impl Into<String>, reason: impl Into<String>) -> Self {
        Self {
            event_type: "permission_response",
            request_id: request_id.into(),
            behavior: PermissionBehavior::Deny,
            updated_input: None,
            message: Some(reason.into()),
        }
    }

    pub fn to_ndjson(&self) -> serde_json::Result<String> {
        serde_json::to_string(self)
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PermissionBehavior {
    Allow,
    Deny,
}

fn is_false(b: &bool) -> bool {
    !b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn turn_object_serializes_required_and_optional_fields() {
        let turn = TurnObject::new("hi", "", "default", "u1", vec![], false);
        let json = turn.to_ndjson().unwrap();
        assert!(!json.contains("\"permission\""));
        assert!(json.contains("\"type\":\"turn\""));
        assert!(json.contains("\"message\":\"hi\""));
        assert!(json.contains("\"session_id\":\"\""));
        assert!(json.contains("\"from_user\":\"u1\""));
        assert!(json.contains("\"protocol_version\":\"0.4\""));
        assert!(json.contains("\"session_name\":\"default\""));
        assert!(json.contains("\"attachments\":[]"));
    }

    #[test]
    fn turn_object_includes_permission_when_true() {
        let turn = TurnObject::new("hi", "s1", "feat", "u1", vec![], true);
        let json = turn.to_ndjson().unwrap();
        assert!(json.contains("\"permission\":true"));
    }

    #[test]
    fn read_turn_from_slice() {
        let input = b"{\"type\":\"turn\",\"message\":\"hi\",\"session_id\":\"\",\"from_user\":\"u1\"}\n";
        let turn = read_turn(&input[..]).unwrap();
        assert_eq!(turn.message, "hi");
        assert_eq!(turn.from_user, "u1");
        assert_eq!(turn.session_name, "default");
        assert!(turn.attachments.is_empty());
        assert!(!turn.permission);
    }

    #[test]
    fn read_turn_eof_returns_none() {
        assert!(read_turn("".as_bytes()).is_none());
    }

    #[test]
    fn read_turn_malformed_returns_none() {
        assert!(read_turn(&b"not json\n"[..]).is_none());
    }

    #[test]
    fn parse_partial_event() {
        let ev = parse_event(r#"{"type":"partial","text":"hello "}"#).unwrap();
        match ev {
            AgentEvent::Partial { text, role, session_id } => {
                assert_eq!(text, "hello ");
                assert_eq!(role, None);
                assert_eq!(session_id, None);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn parse_partial_with_session_id() {
        let ev = parse_event(r#"{"type":"partial","text":"hi","session_id":"sess-1"}"#).unwrap();
        assert_eq!(ev.session_id(), Some("sess-1"));
    }

    #[test]
    fn parse_result_with_usage() {
        let ev = parse_event(
            r#"{"type":"result","text":"","usage":{"input_tokens":1,"output_tokens":2}}"#,
        )
        .unwrap();
        match ev {
            AgentEvent::Result { usage: Some(u), .. } => {
                assert_eq!(u["input_tokens"], 1);
                assert_eq!(u["output_tokens"], 2);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn parse_legacy_text_and_session_are_unknown() {
        assert!(parse_event(r#"{"type":"text","text":"final"}"#).is_none());
        assert!(parse_event(r#"{"type":"session","id":"sess-1"}"#).is_none());
    }

    #[test]
    fn parse_permission_request_event() {
        let ev = parse_event(
            r#"{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{"command":"echo hi"},"session_id":"s1"}"#,
        )
        .unwrap();
        match ev {
            AgentEvent::PermissionRequest(req) => {
                assert_eq!(req.request_id, "1");
                assert_eq!(req.tool_name, "Bash");
                assert_eq!(req.session_id.as_deref(), Some("s1"));
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn parse_unknown_type_returns_none() {
        assert!(parse_event(r#"{"type":"tool_call","text":"x"}"#).is_none());
        assert!(parse_event("not json").is_none());
        assert!(parse_event("").is_none());
        assert!(parse_event(r#"{"text":"no type"}"#).is_none());
    }

    #[test]
    fn permission_response_serializes() {
        let allow = PermissionResponse::allow("42").to_ndjson().unwrap();
        assert!(allow.contains("\"behavior\":\"allow\""));
        assert!(!allow.contains("updated_input"));

        let deny = PermissionResponse::deny("42", "not allowed").to_ndjson().unwrap();
        assert!(deny.contains("\"behavior\":\"deny\""));
        assert!(deny.contains("\"message\":\"not allowed\""));
    }
}
