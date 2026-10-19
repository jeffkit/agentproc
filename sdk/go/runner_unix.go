//go:build !windows

package agentproc

import (
	"os"
	"os/exec"
	"syscall"
)

// syscallSignal abstracts syscall.Signal so the runner code reads the same on
// every platform.
type syscallSignal = syscall.Signal

const (
	sigTerm syscallSignal = syscall.SIGTERM
	sigKill syscallSignal = syscall.SIGKILL
)

func decisionForSignal(sig syscallSignal) string {
	if sig == syscall.SIGKILL {
		return "sigkill_process_group"
	}
	return "sigterm_process_group"
}

// configureProcessGroup puts the child in its own process group (the Go
// equivalent of start_new_session=True) so a group kill reaches the whole
// agent subtree — helpers, builds, sub-agents (spec "Kill the tree, not the
// process").
func configureProcessGroup(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// signalProcessGroup signals the agent's whole process group, falling back to
// the direct child when the group probe fails (mirrors the reference
// runners' best-effort killpg).
func signalProcessGroup(cmd *exec.Cmd, sig syscallSignal) {
	if cmd.Process == nil {
		return
	}
	pgid := -cmd.Process.Pid
	if pg, err := syscall.Getpgid(cmd.Process.Pid); err == nil {
		pgid = -pg
	}
	if err := syscall.Kill(pgid, sig); err == nil {
		return
	}
	_ = cmd.Process.Signal(sig)
}

const processGroupsSupported = true

const syscallZero = syscall.Signal(0)

// killGroup SIGKILLs a verified process group (the recorded pid IS the group
// leader). A gone group (ESRCH) is success.
func killGroup(pgid int) error {
	err := syscall.Kill(-pgid, syscall.SIGKILL)
	if err == syscall.ESRCH {
		return nil
	}
	return err
}

// normalizeExitCode normalises a child exit status to a spec exit code.
// POSIX wait reports signal deaths via WaitStatus.Signaled — normalise to
// 128 + signo (SIGINT → 130, SIGTERM → 143). A missing status (no Wait)
// maps to ExitError.
func normalizeExitCode(state *os.ProcessState) int {
	if state == nil {
		return ExitError
	}
	if ws, ok := state.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return 128 + int(ws.Signal())
	}
	return state.ExitCode()
}
