package agentproc

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// PROTOCOL_VERSION is the wire protocol this runner speaks (spec/protocol.md
// v0.4). Single literal definition — the SDK entry point re-exports it, the
// way the Python and Node SDKs import it from their runner modules.
const PROTOCOL_VERSION = "0.4"

// Spec exit codes (spec/protocol.md "Exit Codes").
const (
	ExitSuccess   = 0
	ExitError     = 1
	ExitTimeout   = 124 // bridge-imposed timeout (GNU timeout convention)
	ExitCancelled = 125 // cooperative cancel (host extension, matches the Python SDK)
	ExitSigint    = 130 // interrupted by SIGINT
	ExitSigterm   = 143 // terminated by SIGTERM
)

// AutoApproveFlags are the argv tokens that mean "auto-approve everything".
// Single source of truth is auto_approve_flags in spec/conformance/cases.json;
// the conformance driver asserts the two lists are equal item for item.
var AutoApproveFlags = []string{
	"--dangerously-skip-permissions",
	"--yolo",
	"--always-approve",
	"--yes-always",
	"--approve",
	"--auto",
}

// AutoApproveEnabled reports whether executors may bake auto-approve flags
// into argv. Read from the runner process's own environment (the spec makes
// AGENTPROC_AUTO_APPROVE a process-side knob, not a profile field): "0" /
// "false", case-insensitive and whitespace-trimmed, turns it off.
func AutoApproveEnabled() bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv("AGENTPROC_AUTO_APPROVE")))
	return value != "0" && value != "false"
}

// PostureRefusal returns why this turn must not be spawned, or "" to proceed.
// Two refusal rules (spec "Runner contract" step 9): a profile permission
// channel the executor cannot serve, and an auto-approve argv while
// AGENTPROC_AUTO_APPROVE is off. Refusal is an error + non-zero exit — never
// a silent fallback to an auto-approve flag.
func PostureRefusal(cliName string, supportsPermission, permission, autoApproveEnabled bool, argv []string) string {
	if permission && !supportsPermission {
		return fmt.Sprintf(
			"executor '%s' has no AgentProc permission channel; "+
				"refusing to run with auto-approve. Remove 'permission: true' from "+
				"the profile, or use an executor that supports it (claude-code).", cliName)
	}
	if !autoApproveEnabled {
		for _, token := range AutoApproveFlags {
			for _, a := range argv {
				if a == token {
					return fmt.Sprintf(
						"executor '%s' would run with the auto-approve flag "+
							"'%s', but AGENTPROC_AUTO_APPROVE is off. Unset "+
							"AGENTPROC_AUTO_APPROVE, or use a profile that does not need "+
							"auto-approval.", cliName, token)
				}
			}
		}
	}
	return ""
}

// RunResult is the outcome of one agent turn.
type RunResult struct {
	// Reply is the user-visible reply body (spec reply-assembly rules).
	Reply string
	// SessionID is the first non-empty valid session id observed (or "").
	SessionID string
	// Error is the terminal error message ("" on success).
	Error string
	// ExitCode is the spec-normalized exit code.
	ExitCode int
	// TimedOut is true when the bridge killed the turn on its time limit.
	TimedOut bool
	// Usage is the terminal event's usage object (first non-empty wins).
	Usage map[string]any
	// StartedAt is the ISO-8601 UTC turn-start instant, millisecond precision
	// (spec "Event traceability").
	StartedAt string
	// Duration is bridge-measured wall-clock seconds covering spawn-to-exit.
	Duration float64
}

// PermissionDecision is the bridge's answer to one permission_request.
type PermissionDecision struct {
	// Behavior is "allow" or "deny" (anything but "allow" denies).
	Behavior string
	// UpdatedInput, when non-nil, overrides the request's input on allow.
	// When nil the response omits updated_input and the agent MUST fall back
	// to the request's original input — the runner never auto-fills it.
	UpdatedInput map[string]any
	// Message is the deny reason surfaced to the model or user.
	Message string
}

// JournalEntry is one opt-in journal record (spec "Event traceability"):
// classified events carry Kind/Line/Seq; bridge-level decisions carry
// Decision (timeout | sigterm_process_group | sigkill_process_group).
type JournalEntry struct {
	Seq        int     `json:"seq"`
	Ts         string  `json:"ts"`
	Kind       string  `json:"kind,omitempty"`
	Line       string  `json:"line,omitempty"`
	Decision   string  `json:"decision,omitempty"`
	BudgetSecs float64 `json:"budget_secs,omitempty"`
}

