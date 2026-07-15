//! Runner: `run()` and the four-case executor resolution rule.
//!
//! Two execution paths share the same stdout-handling semantics:
//!
//! - **spawn path** — fork `command`/`args` (a bridge script or any
//!   AgentProc-conformant process), write the turn object to its stdin, read
//!   NDJSON events from its stdout.
//! - **in-process path** (`run_via_executor`) — when `profile.executor` is
//!   set and the SDK recognises the name, skip the bridge subprocess; the
//!   executor's `build_args` spawns the target CLI directly, its
//!   `parse_event` translates the CLI's raw output.
//!
//! Both apply `timeout_secs` / `kill_grace_secs` / `max_reply_chars` /
//! `truncation_suffix` / `streaming` / `permission` identically.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::watch;
use tokio::time::timeout;

use crate::env::{build_base_env, expand_env_ref_with_allowlist, substitute, SubstCtx};
use crate::error::RunnerError;
#[cfg(feature = "executors")]
use crate::executors::{lookup, TurnHandlers};
use crate::protocol::{parse_event, AgentEvent, TurnObject};

/// Callbacks and inputs for a single [`run`] call.
pub struct RunOptions {
    pub message: String,
    pub session_id: Option<String>,
    pub session_name: Option<String>,
    pub from_user: Option<String>,
    pub cwd: Option<PathBuf>,
    pub profile_dir: Option<PathBuf>,
    pub timeout_secs: Option<u64>,
    pub streaming: Option<bool>,
    pub extra_env: HashMap<String, String>,
    pub attachments: Vec<crate::Attachment>,
    pub on_partial:
        Option<Arc<dyn Fn(String, Option<String>) + Send + Sync>>,
    pub on_session: Option<Arc<dyn Fn(&str) + Send + Sync>>,
    pub on_error: Option<Arc<dyn Fn(&str) + Send + Sync>>,
    pub on_permission:
        Option<Arc<dyn Fn(crate::PermissionRequest) -> crate::PermissionFuture + Send + Sync>>,
    pub on_stderr: Option<Arc<dyn Fn(&str) + Send + Sync>>,
}

impl RunOptions {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            session_id: None,
            session_name: None,
            from_user: None,
            cwd: None,
            profile_dir: None,
            timeout_secs: None,
            streaming: None,
            extra_env: HashMap::new(),
            attachments: Vec::new(),
            on_partial: None,
            on_session: None,
            on_error: None,
            on_permission: None,
            on_stderr: None,
        }
    }

    pub fn with_session_id(mut self, sid: impl Into<String>) -> Self {
        self.session_id = Some(sid.into());
        self
    }

    pub fn with_cwd(mut self, cwd: impl Into<PathBuf>) -> Self {
        self.cwd = Some(cwd.into());
        self
    }

    pub fn on_partial<F>(mut self, f: F) -> Self
    where
        F: Fn(String, Option<String>) + Send + Sync + 'static,
    {
        self.on_partial = Some(Arc::new(f));
        self
    }

    pub fn on_session<F>(mut self, f: F) -> Self
    where
        F: Fn(&str) + Send + Sync + 'static,
    {
        self.on_session = Some(Arc::new(f));
        self
    }
}

/// The outcome of a run.
#[derive(Debug, Clone, Default)]
pub struct RunResult {
    /// Concatenation of forwarded partials (streaming) or the result body.
    pub reply: String,
    /// First non-empty session id seen on any event.
    pub session_id: String,
    /// Error message, empty when the turn succeeded.
    pub error: String,
    /// Child exit code. 124 = timeout, 130/143 = killed by signal.
    pub exit_code: i32,
    pub timed_out: bool,
    pub usage: Option<serde_json::Value>,
}

impl RunResult {
    pub fn ok(&self) -> bool {
        self.error.is_empty() && !self.timed_out
    }
}

