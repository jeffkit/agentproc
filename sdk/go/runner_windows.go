//go:build windows

package agentproc

import (
	"os"
	"os/exec"
	"syscall"
)

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

// configureProcessGroup: Windows has no process groups — the bridge degrades
// to terminating the direct process (spec "Kill the tree, not the process",
// Windows caveat).
func configureProcessGroup(cmd *exec.Cmd) {}

// signalProcessGroup degrades to hard-terminating the direct process:
// TerminateProcess is the only available lever on Windows, so the grace
// period collapses in practice.
func signalProcessGroup(cmd *exec.Cmd, _ syscallSignal) {
	if cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
}

const processGroupsSupported = false

const syscallZero = syscall.Signal(0)

// killGroup is unreachable on Windows (CleanupStaleRun reports unsupported).
func killGroup(pgid int) error { return syscall.Errno(1) }

// normalizeExitCode: Windows return codes are never negative, so the mapping
// is a no-op (mirrors the Python runner).
func normalizeExitCode(state *os.ProcessState) int {
	if state == nil {
		return ExitError
	}
	return state.ExitCode()
}