// RunOptions configures one run.
type RunOptions struct {
	// Message is the user message; delivered on stdin only.
	Message string
	// SessionID resumes an agent session ("" = new session).
	SessionID string
	// SessionName is the human-readable session name ("" = "default").
	SessionName string
	// Streaming overrides the profile's streaming hint. nil = use profile.
	Streaming *bool
	// ExtraEnv is the CLI --env layer (overrides profile env).
	ExtraEnv map[string]string
	// Attachments is the turn's attachment list (presence-as-feature: the
	// key is included in the turn object only when non-empty).
	Attachments []any
	// Cwd overrides the profile cwd.
	Cwd string
	// ProfileDir is the profile's own directory ({{PROFILE_DIR}} expansion
	// and relative-cwd resolution).
	ProfileDir string
	// TimeoutSecs overrides the profile timeout. 0 = use the profile value
	// (a profile value of 0 means no host-side limit).
	TimeoutSecs int
	// RunLockKey (usually the workspace absolute path) turns on the "last
	// will" run lock: after spawn the runner records pid + argv + this
	// process's pid, so a takeover can kill-before-start the orphan group a
	// hard-killed host left behind (CleanupStaleRun). The lock is cleared
	// after the group kill on normal completion; early returns keep it on
	// purpose (self-healing: the recorded pid is dead by then).
	RunLockKey string
	// OnJournal is the opt-in journal hook (nil = no journaling).
	OnJournal func(JournalEntry)
	// OnPartial is called as each partial is forwarded. role is nil when the
	// event carries none (the runner MUST NOT synthesise "output").
	OnPartial func(text string, role *string)
	// OnSession is called once, when the first non-empty valid session id
	// is learned.
	OnSession func(sessionID string)
	// OnError is called when a terminal error is observed.
	OnError func(message string)
	// OnProtocolLine is called for every raw stdout line (debug traces).
	OnProtocolLine func(line string)
	// OnStderr is called for each stderr line and for runner warnings.
	OnStderr func(line string)
	// OnPermission answers a permission_request. nil = no approver wired:
	// the agent stays blocked until the turn timeout.
	OnPermission func(request map[string]any) *PermissionDecision
}

func (o *RunOptions) stderrf(format string, args ...any) {
	if o != nil && o.OnStderr != nil {
		o.OnStderr(fmt.Sprintf(format, args...))
	}
}

func (o *RunOptions) sessionName() string {
	if o == nil || o.SessionName == "" {
		return "default"
	}
	return o.SessionName
}

func isoMillis(t time.Time) string {
	return t.UTC().Format("2006-01-02T15:04:05.000+00:00")
}

// Run executes one agent turn per the AgentProc spec (spawn path, or the
// in-process executor path when the profile names a registered executor).
//
// Profile validation errors (including an invalid deadline) return a non-nil
// error; turn-level failures come back in the RunResult (Error + ExitCode).
// ctx cancellation performs the cooperative cancel: the process group gets
// the graded kill (SIGTERM → kill grace → SIGKILL) and the result carries
// ExitCancelled (125) with TimedOut=false.
func Run(ctx context.Context, profileRaw map[string]any, opts *RunOptions) (*RunResult, error) {
	profile, err := NormalizeProfile(profileRaw)
	if err != nil {
		return nil, err
	}

	// Four-case executor resolution (mirrors the Python/Node runners):
	//  (1) no executor: fall through to the spawn path
	//  (2) executor present + SDK knows it: in-process, skip spawn
	//  (3) executor present + SDK unknown + command present: warn, spawn fallback
	//  (4) executor present + SDK unknown + no command: hard fail
	if profile.Executor != "" {
		factory, known := lookupExecutor(profile.Executor)
		if !known {
			if profile.Command == "" {
				result := &RunResult{ExitCode: ExitError}
				result.Error = fmt.Sprintf("Unknown executor '%s'. Available: %s",
					profile.Executor, strings.Join(ExecutorNames(), ", "))
				if opts != nil && opts.OnError != nil {
					opts.OnError(result.Error)
				}
				return result, nil
			}
			opts.stderrf("[agentproc runner] unknown executor %q; falling back to spawn (command: %q)",
				profile.Executor, profile.Command)
		} else {
			return RunViaExecutor(ctx, factory(), opts, profile), nil
		}
	}
	return runSpawn(ctx, profile, opts)
}

