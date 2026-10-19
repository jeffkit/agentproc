package agentproc

// Workspace run lock — the "last will" record of a running agent process
// group, for kill-before-start orphan cleanup. Port of the Python SDK's
// run_lock.py (keeper-migration design G3).
//
// Problem: when the host (worker/bridge) is hard-killed (SIGKILL / OOM), the
// agent CLI survives in its own process group with nobody left to reap it —
// it keeps running and writing the workspace while a takeover attempt starts
// a second agent on the same tree. The fix is kill-before-start, not
// die-with-me cleanup (that would need a trigger living outside the host).
//
// Two halves:
//   - spawn side (WriteRunLock / ClearRunLock): after a successful Start,
//     record pid + command + the spawner pid as plain JSON. Normal end
//     clears it; a hard-killed host leaves the file behind as the will.
//   - takeover side (CleanupStaleRun): probed before a new agent starts on
//     the same key (usually the workspace absolute path). pid dead → stale,
//     release. pid alive AND identity verified (pgid == pid and the command
//     line contains the recorded argv[0] base name) → look at the recorded
//     spawner: dead → orphan, kill the whole group, wait, release; alive →
//     not an orphan, refuse with RunLockBusy (issue #9: never kill a healthy
//     agent whose host is still running). Identity unverifiable → refuse.
//
// Design choices mirrored from the Python module:
//   - A plain file, not flock: kernel locks die with the holder, which is
//     exactly the process that is gone. The file + pid probe yields "who to
//     kill".
//   - Central directory (~/.agentproc/run-locks/<sha256(key)>.json) instead
//     of inside the workspace: keeps git worktrees clean. AGENTPROC_RUN_LOCK_DIR
//     overrides (tests).
//   - Not clearing on early return is self-healing: every early return
//     happens after the child is dead, so the leftover pid is stale and gets
//     swept next time.
//   - Two-factor identity verification guards against pid reuse; anything
//     that does not match is treated as stale, never blind-killed.
//   - Old records without a spawner field are unverifiable → RunLockBusy.
//     No ppid==1 adoption heuristics in the kill decision.
//   - Windows has no process groups: CleanupStaleRun reports "unsupported"
//     (non-blocking); lock read/write work as usual.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// RunLockDirEnv overrides the lock directory (tests / sandboxed hosts).
const RunLockDirEnv = "AGENTPROC_RUN_LOCK_DIR"

// RunLockBusy is returned when the workspace appears to still have a live,
// identity-verified agent running — refuse to start (fail-safe).
type RunLockBusy struct {
	Key     string
	Message string
}

func (e *RunLockBusy) Error() string { return e.Message }

func runLockDir() string {
	if dir := os.Getenv(RunLockDirEnv); dir != "" {
		return dir
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".agentproc/run-locks"
	}
	return filepath.Join(home, ".agentproc", "run-locks")
}

// LockPathFor resolves the lock file path for a key (the workspace absolute
// path), hashed into the central lock directory.
func LockPathFor(key string) string {
	abs, err := filepath.Abs(key)
	if err != nil {
		abs = key
	}
	digest := sha256.Sum256([]byte(abs))
	name := hex.EncodeToString(digest[:])[:32]
	return filepath.Join(runLockDir(), name+".json")
}

type runLockRecord struct {
	Key       string `json:"key"`
	PID       int    `json:"pid"`
	Command   string `json:"command"`
	StartedAt string `json:"started_at"`
	Spawner   int    `json:"spawner"`
}