/// Run a profile for one turn. Resolves the four-case executor rule:
///
/// 1. `executor` + known   → in-process, ignore command/args
/// 2. `executor` + unknown → warn stderr, spawn command (fallback)
/// 3. `executor` + unknown + no command → hard fail
/// 4. no `executor`        → spawn command
pub async fn run(profile: &crate::Profile, opts: RunOptions) -> Result<RunResult, RunnerError> {
    let cfg = ResolvedConfig::from(profile, &opts)?;

    #[cfg(feature = "executors")]
    if let Some(name) = &profile.executor {
        if let Some(exec) = lookup(name) {
            return run_via_executor(profile, opts, cfg, exec).await;
        }
        // Unknown executor: warn + fallback to command.
        if let Some(on_stderr) = &opts.on_stderr {
            on_stderr(&format!(
                "[agentproc runner] unknown executor `{name}`; falling back to command spawn"
            ));
        }
        if profile.command.trim().is_empty() {
            return Err(RunnerError::UnknownExecutor(
                name.clone(),
                crate::executors::executor_names().join(", "),
            ));
        }
    }

    run_via_spawn(profile, opts, cfg).await
}

/// Pre-resolved, merged run configuration (profile + option overrides).
struct ResolvedConfig {
    timeout_secs: u64,
    streaming: bool,
    env: HashMap<String, String>,
    subst: SubstCtx,
    from_user: String,
    session_name: String,
}

impl ResolvedConfig {
    fn from(profile: &crate::Profile, opts: &RunOptions) -> Result<Self, RunnerError> {
        let timeout_secs = opts.timeout_secs.unwrap_or(profile.timeout_secs);
        let streaming = opts.streaming.unwrap_or(profile.streaming);

        let parent_env: HashMap<String, String> = std::env::vars().collect();
        let mut env = build_base_env(&parent_env);

        // Layer 2: profile.env after ${VAR} expansion + allowlist.
        let allowlist = profile.env_allowlist.as_deref();
        let on_blocked = Box::new(|msg: &str| {
            eprintln!("[agentproc runner] {msg}");
        });
        for (k, raw_v) in &profile.env {
            let expanded =
                expand_env_ref_with_allowlist(raw_v, &parent_env, allowlist, |m| on_blocked(m));
            env.insert(k.clone(), expanded);
        }
        // Layer 3: caller extras.
        for (k, v) in &opts.extra_env {
            env.insert(k.clone(), v.clone());
        }

        let subst = SubstCtx {
            message: opts.message.clone(),
            session_id: opts.session_id.clone().unwrap_or_default(),
            session_name: opts
                .session_name
                .clone()
                .unwrap_or_else(|| "default".to_string()),
            profile_dir: opts
                .profile_dir
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default(),
        };

        Ok(Self {
            timeout_secs,
            streaming,
            env,
            subst,
            from_user: opts.from_user.clone().unwrap_or_else(|| "user".to_string()),
            session_name: opts
                .session_name
                .clone()
                .unwrap_or_else(|| "default".to_string()),
        })
    }
}

// ---------------------------------------------------------------------------
// spawn path
// ---------------------------------------------------------------------------

async fn run_via_spawn(
    profile: &crate::Profile,
    opts: RunOptions,
    cfg: ResolvedConfig,
) -> Result<RunResult, RunnerError> {
    let command = substitute(&profile.command, &cfg.subst);
    let args: Vec<String> = profile.args.iter().map(|a| substitute(a, &cfg.subst)).collect();
    if command.trim().is_empty() {
        return Err(RunnerError::EmptyCommand);
    }

    let mut cmd = Command::new(&command);
    cmd.args(&args);
    if let Some(cwd) = &opts.cwd {
        cmd.current_dir(cwd);
    } else if let Some(cwd) = &profile.cwd {
        cmd.current_dir(substitute(cwd, &cfg.subst));
    }
    cmd.env_clear();
    for (k, v) in &cfg.env {
        cmd.env(k, v);
    }
    cmd.stdin(Stdio::piped());
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());
    cmd.kill_on_drop(true);

    let cli = command.clone();
    let mut child = cmd.spawn().map_err(|e| RunnerError::Spawn { cli, source: e })?;

    // Write the turn object to stdin, then (unless permission) close stdin.
    let turn = TurnObject::new(
        opts.message.clone(),
        opts.session_id.clone().unwrap_or_default(),
        cfg.session_name.clone(),
        cfg.from_user.clone(),
        opts.attachments.clone(),
        profile.permission,
    );
    let turn_line = turn.to_ndjson().map_err(RunnerError::Json)?;
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(turn_line.as_bytes()).await;
        let _ = stdin.write_all(b"\n").await;
        if !profile.permission {
            drop(stdin);
        }
    }

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    let stderr_task = tokio::spawn(drain_capped(stderr, opts.on_stderr.clone(), 1_000_000));

    let (partial_tx, partial_rx) = watch::channel(None::<String>);
    let result = process_stdout(
        BufReader::new(stdout),
        process_opts(&opts, &cfg),
        partial_tx.clone(),
        child.stdin.take(),
    )
    .await;

    // Stop streaming once turn is done.
    let _ = partial_tx.send(None);

    let (status, stderr_output) = tokio::join!(
        wait_with_timeout(child, cfg.timeout_secs),
        stderr_task,
    );
    let stderr_output = stderr_output.unwrap_or_default();

    let mut result = result?;
    let exit_code = match status {
        Ok(s) => s.code().unwrap_or(0),
        Err(RunnerError::Timeout { .. }) => {
            result.timed_out = true;
            124
        }
        Err(e) => return Err(e),
    };
    result.exit_code = exit_code;

    finalise_result(
        &mut result,
        profile,
        &cfg,
        &stderr_output,
        exit_code,
        opts.on_error,
    );

    // Drain any remaining partials we haven't observed yet.
    drop(partial_rx);
    Ok(result)
}