// runSpawn — the spawn-path bridge engine.
func runSpawn(ctx context.Context, profile *Profile, opts *RunOptions) (*RunResult, error) {
	if opts == nil {
		opts = &RunOptions{}
	}
	streaming := profile.Streaming
	if opts.Streaming != nil {
		streaming = *opts.Streaming
	}
	timeoutSecs := profile.TimeoutSecs
	if opts.TimeoutSecs != 0 {
		timeoutSecs = opts.TimeoutSecs
	}

	startedWall := time.Now()
	startedMono := startedWall
	var seq atomic.Int64
	journal := func(e JournalEntry) {
		if opts.OnJournal == nil {
			return
		}
		e.Ts = isoMillis(time.Now())
		opts.OnJournal(e)
	}

	// Earliest expiry of timeout_secs / budget_secs / deadline (spec "Time
	// budget and absolute deadline").
	var effectiveSecs float64
	if timeoutSecs > 0 {
		effectiveSecs = float64(timeoutSecs)
	}
	if profile.BudgetSecs != nil && *profile.BudgetSecs > 0 {
		if effectiveSecs == 0 || *profile.BudgetSecs < effectiveSecs {
			effectiveSecs = *profile.BudgetSecs
		}
	}
	deadlineEpoch, err := ParseDeadline(profile.Deadline)
	if err != nil {
		return nil, err
	}
	if deadlineEpoch > 0 {
		fromDeadline := deadlineEpoch - float64(startedWall.UnixNano())/1e9
		if effectiveSecs == 0 || fromDeadline < effectiveSecs {
			effectiveSecs = fromDeadline
		}
	}

	cwd := opts.Cwd
	if cwd == "" {
		cwd = profile.Cwd
	}
	if cwd != "" && !filepath.IsAbs(cwd) && opts.ProfileDir != "" {
		cwd = filepath.Join(opts.ProfileDir, cwd)
	}

	substCtx := map[string]string{
		"message":      opts.Message,
		"session_id":   opts.SessionID,
		"session_name": opts.sessionName(),
		"profile_dir":  opts.ProfileDir,
	}
	argv := make([]string, 0, len(profile.Args)+1)
	argv = append(argv, Substitute(profile.Command, substCtx))
	for _, a := range profile.Args {
		argv = append(argv, Substitute(a, substCtx))
	}
	env := ComposeEnv(profile.Env, profile.EnvAllowlist, opts.ExtraEnv, substCtx, opts.OnStderr)

	// The turn object (wire 0.4 stdin payload). No AGENT_* env in 0.4.
	turn := orderedObject{
		{"type", "turn"},
		{"message", opts.Message},
		{"session_id", opts.SessionID},
		{"session_name", opts.sessionName()},
		{"protocol_version", PROTOCOL_VERSION},
	}
	if len(opts.Attachments) > 0 {
		turn = append(turn, orderedField{"attachments", opts.Attachments})
	}
	if profile.Permission {
		turn = append(turn, orderedField{"permission", true})
	}
	turnLine := marshalCompact(turn) + "\n"

	result := &RunResult{StartedAt: isoMillis(startedWall), ExitCode: ExitSuccess}

	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Dir = cwd
	cmd.Env = envSlice(env)
	configureProcessGroup(cmd)

	stdinR, stdinW, err := os.Pipe()
	if err != nil {
		return fatalResult(err)
	}
	stdoutR, stdoutW, err := os.Pipe()
	if err != nil {
		stdinR.Close()
		stdinW.Close()
		return fatalResult(err)
	}
	stderrR, stderrW, err := os.Pipe()
	if err != nil {
		stdinR.Close()
		stdinW.Close()
		stdoutR.Close()
		stdoutW.Close()
		return fatalResult(err)
	}
	cmd.Stdin = stdinR
	cmd.Stdout = stdoutW
	cmd.Stderr = stderrW

	if err := cmd.Start(); err != nil {
		for _, f := range []*os.File{stdinR, stdinW, stdoutR, stdoutW, stderrR, stderrW} {
			f.Close()
		}
		hint := diagnoseSpawnError(err, argv, cwd)
		opts.stderrf("[agentproc runner] spawn error: %s", err)
		if hint != "" {
			opts.stderrf("[agentproc runner] hint: %s", hint)
		}
		message := hint
		if message == "" {
			message = err.Error()
		}
		if opts.OnError != nil {
			opts.OnError("failed to start agent: " + message)
		}
		result.Error = message
		result.ExitCode = ExitError
		result.Duration = time.Since(startedMono).Seconds()
		return result, nil
	}
	// The child holds its own ends now; the parent copies must close so EOF
	// can arrive when the child (and every helper it spawned) exits.
	stdinR.Close()
	stdoutW.Close()
	stderrW.Close()

	if opts.RunLockKey != "" {
		WriteRunLock(opts.RunLockKey, cmd.Process.Pid, argv, 0)
	}

	// --- stdin: turn line, then EOF unless the permission channel is on ---
	var stdinMu sync.Mutex
	stdinClosed := false
	closeStdin := func() {
		stdinMu.Lock()
		defer stdinMu.Unlock()
		if stdinClosed {
			return
		}
		stdinClosed = true
		stdinW.Close()
	}
	writePermissionResponse := func(response map[string]any) bool {
		stdinMu.Lock()
		defer stdinMu.Unlock()
		if stdinClosed {
			return false
		}
		if _, err := stdinW.WriteString(FormatPermissionResponse(response) + "\n"); err != nil {
			return false
		}
		return true
	}
	pendingMu := sync.Mutex{}
	pendingPermissions := map[string]bool{}
	denyPending := func(message string) {
		pendingMu.Lock()
		ids := make([]string, 0, len(pendingPermissions))
		for id := range pendingPermissions {
			ids = append(ids, id)
		}
		pendingPermissions = map[string]bool{}
		pendingMu.Unlock()
		sort.Strings(ids)
		for _, id := range ids {
			writePermissionResponse(map[string]any{"request_id": id, "behavior": "deny", "message": message})
		}
	}

	if _, err := stdinW.WriteString(turnLine); err != nil {
		opts.stderrf("[agentproc runner] warning: turn write failed: %v", err)
	}
	if !profile.Permission {
		closeStdin()
	}

	// --- stdout pump: raw lines to the main loop, single-threaded handling ---
	lineCh := make(chan string, 256)
	stdoutStop := make(chan struct{})
	stdoutPumpDone := make(chan struct{})
	go func() {
		defer close(lineCh) // EOF signal for the main loop
		defer close(stdoutPumpDone)
		reader := bufio.NewReaderSize(stdoutR, 64*1024)
		for {
			line, rerr := reader.ReadString('\n')
			if len(line) > 0 {
				select {
				case lineCh <- line:
				case <-stdoutStop:
					return
				}
			}
			if rerr != nil {
				return
			}
		}
	}()

	// --- stderr pump: bounded head capture + live callback ---
	const stderrCap = 1 << 20 // 1 MB
	var stderrBuf []byte
	stderrTruncated := false
	var stderrMu sync.Mutex
	appendStderr := func(text string) {
		stderrMu.Lock()
		defer stderrMu.Unlock()
		if len(stderrBuf) < stderrCap {
			room := stderrCap - len(stderrBuf)
			if len(text) > room {
				text = text[:room]
			}
			stderrBuf = append(stderrBuf, text...)
		} else if !stderrTruncated {
			stderrBuf = append(stderrBuf, "\n[agentproc runner] stderr capped at 1 MB; trailing output dropped\n"...)
			stderrTruncated = true
		}
	}
	stderrStop := make(chan struct{})
	stderrDone := make(chan struct{})
	go func() {
		defer close(stderrDone)
		reader := bufio.NewReaderSize(stderrR, 64*1024)
		for {
			line, rerr := reader.ReadString('\n')
			if len(line) > 0 {
				select {
				case <-stderrStop:
					return
				default:
				}
				appendStderr(line)
				if opts.OnStderr != nil {
					opts.OnStderr(strings.TrimRight(line, "\r\n"))
				}
			}
			if rerr != nil {
				return
			}
		}
	}()

	// --- wait: the direct child's exit, delivered on one channel ---
	type waitInfo struct{ state *os.ProcessState }
	waitCh := make(chan waitInfo, 1)
	go func() {
		_ = cmd.Wait()
		waitCh <- waitInfo{state: cmd.ProcessState}
	}()

	// --- NDJSON turn state ---
	var resultText *string
	resultSeen := false
	errorSeen := false
	partialsForwarded := false
	var turnFinished atomic.Bool

	noteSessionID := func(sid any) {
		if turnFinished.Load() {
			return
		}
		s, ok := sid.(string)
		if !ok || s == "" {
			return
		}
		if !IsValidSessionIDString(s) {
			opts.stderrf("[agentproc runner] ignoring invalid session id %q (must be non-empty, no path separators or control chars); previous session id preserved", s)
			return
		}
		if result.SessionID == "" {
			result.SessionID = s
			if opts.OnSession != nil {
				opts.OnSession(s)
			}
			return
		}
		if s != result.SessionID {
			opts.stderrf("[agentproc runner] ignoring conflicting session_id %q; keeping first %q", s, result.SessionID)
		}
	}

	writeResponseForDecision := func(req map[string]any, decision *PermissionDecision) {
		if decision == nil {
			return
		}
		behavior := "deny"
		if decision.Behavior == "allow" {
			behavior = "allow"
		}
		response := map[string]any{
			"request_id": req["request_id"],
			"behavior":   behavior,
			"message":    decision.Message,
		}
		if decision.UpdatedInput != nil {
			response["updated_input"] = decision.UpdatedInput
		}
		writePermissionResponse(response)
	}

	handleLine := func(raw string) {
		if turnFinished.Load() {
			return
		}
		line := strings.TrimRight(raw, "\n")
		line = strings.TrimRight(line, "\r")
		c := ClassifyLine(line)
		if c.Kind != KindMalformed && opts.OnJournal != nil {
			trimmed := line
			if len(trimmed) > 2000 {
				trimmed = trimmed[:2000]
			}
			journal(JournalEntry{Seq: int(seq.Add(1)), Kind: c.Kind, Line: trimmed})
		}
		switch c.Kind {
		case KindPartial:
			noteSessionID(c.SessionID)
			// Post-error partials are discarded (not forwarded).
			if !errorSeen && streaming && opts.OnPartial != nil {
				opts.OnPartial(c.Value, c.Role)
				partialsForwarded = true
			}
			if opts.OnProtocolLine != nil {
				opts.OnProtocolLine(line)
			}
		case KindResult:
			switch {
			case errorSeen:
				// Cannot contribute to a failed turn's reply; usage still counts.
				if c.Usage != nil && result.Usage == nil {
					result.Usage = c.Usage
				}
			case resultSeen:
				opts.stderrf("[agentproc runner] ignoring extra {\"type\":\"result\"} (at most one per turn)")
			default:
				resultSeen = true
				noteSessionID(c.SessionID)
				text := c.Value
				resultText = &text
				if c.Usage != nil {
					result.Usage = c.Usage
				}
			}
			if opts.OnProtocolLine != nil {
				opts.OnProtocolLine(line)
			}
		case KindError:
			noteSessionID(c.SessionID)
			result.Error = c.Value
			errorSeen = true
			if c.Usage != nil {
				result.Usage = c.Usage
			}
			if opts.OnError != nil {
				opts.OnError(c.Value)
			}
			pendingMu.Lock()
			pendingPermissions = map[string]bool{}
			pendingMu.Unlock()
			if opts.OnProtocolLine != nil {
				opts.OnProtocolLine(line)
			}
		case KindPermissionRequest:
			if c.Obj != nil {
				noteSessionID(c.Obj["session_id"])
			}
			switch {
			case !profile.Permission:
				opts.stderrf("[agentproc runner] ignoring {\"type\":\"permission_request\"} (profile.permission is not true)")
			case !IsValidPermissionRequest(c.Obj):
				opts.stderrf("[agentproc runner] malformed permission_request: %q", truncForLog(line, 200))
				if c.Obj != nil {
					if rid, ok := c.Obj["request_id"].(string); ok && strings.TrimSpace(rid) != "" && !whitespaceControlRe.MatchString(rid) {
						writePermissionResponse(map[string]any{
							"request_id": rid,
							"behavior":   "deny",
							"message":    "malformed permission request",
						})
					}
				}
			default:
				if rid, ok := c.Obj["request_id"].(string); ok {
					pendingMu.Lock()
					pendingPermissions[rid] = true
					pendingMu.Unlock()
				}
				if opts.OnPermission != nil {
					// Dispatch off the main loop: a slow approver must not
					// stall event handling or the time limit (the reference
					// runners call on_permission on their drain thread).
					go func(req map[string]any) {
						defer func() {
							if r := recover(); r != nil {
								opts.stderrf("[agentproc runner] on_permission failed: %v", r)
								writeResponseForDecision(req, &PermissionDecision{Behavior: "deny", Message: "permission handler error"})
							}
						}()
						decision := opts.OnPermission(req)
						if turnFinished.Load() {
							return
						}
						writeResponseForDecision(req, decision)
					}(c.Obj)
				}
				// No OnPermission: leave the agent blocked until the turn timeout.
			}
			if opts.OnProtocolLine != nil {
				opts.OnProtocolLine(line)
			}
		default:
			// malformed: log + ignore (never the reply body in 0.4). Includes
			// legacy {"type":"session"} / {"type":"text"}.
			opts.stderrf("[agentproc runner] ignoring malformed stdout line: %q", truncForLog(line, 200))
			if opts.OnProtocolLine != nil {
				opts.OnProtocolLine(line)
			}
		}
	}

	// --- main loop ---
	var exitState *os.ProcessState
	childGone := false
	timedOut := false
	cancelled := false

	journalDecisions := true
	signalGroup := func(sig syscallSignal) {
		if journalDecisions {
			journal(JournalEntry{Seq: int(seq.Add(1)), Decision: decisionForSignal(sig)})
		}
		signalProcessGroup(cmd, sig)
	}
	killSequence := func(denyMessage string) {
		denyPending(denyMessage)
		closeStdin()
		signalGroup(sigTerm)
		grace := time.NewTimer(time.Duration(profile.KillGraceSecs) * time.Second)
		defer grace.Stop()
		select {
		case wi := <-waitCh:
			exitState = wi.state
			childGone = true
		case <-grace.C:
			signalGroup(sigKill)
			select {
			case wi := <-waitCh:
				exitState = wi.state
				childGone = true
			case <-time.After(2 * time.Second):
				opts.stderrf("[agentproc runner] warning: direct child pid=%d unreaped after timeout kill", cmd.Process.Pid)
			}
		}
	}

	var timeLimitCh <-chan time.Time
	if timeoutSecs > 0 || (profile.BudgetSecs != nil && *profile.BudgetSecs > 0) || deadlineEpoch > 0 {
		// A negative effective (a deadline already in the past) fires the
		// timer immediately — matching the reference runners' first-poll
		// expiry.
		timer := time.NewTimer(time.Duration(effectiveSecs * float64(time.Second)))
		defer timer.Stop()
		timeLimitCh = timer.C
	}
	var drainDeadline *time.Time
	drainCheck := time.NewTicker(50 * time.Millisecond)
	defer drainCheck.Stop()

loop:
	for {
		select {
		case line, ok := <-lineCh:
			if !ok {
				break loop // EOF: every writer is gone, the child's tail is in
			}
			handleLine(line)
		case wi := <-waitCh:
			exitState = wi.state
			childGone = true
			t := time.Now().Add(drainGrace)
			drainDeadline = &t
		case <-ctx.Done():
			cancelled = true
			journalDecisions = false // the cancel path records no journal decisions
			killSequence("cancelled")
			break loop
		case <-timeLimitCh:
			timedOut = true
			journal(JournalEntry{Seq: int(seq.Add(1)), Decision: "timeout", BudgetSecs: effectiveSecs})
			killSequence("permission timed out")
			break loop
		case <-drainCheck.C:
			if drainDeadline != nil && time.Now().After(*drainDeadline) {
				opts.stderrf("[agentproc runner] warning: agent exited but its stdout/stderr pipe is still open (held by a surviving grandchild); abandoning the drain")
				break loop
			}
		}
	}
	// Salvage window: after the direct child is reaped (or the kill gave up),
	// keep consuming the pump for at most drainGrace; a straggler that keeps
	// the pipe busy must not hold the turn open (spec "Never wait unboundedly
	// for inherited stdio").
	if !childGone {
		// EOF without a reap (possible only if stdout closed spuriously):
		// block until Wait returns, as the reference runners do.
		wi := <-waitCh
		exitState = wi.state
		childGone = true
	}
	salvageDeadline := time.Now().Add(drainGrace)
	for {
		select {
		case line, ok := <-lineCh:
			if !ok {
				goto salvaged
			}
			handleLine(line)
			if time.Now().After(salvageDeadline) {
				goto salvaged
			}
		case <-time.After(time.Until(salvageDeadline)):
			goto salvaged
		}
	}
salvaged:
	closeStdin()
	turnFinished.Store(true)
	close(stdoutStop)
	close(stderrStop)
	stdoutR.Close()
	stderrR.Close()
	select {
	case <-stdoutPumpDone:
	case <-time.After(drainGrace):
	}
	select {
	case <-stderrDone:
	case <-time.After(drainGrace):
	}

	// --- exit code ---
	exitCode := normalizeExitCode(exitState)

	// --- reply assembly (wire 0.4) ---
	switch {
	case streaming && partialsForwarded:
		result.Reply = "" // body already delivered via on_partial — never duplicated
	case resultText != nil:
		result.Reply = *resultText
	}

	// Non-zero exit with no error event: peek at the stderr head for common
	// "command/file not found" patterns and surface a friendly hint.
	stderrMu.Lock()
	stderrText := string(stderrBuf)
	stderrMu.Unlock()
	if !timedOut && !cancelled && result.Error == "" && exitCode != 0 {
		if hint := DiagnoseStderrFailure(stderrText); hint != "" {
			result.Error = hint
			if opts.OnError != nil {
				opts.OnError(hint)
			}
		}
	}

	switch {
	case cancelled:
		result.ExitCode = ExitCancelled
		if result.Error == "" {
			result.Error = "cancelled"
		}
	case timedOut:
		result.TimedOut = true
		result.ExitCode = ExitTimeout
	case result.Error != "":
		if exitCode == 0 {
			result.ExitCode = ExitError
		} else {
			result.ExitCode = exitCode
		}
	default:
		result.ExitCode = exitCode
	}

	if opts.RunLockKey != "" && !cancelled {
		ClearRunLock(opts.RunLockKey)
	}
	result.Duration = time.Since(startedMono).Seconds()
	return result, nil
}

