package agentproc

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func lockTestEnv(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv(RunLockDirEnv, dir)
	return dir
}

func TestRunLockWriteClear(t *testing.T) {
	dir := lockTestEnv(t)
	key := filepath.Join(dir, "workspace")
	WriteRunLock(key, 4242, []string{"/usr/bin/env", "sleep", "30"}, 0)
	path := LockPathFor(key)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("lock file missing: %v", err)
	}
	var rec map[string]any
	if err := json.Unmarshal(raw, &rec); err != nil {
		t.Fatalf("lock is not JSON: %v", err)
	}
	if rec["pid"].(float64) != 4242 || rec["command"] != "env" {
		t.Fatalf("unexpected record: %s", raw)
	}
	// The spawner defaults to this process — the takeover probe needs it.
	if int(rec["spawner"].(float64)) != os.Getpid() {
		t.Fatalf("spawner %v, expected our pid", rec["spawner"])
	}
	ClearRunLock(key)
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("lock not cleared")
	}
	// Key hashing is stable across absolute/relative spellings.
	if LockPathFor(key) != LockPathFor(absPath(key)) {
		t.Fatal("lock path must hash the absolute key")
	}
}

func TestCleanupStaleRunClean(t *testing.T) {
	lockTestEnv(t)
	res, err := CleanupStaleRun(filepath.Join(t.TempDir(), "nothing"), 1)
	if err != nil || res.Action != "clean" {
		t.Fatalf("expected clean, got %+v err=%v", res, err)
	}
}

func TestCleanupStaleRunStalePidGone(t *testing.T) {
	dir := lockTestEnv(t)
	key := filepath.Join(dir, "ws")
	// Spawn-and-exit yields a real dead pid.
	dead := exec.Command("true")
	if err := dead.Start(); err != nil {
		t.Fatal(err)
	}
	pid := dead.Process.Pid
	_, _ = dead.Process.Wait()
	WriteRunLock(key, pid, []string{"true"}, os.Getpid())
	res, err := CleanupStaleRun(key, 1)
	if err != nil || res.Action != "stale" || res.Reason != "pid-gone" {
		t.Fatalf("expected stale/pid-gone, got %+v err=%v", res, err)
	}
	if _, err := os.Stat(LockPathFor(key)); !os.IsNotExist(err) {
		t.Fatal("stale lock must be removed")
	}
}

func TestCleanupStaleRunBusyWhileHostAlive(t *testing.T) {
	dir := lockTestEnv(t)
	key := filepath.Join(dir, "ws")
	// A real process group led by `sleep`, spawned by a LIVE host (this test
	// process): identity verifies, spawner alive → Busy, never killed.
	cmd := exec.Command("sleep", "30")
	setTestpgid(cmd)
	if err := cmd.Start(); err != nil {
		t.Skipf("cannot spawn sleep: %v", err)
	}
	defer killTestGroup(cmd)
	WriteRunLock(key, cmd.Process.Pid, []string{"/bin/sleep", "30"}, os.Getpid())
	res, err := CleanupStaleRun(key, 1)
	if err == nil {
		t.Fatalf("expected RunLockBusy, got %+v", res)
	}
	busy, ok := err.(*RunLockBusy)
	if !ok {
		t.Fatalf("expected *RunLockBusy, got %T: %v", err, err)
	}
	if !strings.Contains(busy.Message, "拒绝接管") {
		t.Fatalf("unexpected busy message: %s", busy.Message)
	}
	// The healthy agent must still be alive and the lock kept.
	if !pidAlive(cmd.Process.Pid) {
		t.Fatal("RunLockBusy path must not kill the agent")
	}
	if _, err := os.Stat(LockPathFor(key)); err != nil {
		t.Fatal("RunLockBusy path must keep the lock")
	}
}