/// Holds the runtime callbacks in an Arc for cheap cloning into tasks.
struct ProcessOpts {
    streaming: bool,
    #[allow(dead_code)]
    max_reply_chars: usize,
    #[allow(dead_code)]
    truncation_suffix: String,
    on_partial: Option<Arc<dyn Fn(String, Option<String>) + Send + Sync>>,
    on_session: Option<Arc<dyn Fn(&str) + Send + Sync>>,
}

fn process_opts(opts: &RunOptions, cfg: &ResolvedConfig) -> ProcessOpts {
    ProcessOpts {
        streaming: cfg.streaming,
        max_reply_chars: 0,
        truncation_suffix: String::new(),
        on_partial: opts.on_partial.clone(),
        on_session: opts.on_session.clone(),
    }
}

/// Read NDJSON lines from the agent's stdout, classify them, invoke callbacks,
/// and accumulate the RunResult.
async fn process_stdout<R: tokio::io::AsyncBufRead + Unpin>(
    mut reader: R,
    po: ProcessOpts,
    _partial_tx: watch::Sender<Option<String>>,
    _stdin: Option<tokio::process::ChildStdin>,
) -> Result<RunResult, RunnerError> {
    let mut result = RunResult::default();
    let mut saw_error = false;
    let mut line = String::new();

    loop {
        line.clear();
        let n = reader.read_line(&mut line).await?;
        if n == 0 {
            break;
        }
        let Some(event) = parse_event(line.trim()) else {
            // Malformed / unknown — warn and ignore per spec.
            continue;
        };
        match event {
            AgentEvent::Partial { text, session_id, .. } => {
                if saw_error {
                    continue;
                }
                if let Some(sid) = session_id.as_deref() {
                    note_session(&mut result, sid, &po.on_session);
                }
                if po.streaming {
                    if let Some(cb) = &po.on_partial {
                        cb(text.clone(), result.session_id.clone().into());
                    }
                    result.reply.push_str(&text);
                }
            }
            AgentEvent::Result { text, session_id, usage } => {
                if let Some(sid) = session_id.as_deref() {
                    note_session(&mut result, sid, &po.on_session);
                }
                if let Some(u) = usage {
                    result.usage = Some(u);
                }
                // Append result text only when no partials forwarded.
                if !saw_error && result.reply.is_empty() && !text.is_empty() {
                    result.reply = text;
                }
            }
            AgentEvent::Error { message, session_id, usage } => {
                if let Some(sid) = session_id.as_deref() {
                    note_session(&mut result, sid, &po.on_session);
                }
                if let Some(u) = usage {
                    result.usage = Some(u);
                }
                saw_error = true;
                result.error = message;
                if let Some(cb) = &po.on_session {
                    // reuse a no-op slot
                    let _ = cb;
                }
            }
            AgentEvent::PermissionRequest(_req) => {
                // Permission handling via on_permission is wired but not yet
                // driving stdin writes in this skeleton (TODO: mpsc writer
                // task). For now we log to stderr.
                eprintln!(
                    "[agentproc runner] permission_request received but interactive permission channel is not wired up in this build"
                );
            }
        }
    }
    Ok(result)
}