func fatalResult(err error) (*RunResult, error) {
	return nil, err
}

func truncForLog(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

const drainGrace = 1 * time.Second

func envSlice(env map[string]string) []string {
	out := make([]string, 0, len(env))
	for k, v := range env {
		out = append(out, k+"="+v)
	}
	return out
}

// diagnoseSpawnError disambiguates the several conditions that surface as one
// spawn failure (mirrors the Python runner's _diagnose_spawn_error): a missing
// cwd, a bare command not on PATH, a pathed command that does not exist, or a
// file argument that does not exist.
func diagnoseSpawnError(err error, argv []string, cwd string) string {
	if cwd != "" {
		if info, statErr := os.Stat(cwd); statErr != nil || !info.IsDir() {
			return fmt.Sprintf("profile.cwd is not a directory: %s", cwd)
		}
	}
	if len(argv) == 0 {
		return ""
	}
	cmd := argv[0]
	isPathed := strings.ContainsAny(cmd, "/\\")
	if !isPathed && cmd != "" {
		if _, lookErr := exec.LookPath(cmd); lookErr != nil {
			return fmt.Sprintf("'%s' not found on PATH. Install it, or if it's installed, "+
				"make sure PATH is set correctly when the bridge spawns the agent.", cmd)
		}
	}
	if isPathed && cmd != "" {
		if _, statErr := os.Stat(cmd); statErr != nil {
			return fmt.Sprintf("command path does not exist or is not executable: %s", cmd)
		}
	}
	for _, a := range argv[1:] {
		if strings.HasPrefix(a, "-") {
			continue
		}
		if !strings.ContainsAny(a, "/\\") {
			continue
		}
		resolved := a
		if !filepath.IsAbs(a) && cwd != "" {
			resolved = filepath.Join(cwd, a)
		}
		if _, statErr := os.Stat(resolved); statErr != nil {
			return fmt.Sprintf("argument file not found: %s (resolved to %s). "+
				"The profile likely needs --cwd or the bundled script path is wrong.", a, resolved)
		}
	}
	_ = err
	return ""
}

// RunViaExecutor runs a turn through an in-process executor (no bridge
// subprocess). Mirrors the Python run_via_executor / Node runViaExecutor:
// same env composition, same posture refusals, same reply-assembly rules —
// only the target CLI is spawned directly by the runner.
func RunViaExecutor(ctx context.Context, executor Executor, opts *RunOptions, profile *Profile) *RunResult {
	if opts == nil {
		opts = &RunOptions{}
	}
	if profile == nil {
		profile = &Profile{
			Executor:      executor.CLIName(),
			Env:           map[string]string{},
			TimeoutSecs:   DefaultTimeoutSecs,
			KillGraceSecs: DefaultKillGraceSecs,
			Streaming:     true,
		}
	}
	cliName := executor.CLIName()
	result := &RunResult{ExitCode: ExitError, StartedAt: isoMillis(time.Now())}
	startedMono := time.Now()

	streaming := profile.Streaming
	if opts.Streaming != nil {
		streaming = *opts.Streaming
	}
	timeoutSecs := profile.TimeoutSecs
	if opts.TimeoutSecs != 0 {
		timeoutSecs = opts.TimeoutSecs
	}
	cwd := opts.Cwd
	if cwd == "" {
		cwd = profile.Cwd
	}
	if cwd != "" && !filepath.IsAbs(cwd) && opts.ProfileDir != "" {
		cwd = filepath.Join(opts.ProfileDir, cwd)
	}

	substCtx := map[string]string{
		"message":      opts.Message,
		"session_id":   opts.SessionID,
		"session_name": opts.sessionName(),
		"profile_dir":  opts.ProfileDir,
	}
	env := ComposeEnv(profile.Env, profile.EnvAllowlist, opts.ExtraEnv, substCtx, opts.OnStderr)

	handlers := resolveTurnHandlers(executor)

	fail := func(message string) *RunResult {
		result.Error = message
		result.Duration = time.Since(startedMono).Seconds()
		if opts.OnError != nil {
			opts.OnError(message)
		}
		return result
	}

	if handlers == nil || handlers.BuildArgs == nil {
		return fail(fmt.Sprintf("executor '%s' has no build_args", cliName))
	}
	argv, err := handlers.BuildArgs(opts.Message, opts.SessionID, env, TurnCtx{Permission: profile.Permission})
	if err != nil {
		return fail(fmt.Sprintf("executor '%s' build_args raised: %v", cliName, err))
	}
	if len(argv) == 0 {
		return fail(fmt.Sprintf("executor '%s' build_args returned empty argv", cliName))
	}

	// Optional stdin channel: an executor whose CLI reads its prompt from
	// stdin returns the payload here, keeping the user message out of argv.
	// Absent ⇒ the CLI's stdin is the null device.
	var initialStdin *string
	if handlers.BuildInitialStdin != nil {
		payload, err := handlers.BuildInitialStdin(opts.Message, opts.SessionID)
		if err != nil {
			return fail(fmt.Sprintf("executor '%s' build_initial_stdin raised: %v", cliName, err))
		}
		initialStdin = payload
	}

	if refusal := PostureRefusal(cliName, executor.SupportsPermission(), profile.Permission, AutoApproveEnabled(), argv); refusal != "" {
		return fail(refusal)
	}
	if _, lookErr := exec.LookPath(argv[0]); lookErr != nil {
		hint := executor.InstallHint()
		message := fmt.Sprintf("executor '%s': command not found: %s. %s", cliName, argv[0], hint)
		return fail(strings.TrimSpace(message))
	}

	plain := executor.Plain()
	if !plain && (handlers == nil || handlers.ParseEvent == nil) {
		return fail(fmt.Sprintf("executor '%s' has no parse_event for NDJSON mode", cliName))
	}

	child := exec.Command(argv[0], argv[1:]...)
	child.Dir = cwd
	child.Env = envSlice(env)
	configureProcessGroup(child)

	stdinR, stdinW, err := os.Pipe()
	if err != nil {
		return fail(err.Error())
	}
	stdoutR, stdoutW, err := os.Pipe()
	if err != nil {
		stdinR.Close()
		stdinW.Close()
		return fail(err.Error())
	}
	stderrR, stderrW, err := os.Pipe()
	if err != nil {
		stdinR.Close()
		stdinW.Close()
		stdoutR.Close()
		stdoutW.Close()
		return fail(err.Error())
	}
	if initialStdin != nil {
		child.Stdin = stdinR
	} else {
		child.Stdin = nil // null device
		stdinR.Close()
		stdinW.Close()
	}
	child.Stdout = stdoutW
	child.Stderr = stderrW

	if err := child.Start(); err != nil {
		for _, f := range []*os.File{stdoutR, stdoutW, stderrR, stderrW} {
			f.Close()
		}
		if initialStdin != nil {
			stdinR.Close()
			stdinW.Close()
		}
		hint := executor.InstallHint()
		message := fmt.Sprintf("executor '%s': command not found: %s. %s", cliName, argv[0], hint)
		return fail(strings.TrimSpace(message))
	}
	stdoutW.Close()
	stderrW.Close()

	if opts.RunLockKey != "" {
		WriteRunLock(opts.RunLockKey, child.Process.Pid, argv, 0)
	}

	// Stderr is drained in the background so the pipe can never fill and
	// deadlock; captured for the failure diagnostics below.
	var stderrBuf bytes.Buffer
	var stderrMu sync.Mutex
	stderrDone := make(chan struct{})
	go func() {
		defer close(stderrDone)
		reader := bufio.NewReaderSize(stderrR, 64*1024)
		for {
			line, rerr := reader.ReadString('\n')
			if len(line) > 0 {
				stderrMu.Lock()
				stderrBuf.WriteString(line)
				stderrMu.Unlock()
			}
			if rerr != nil {
				return
			}
		}
	}()

	lineCh := make(chan string, 256)
	pumpDone := make(chan struct{})
	go func() {
		defer close(lineCh)
		defer close(pumpDone)
		reader := bufio.NewReaderSize(stdoutR, 64*1024)
		for {
			line, rerr := reader.ReadString('\n')
			if len(line) > 0 {
				select {
				case lineCh <- line:
				case <-ctx.Done():
					return
				}
			}
			if rerr != nil {
				return
			}
		}
	}()

	if initialStdin != nil {
		go func() {
			stdinW.WriteString(*initialStdin + "\n")
			stdinW.Close()
		}()
	}

	var (
		resultSeen        bool
		partialsForwarded bool
		errorMessage      *string
		finalText         *string
		plainLines        []string
		replyParts        []string
	)
	handleLine := func(raw string) {
		line := strings.TrimRight(raw, "\n")
		line = strings.TrimRight(line, "\r")
		if line == "" {
			return
		}
		if plain {
			plainLines = append(plainLines, line)
			if opts.OnProtocolLine != nil {
				opts.OnProtocolLine(line)
			}
			return
		}
		if opts.OnProtocolLine != nil {
			opts.OnProtocolLine(line)
		}
		event := ParseJSONLine(line)
		if event == nil {
			return
		}
		parsed := handlers.ParseEvent(event)
		if parsed == nil {
			return
		}
		if parsed.SessionID != "" && IsValidSessionIDString(parsed.SessionID) && result.SessionID == "" {
			result.SessionID = parsed.SessionID
			if opts.OnSession != nil {
				opts.OnSession(parsed.SessionID)
			}
		}
		if parsed.Usage != nil && result.Usage == nil {
			result.Usage = parsed.Usage
		}
		if parsed.Error != "" {
			if errorMessage == nil {
				message := parsed.Error
				errorMessage = &message
			}
			return
		}
		if errorMessage != nil {
			return // post-error partial/result are suppressed
		}
		if parsed.PartialText != nil && *parsed.PartialText != "" {
			if streaming && opts.OnPartial != nil {
				// ParseResult has no role — the second argument is always nil.
				opts.OnPartial(*parsed.PartialText, nil)
				partialsForwarded = true
			}
			if !partialsForwarded {
				replyParts = append(replyParts, *parsed.PartialText)
			}
		}
		if parsed.FinalText != nil && !resultSeen {
			// First result wins — an explicit '' counts.
			resultSeen = true
			text := *parsed.FinalText
			finalText = &text
		}
	}

	exitState, timedOut, cancelled := waitExecutorProcess(ctx, child, time.Duration(timeoutSecs)*time.Second, profile.KillGraceSecs)

	// Drain window: the child is reaped (or the kill gave up) — its output is
	// all in the pipes; a straggler holding the write end must not hold the
	// turn open (same 1 s bound as the spawn path).
	drainDeadline := time.Now().Add(drainGrace)
drain:
	for {
		select {
		case line, ok := <-lineCh:
			if !ok {
				break drain
			}
			handleLine(line)
			if time.Now().After(drainDeadline) {
				break drain
			}
		case <-time.After(time.Until(drainDeadline)):
			break drain
		}
	}
	// The direct child is gone: release the pipe read ends so a straggler
	// cannot leak an fd per turn, and join the pumps with the same backstop
	// as the spawn path.
	stdoutR.Close()
	stderrR.Close()
	select {
	case <-pumpDone:
	case <-time.After(drainGrace):
	}
	select {
	case <-stderrDone:
	case <-time.After(drainGrace):
	}

	exitCode := normalizeExitCode(exitState)

	if plain {
		result.Reply = strings.TrimSpace(strings.Join(plainLines, "\n"))
	} else {
		switch {
		case cancelled:
			result.Reply = valueOrJoin(finalText, replyParts)
		case timedOut:
			result.Reply = valueOrJoin(finalText, replyParts)
		case streaming && partialsForwarded:
			result.Reply = ""
		case finalText != nil:
			result.Reply = *finalText
		}
	}

	stderrMu.Lock()
	stderrText := stderrBuf.String()
	stderrMu.Unlock()
	stderrText = strings.TrimSpace(stderrText)

	if opts.RunLockKey != "" && !timedOut && !cancelled {
		ClearRunLock(opts.RunLockKey)
	}

	if cancelled {
		result.ExitCode = ExitCancelled
		result.Duration = time.Since(startedMono).Seconds()
		return fail(fmt.Sprintf("executor '%s' cancelled", cliName))
	}
	if timedOut {
		result.TimedOut = true
		result.Error = fmt.Sprintf("executor '%s' timed out after %ds", cliName, timeoutSecs)
		result.ExitCode = ExitTimeout
		result.Duration = time.Since(startedMono).Seconds()
		if opts.OnError != nil {
			opts.OnError(result.Error)
		}
		return result
	}
	if errorMessage != nil {
		result.Error = *errorMessage
		if exitCode == 0 {
			result.ExitCode = ExitError
		} else {
			result.ExitCode = exitCode
		}
		result.Duration = time.Since(startedMono).Seconds()
		if opts.OnError != nil {
			opts.OnError(result.Error)
		}
		return result
	}
	if exitCode != 0 {
		result.Error = fmt.Sprintf("executor '%s' exited %d: %s", cliName, exitCode, ExtractFailureDiagnostics(stderrText, failureTailLines))
		result.ExitCode = exitCode
		result.Duration = time.Since(startedMono).Seconds()
		if opts.OnError != nil {
			opts.OnError(result.Error)
		}
		return result
	}
	if plain {
		if result.Reply == "" {
			result.Error = fmt.Sprintf("%s returned empty output", cliName)
			result.ExitCode = ExitError
			result.Duration = time.Since(startedMono).Seconds()
			if opts.OnError != nil {
				opts.OnError(result.Error)
			}
			return result
		}
		// Plain executors that manage a session id expose get_session_id so
		// the runner can surface it after the process exits.
		if handlers != nil && handlers.GetSessionID != nil {
			sid := handlers.GetSessionID()
			if sid != "" && IsValidSessionIDString(sid) && result.SessionID == "" {
				result.SessionID = sid
				if opts.OnSession != nil {
					opts.OnSession(sid)
				}
			}
		}
	}
	// An NDJSON turn that exited 0 with nothing recognisable is a SUCCESS
	// with an empty reply (spec; scenarios.json "empty output → empty reply").
	result.ExitCode = ExitSuccess
	result.Duration = time.Since(startedMono).Seconds()
	return result
}

func valueOrJoin(final *string, parts []string) string {
	if final != nil {
		return *final
	}
	return strings.Join(parts, "")
}

// waitExecutorProcess waits for the executor's CLI, enforcing the optional
// timeout and the cooperative cancel with the graded process-group kill
// (SIGTERM → kill grace → SIGKILL). Returns (state, timedOut, cancelled).
func waitExecutorProcess(ctx context.Context, child *exec.Cmd, timeout time.Duration, killGraceSecs int) (state *os.ProcessState, timedOut bool, cancelled bool) {
	type waitInfo struct{ state *os.ProcessState }
	waitCh := make(chan waitInfo, 1)
	go func() {
		_ = child.Wait()
		waitCh <- waitInfo{state: child.ProcessState}
	}()

	killSequence := func() {
		signalProcessGroup(child, sigTerm)
		grace := time.NewTimer(time.Duration(killGraceSecs) * time.Second)
		defer grace.Stop()
		select {
		case wi := <-waitCh:
			state = wi.state
		case <-grace.C:
			signalProcessGroup(child, sigKill)
			select {
			case wi := <-waitCh:
				state = wi.state
			case <-time.After(2 * time.Second):
			}
		}
	}

	var timeoutCh <-chan time.Time
	if timeout > 0 {
		timer := time.NewTimer(timeout)
		defer timer.Stop()
		timeoutCh = timer.C
	}
	for {
		select {
		case wi := <-waitCh:
			return wi.state, false, false
		case <-ctx.Done():
			cancelled = true
			killSequence()
			return state, false, true
		case <-timeoutCh:
			timedOut = true
			killSequence()
			return state, true, false
		}
	}
}