func TestCleanupStaleRunKillsOrphan(t *testing.T) {
	dir := lockTestEnv(t)
	key := filepath.Join(dir, "ws")
	// Orphan shape: a live group leader whose spawner pid is dead. Spawn a
	// short-lived host first to obtain a genuinely dead spawner pid.
	host := exec.Command("true")
	if err := host.Start(); err != nil {
		t.Fatal(err)
	}
	deadSpawner := host.Process.Pid
	_, _ = host.Process.Wait()

	cmd := exec.Command("sleep", "30")
	setTestpgid(cmd)
	if err := cmd.Start(); err != nil {
		t.Skipf("cannot spawn sleep: %v", err)
	}
	defer killTestGroup(cmd)
	// A grandchild in the group must die with the leader.
	grandchild := exec.Command("sleep", "30")
	setTestpgidJoin(cmd.Process.Pid, grandchild)
	if err := grandchild.Start(); err != nil {
		t.Skipf("cannot spawn grandchild: %v", err)
	}

	WriteRunLock(key, cmd.Process.Pid, []string{"/bin/sleep", "30"}, deadSpawner)
	res, err := CleanupStaleRun(key, 10)
	if err != nil || res.Action != "killed" {
		t.Fatalf("expected killed, got %+v err=%v", res, err)
	}
	// Both were our children: SIGKILL leaves zombies until reaped, and a
	// zombie still answers signal-0 (same distinction the lock itself makes
	// via the ps stat probe).
	_, _ = cmd.Process.Wait()
	_, _ = grandchild.Process.Wait()
	if pidAlive(cmd.Process.Pid) {
		t.Fatal("orphan leader still alive after cleanup")
	}
	if pidAlive(grandchild.Process.Pid) {
		t.Fatal("grandchild survived the group kill")
	}
	if _, err := os.Stat(LockPathFor(key)); !os.IsNotExist(err) {
		t.Fatal("lock must be cleared after a killed cleanup")
	}
}

func TestCleanupStaleRunPidReused(t *testing.T) {
	dir := lockTestEnv(t)
	key := filepath.Join(dir, "ws")
	// Record points at a live pid whose identity cannot match (this test
	// process is not a group leader named `env`): stale/pid-reused.
	WriteRunLock(key, os.Getpid(), []string{"/usr/bin/env", "sleep"}, 12345)
	res, err := CleanupStaleRun(key, 1)
	if err != nil || res.Action != "stale" || res.Reason != "pid-reused" {
		t.Fatalf("expected stale/pid-reused, got %+v err=%v", res, err)
	}
}

func TestCleanupStaleRunMissingSpawnerRefused(t *testing.T) {
	dir := lockTestEnv(t)
	key := filepath.Join(dir, "ws")
	// Old-format record without a spawner field → unverifiable → Busy.
	cmd := exec.Command("sleep", "30")
	setTestpgid(cmd)
	if err := cmd.Start(); err != nil {
		t.Skipf("cannot spawn sleep: %v", err)
	}
	defer killTestGroup(cmd)
	rec := map[string]any{"key": key, "pid": cmd.Process.Pid, "command": "sleep"}
	raw, _ := json.Marshal(rec)
	if err := os.MkdirAll(filepath.Dir(LockPathFor(key)), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(LockPathFor(key), raw, 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := CleanupStaleRun(key, 1)
	if _, ok := err.(*RunLockBusy); !ok {
		t.Fatalf("expected RunLockBusy for a spawner-less record, got %v", err)
	}
}

func TestRunLockKeyWiredIntoRunner(t *testing.T) {
	requireExecable(t)
	lockTestEnv(t)
	dir := t.TempDir()
	agent := filepath.Join(dir, "agent.sh")
	if err := os.WriteFile(agent, []byte("#!/usr/bin/env bash\necho '{\"type\":\"result\",\"text\":\"ok\"}'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	ws := filepath.Join(dir, "workspace")
	result, err := Run(context.Background(), map[string]any{"command": agent}, &RunOptions{
		Message:    "hi",
		RunLockKey: ws,
	})
	if err != nil || result.Reply != "ok" {
		t.Fatalf("run: %+v err=%v", result, err)
	}
	if _, err := os.Stat(LockPathFor(ws)); !os.IsNotExist(err) {
		t.Fatal("lock must be cleared after a successful turn")
	}
}

// setTestpgid starts cmd in its own process group (the orphan shape).
func setTestpgid(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// setTestpgidJoin joins an existing process group (the grandchild shape).
func setTestpgidJoin(pgid int, cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
	cmd.SysProcAttr.Pgid = pgid
}

func killTestGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	_, _ = cmd.Process.Wait()
}