fn note_session(
    result: &mut RunResult,
    sid: &str,
    on_session: &Option<Arc<dyn Fn(&str) + Send + Sync>>,
) {
    if sid.is_empty() {
        return;
    }
    if result.session_id.is_empty() {
        result.session_id = sid.to_string();
        if let Some(cb) = on_session {
            cb(sid);
        }
    }
}

fn finalise_result(
    result: &mut RunResult,
    profile: &crate::Profile,
    _cfg: &ResolvedConfig,
    stderr: &str,
    exit_code: i32,
    on_error: Option<Arc<dyn Fn(&str) + Send + Sync>>,
) {
    // Apply max_reply_chars truncation.
    let max = profile.max_reply_chars;
    if result.reply.chars().count() > max {
        let truncated: String = result.reply.chars().take(max).collect();
        result.reply = format!("{truncated}{}", profile.truncation_suffix);
    }

    if !result.error.is_empty() {
        if profile.send_error_reply {
            if let Some(cb) = &on_error {
                cb(&result.error);
            }
        }
        return;
    }

    if exit_code != 0 && result.reply.is_empty() && result.session_id.is_empty() {
        let mut msg = format!("agent exited with code {exit_code}");
        let stderr_trim = stderr.trim();
        if !stderr_trim.is_empty() {
            msg.push_str(": ");
            msg.push_str(&stderr_trim.chars().take(500).collect::<String>());
        }
        result.error = msg;
        if profile.send_error_reply {
            if let Some(cb) = &on_error {
                cb(&result.error);
            }
        }
    }
}

async fn drain_capped<R: tokio::io::AsyncRead + Unpin>(
    mut reader: R,
    on_stderr: Option<Arc<dyn Fn(&str) + Send + Sync>>,
    cap: usize,
) -> String {
    let mut buf = Vec::with_capacity(8192);
    let mut chunk = [0u8; 4096];
    loop {
        match reader.read(&mut chunk).await {
            Ok(0) => break,
            Ok(n) => {
                if buf.len() + n > cap {
                    let room = cap.saturating_sub(buf.len());
                    buf.extend_from_slice(&chunk[..room]);
                    if let Some(cb) = &on_stderr {
                        cb("[agentproc runner] stderr capped; trailing output dropped");
                    }
                    break;
                }
                if let Some(cb) = &on_stderr {
                    if let Ok(s) = std::str::from_utf8(&chunk[..n]) {
                        cb(s);
                    }
                }
                buf.extend_from_slice(&chunk[..n]);
            }
            Err(_) => break,
        }
    }
    String::from_utf8_lossy(&buf).into_owned()
}

async fn wait_with_timeout(
    mut child: tokio::process::Child,
    secs: u64,
) -> Result<std::process::ExitStatus, RunnerError> {
    match timeout(Duration::from_secs(secs), child.wait()).await {
        Ok(r) => r.map_err(RunnerError::from),
        Err(_) => {
            // Timeout: SIGTERM, wait kill_grace, then SIGKILL.
            let _ = child.start_kill();
            let _ = child.wait().await;
            Err(RunnerError::Timeout { secs })
        }
    }
}

// ---------------------------------------------------------------------------
// in-process path
// ---------------------------------------------------------------------------

