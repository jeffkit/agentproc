//! Child environment composition and placeholder substitution.
//!
//! Three layers per spec §"Child environment composition":
//! 1. Infra set (always applied) — operational vars only, never credentials.
//! 2. Profile `env` block after `${VAR}` expansion + `env_allowlist` filtering.
//! 3. Caller extras (`RunOptions::extra_env`).

use std::collections::HashMap;

/// Operational variables the runner always copies from its own environment
/// into the child. None are credential-bearing. Mirrors the infra set in
/// `spec/protocol.md` §"Child environment composition".
pub const ENV_INFRA_VARS: &[&str] = &[
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TERM",
    "TMPDIR",
    "TZ",
    "PWD",
    // Windows infra vars (no-ops on unix, present for parity with node SDK).
    "SystemRoot",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "PATHEXT",
    "COMSPEC",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
];

/// Build the base child environment: infra set copied from `parent`.
pub fn build_base_env(parent: &HashMap<String, String>) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for name in ENV_INFRA_VARS {
        if let Some(val) = parent.get(*name) {
            out.insert((*name).to_string(), val.clone());
        }
    }
    out
}

/// Expand `${VAR}` references in `value` against `env`. Unknown variables
/// expand to the empty string (POSIX-shell semantics).
pub fn expand_env_ref(value: &str, env: &HashMap<String, String>) -> String {
    fn noop(_: &str) {}
    expand_env_ref_with_allowlist(value, env, None, noop)
}

/// Expand `${VAR}` with an optional allowlist. When `allowlist` is `Some`,
/// references to names not in the list expand to empty and `on_blocked` is
/// called with a warning message.
pub fn expand_env_ref_with_allowlist<F: FnMut(&str)>(
    value: &str,
    env: &HashMap<String, String>,
    allowlist: Option<&[String]>,
    mut on_blocked: F,
) -> String {
    let bytes = value.as_bytes();
    let mut out = String::with_capacity(value.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'$' && i + 1 < bytes.len() && bytes[i + 1] == b'{' {
            // Find the closing `}`.
            if let Some(end) = bytes[i + 2..].iter().position(|&b| b == b'}') {
                let name = &value[i + 2..i + 2 + end];
                if is_valid_env_ident(name) {
                    let allowed = allowlist.map(|a| a.iter().any(|n| n == name)).unwrap_or(true);
                    if allowed {
                        if let Some(v) = env.get(name) {
                            out.push_str(v);
                        }
                        // Unknown but allowed → empty string.
                    } else {
                        on_blocked(&format!(
                            "env_allowlist blocked ${{{name}}}; expanded to empty"
                        ));
                    }
                    i = i + 2 + end + 1;
                    continue;
                }
            }
        }
        out.push(bytes[i] as char);
        i += 1;
    }
    out
}

fn is_valid_env_ident(name: &str) -> bool {
    if name.is_empty() {
        return false;
    }
    let mut chars = name.chars();
    let first = chars.next().unwrap();
    if !(first.is_ascii_alphabetic() || first == '_') {
        return false;
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// Substitute `{{PLACEHOLDER}}` tokens in a string. Supported tokens:
/// `{{MESSAGE}}`, `{{SESSION_ID}}`, `{{SESSION_NAME}}`, `{{PROFILE_DIR}}`.
pub fn substitute(value: &str, ctx: &SubstCtx) -> String {
    value
        .replace("{{MESSAGE}}", &ctx.message)
        .replace("{{SESSION_ID}}", &ctx.session_id)
        .replace("{{SESSION_NAME}}", &ctx.session_name)
        .replace("{{PROFILE_DIR}}", &ctx.profile_dir)
}

/// Placeholder context.
#[derive(Debug, Clone, Default)]
pub struct SubstCtx {
    pub message: String,
    pub session_id: String,
    pub session_name: String,
    pub profile_dir: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env_of(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    #[test]
    fn expand_known_var() {
        let env = env_of(&[("FOO", "bar")]);
        assert_eq!(expand_env_ref("x${FOO}y", &env), "xbary");
    }

    #[test]
    fn expand_unknown_to_empty() {
        let env = HashMap::new();
        assert_eq!(expand_env_ref("x${NOPE}y", &env), "xy");
    }

    #[test]
    fn expand_with_allowlist_blocks() {
        let env = env_of(&[("ALLOWED", "yes"), ("SECRET", "shh")]);
        let allow = vec!["ALLOWED".to_string()];
        let mut warnings = Vec::new();
        let out = expand_env_ref_with_allowlist(
            "${ALLOWED}-${SECRET}",
            &env,
            Some(&allow),
            |m| warnings.push(m.to_string()),
        );
        assert_eq!(out, "yes-");
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("SECRET"));
    }

    #[test]
    fn expand_invalid_ident_left_untouched() {
        let env = HashMap::new();
        assert_eq!(expand_env_ref("${1BAD}", &env), "${1BAD}");
    }

    #[test]
    fn build_base_env_copies_only_infra() {
        let parent = env_of(&[
            ("PATH", "/bin"),
            ("HOME", "/u"),
            ("SECRET", "shh"),
        ]);
        let base = build_base_env(&parent);
        assert_eq!(base.get("PATH").map(|s| s.as_str()), Some("/bin"));
        assert_eq!(base.get("HOME").map(|s| s.as_str()), Some("/u"));
        assert!(!base.contains_key("SECRET"));
    }

    #[test]
    fn substitute_placeholders() {
        let ctx = SubstCtx {
            message: "hi".into(),
            session_id: "s1".into(),
            session_name: "feat".into(),
            profile_dir: "/p".into(),
        };
        assert_eq!(
            substitute("{{MESSAGE}}|{{SESSION_ID}}|{{SESSION_NAME}}|{{PROFILE_DIR}}", &ctx),
            "hi|s1|feat|/p"
        );
    }
}