// WriteRunLock records a running agent (best-effort: a failure only costs
// orphan visibility, never blocks the turn). spawner <= 0 means this
// process — it must be the one that spawned the agent, so a takeover can ask
// "is the host that launched it still alive?"
func WriteRunLock(key string, pid int, argv []string, spawner int) {
	command := ""
	if len(argv) > 0 {
		command = filepath.Base(argv[0])
	}
	if spawner <= 0 {
		spawner = os.Getpid()
	}
	rec := runLockRecord{
		Key:       absPath(key),
		PID:       pid,
		Command:   command,
		StartedAt: time.Now().Format("2006-01-02T15:04:05"),
		Spawner:   spawner,
	}
	path := LockPathFor(key)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	raw, err := json.Marshal(rec)
	if err != nil {
		return
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".runlock-*.tmp")
	if err != nil {
		return
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(raw); err == nil {
		_ = tmp.Close()
		_ = os.Rename(tmpName, path)
	} else {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
	}
}

// ClearRunLock removes the lock on normal completion (best-effort).
func ClearRunLock(key string) {
	_ = os.Remove(LockPathFor(key))
}

func absPath(p string) string {
	if abs, err := filepath.Abs(p); err == nil {
		return abs
	}
	return p
}

func pidAlive(pid int) bool {
	// signal 0 probes existence without delivering anything; on Windows
	// Process.Signal(0) is unsupported, so go through FindProcess + Signal.
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return p.Signal(syscallZero) == nil
}

// pidGone waits until the process no longer exists (a reaped zombie counts —
// it writes nothing anymore).
func pidGone(pid int, graceSecs float64) bool {
	deadline := time.Now().Add(time.Duration(graceSecs * float64(time.Second)))
	for time.Now().Before(deadline) {
		if !pidAlive(pid) {
			return true
		}
		if state, ok := psStat(pid); ok && strings.HasPrefix(state, "Z") {
			return true
		}
		time.Sleep(200 * time.Millisecond)
	}
	return !pidAlive(pid)
}

// CleanupStaleRunResult is the outcome of a kill-before-start probe.
type CleanupStaleRunResult struct {
	Action  string // clean | stale | killed | unsupported
	Reason  string `json:",omitempty"`
	PID     int    `json:",omitempty"`
	Command string `json:",omitempty"`
}

// CleanupStaleRun probes and cleans the orphan agent process group recorded
// for key (kill-before-start). Ambiguous states (identity unverifiable /
// refused kill / still alive after SIGKILL) return *RunLockBusy — the lock is
// kept, nothing is blind-killed.
func CleanupStaleRun(key string, graceSecs float64) (CleanupStaleRunResult, error) {
	if !processGroupsSupported {
		return CleanupStaleRunResult{Action: "unsupported"}, nil
	}
	path := LockPathFor(key)
	if _, err := os.Stat(path); err != nil {
		return CleanupStaleRunResult{Action: "clean"}, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return CleanupStaleRunResult{Action: "clean"}, nil
	}
	var rec runLockRecord
	if err := json.Unmarshal(raw, &rec); err != nil || rec.PID <= 0 {
		_ = os.Remove(path)
		return CleanupStaleRunResult{Action: "stale", Reason: "corrupt"}, nil
	}
	pid := rec.PID
	command := rec.Command

	if !pidAlive(pid) {
		_ = os.Remove(path)
		return CleanupStaleRunResult{Action: "stale", Reason: "pid-gone", PID: pid}, nil
	}

	pgidNow, cmdNow, ok := psFields(pid)
	if !ok {
		return CleanupStaleRunResult{}, &RunLockBusy{
			Key: absPath(key),
			Message: fmt.Sprintf("workspace %q 的遗言锁指向存活 pid %d，但身份无法核实（ps 不可用）——拒绝开工以免并发写；请人工确认后删除锁文件 %s",
				absPath(key), pid, path),
		}
	}
	if strconv.Itoa(pgidNow) != strconv.Itoa(pid) || (command != "" && !strings.Contains(cmdNow, command)) {
		_ = os.Remove(path)
		return CleanupStaleRunResult{Action: "stale", Reason: "pid-reused", PID: pid}, nil
	}

	// Takeover requires the spawner (the host that launched this agent) to be
	// dead; a missing/unparseable field is unverifiable → refuse.
	if rec.Spawner <= 0 {
		return CleanupStaleRunResult{}, &RunLockBusy{
			Key: absPath(key),
			Message: fmt.Sprintf("workspace %q 的遗言锁无 spawner(worker) 记录（旧格式或字段损坏），无法核实派它的 worker 是否已死——不杀不放行；请人工确认后删除锁文件 %s",
				absPath(key), path),
		}
	}
	if pidAlive(rec.Spawner) {
		return CleanupStaleRunResult{}, &RunLockBusy{
			Key: absPath(key),
			Message: fmt.Sprintf("workspace %q 的 agent pid %d 仍在跑，且派它的 worker pid %d 仍存活——这不是孤儿，拒绝接管",
				absPath(key), pid, rec.Spawner),
		}
	}

	// Identity verified and the spawner is gone: a true orphan of this
	// mechanism → kill the whole group, wait, release.
	if err := killGroup(pid); err != nil {
		return CleanupStaleRunResult{}, &RunLockBusy{
			Key:     absPath(key),
			Message: fmt.Sprintf("workspace %q 孤儿 pid %d 身份已核实但 killpg 被拒：%v", absPath(key), pid, err),
		}
	}
	if !pidGone(pid, graceSecs) {
		return CleanupStaleRunResult{}, &RunLockBusy{
			Key:     absPath(key),
			Message: fmt.Sprintf("workspace %q 孤儿 pid %d SIGKILL 后 %gs 仍存活（D 状态?）——拒绝开工", absPath(key), pid, graceSecs),
		}
	}
	_ = os.Remove(path)
	return CleanupStaleRunResult{Action: "killed", Reason: "spawner-gone", PID: pid, Command: command}, nil
}

// psFields returns (pgid, command) for pid via ps(1); ok=false when ps is
// unavailable — callers must not make kill decisions on that.
func psFields(pid int) (int, string, bool) {
	out, err := exec.Command("ps", "-o", "pgid=,command=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return 0, "", false
	}
	line := strings.TrimSpace(string(out))
	parts := strings.SplitN(line, " ", 2)
	if len(parts) != 2 {
		return 0, "", false
	}
	pgid, err := strconv.Atoi(strings.TrimSpace(parts[0]))
	if err != nil {
		return 0, "", false
	}
	return pgid, strings.TrimSpace(parts[1]), true
}

// psStat returns the process state letter (first column of `ps -o stat=`).
func psStat(pid int) (string, bool) {
	out, err := exec.Command("ps", "-o", "stat=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(out)), true
}