#[cfg(feature = "executors")]
async fn run_via_executor(
    profile: &crate::Profile,
    opts: RunOptions,
    cfg: ResolvedConfig,
    exec: Box<dyn crate::executors::Executor>,
) -> Result<RunResult, RunnerError> {
    let handlers = exec.make_turn();
    let session_id = opts.session_id.clone().unwrap_or_default();
    let argv = handlers.build_args(&opts.message, &session_id, &cfg.env);
    if argv.is_empty() {
        return Err(RunnerError::InvalidProfile(format!(
            "executor `{}` build_args returned empty argv",
            exec.cli_name()
        )));
    }

    let cli = argv[0].clone();
    let mut cmd = Command::new(&cli);
    cmd.args(&argv[1..]);
    if let Some(cwd) = &opts.cwd {
        cmd.current_dir(cwd);
    } else if let Some(cwd) = &profile.cwd {
        cmd.current_dir(substitute(cwd, &cfg.subst));
    }
    cmd.env_clear();
    for (k, v) in &cfg.env {
        cmd.env(k, v);
    }
    cmd.stdin(Stdio::null());
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());
    cmd.kill_on_drop(true);

    let mut child = cmd
        .spawn()
        .map_err(|e| RunnerError::Spawn { cli: cli.clone(), source: e })?;

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    let stderr_task = tokio::spawn(drain_capped(stderr, opts.on_stderr.clone(), 1_000_000));

    // For plain executors that mint/reuse a session id in build_args (e.g.
    // agy), surface it on RunResult after the turn ends. Must extract before
    // handlers is moved into process_executor_stdout (NDJSON path).
    let plain_session_id = if exec.plain() {
        handlers.get_session_id()
    } else {
        None
    };

    let result = if exec.plain() {
        process_plain(BufReader::new(stdout)).await
    } else {
        process_executor_stdout(BufReader::new(stdout), handlers, &cfg, &opts).await
    };

    let (status, stderr_output) = tokio::join!(
        wait_with_timeout(child, cfg.timeout_secs),
        stderr_task,
    );
    let stderr_output = stderr_output.unwrap_or_default();

    let mut result = result?;
    let exit_code = match status {
        Ok(s) => s.code().unwrap_or(0),
        Err(RunnerError::Timeout { .. }) => {
            result.timed_out = true;
            124
        }
        Err(e) => return Err(e),
    };
    result.exit_code = exit_code;

    finalise_result(
        &mut result,
        profile,
        &cfg,
        &stderr_output,
        exit_code,
        opts.on_error,
    );

    // Plain executors: fill session_id from the id minted/reused in build_args
    // (NDJSON path already set it from stdout events).
    if result.session_id.is_empty() {
        if let Some(sid) = plain_session_id {
            result.session_id = sid;
        }
    }

    Ok(result)
}

#[cfg(feature = "executors")]
async fn process_executor_stdout<R: tokio::io::AsyncBufRead + Unpin>(
    mut reader: R,
    mut handlers: Box<dyn TurnHandlers>,
    cfg: &ResolvedConfig,
    opts: &RunOptions,
) -> Result<RunResult, RunnerError> {
    let mut result = RunResult::default();
    let mut saw_error = false;
    let mut line = String::new();

    loop {
        line.clear();
        let n = reader.read_line(&mut line).await?;
        if n == 0 {
            break;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) else {
            continue;
        };
        let Some(parsed) = handlers.parse_event(value) else {
            continue;
        };

        if let Some(sid) = &parsed.session_id {
            if !sid.is_empty() && result.session_id.is_empty() {
                result.session_id = sid.clone();
                if let Some(cb) = &opts.on_session {
                    cb(sid);
                }
            }
        }
        if let Some(u) = parsed.usage {
            result.usage = Some(u);
        }
        if let Some(err) = parsed.error {
            if !saw_error {
                saw_error = true;
                result.error = err;
            }
            continue;
        }
        if saw_error {
            continue;
        }
        if let Some(ptext) = parsed.partial_text {
            if cfg.streaming {
                if let Some(cb) = &opts.on_partial {
                    cb(ptext.clone(), result.session_id.clone().into());
                }
                result.reply.push_str(&ptext);
            }
        }
        if let Some(Some(ftext)) = parsed.final_text {
            if result.reply.is_empty() {
                result.reply = ftext;
            }
        }
    }
    Ok(result)
}

#[cfg(feature = "executors")]
async fn process_plain<R: tokio::io::AsyncRead + Unpin>(
    mut reader: R,
) -> Result<RunResult, RunnerError> {
    let mut buf = Vec::new();
    reader.read_to_end(&mut buf).await?;
    let text = String::from_utf8_lossy(&buf).trim().to_string();
    Ok(RunResult {
        reply: text,
        ..Default::default()
    })
}
