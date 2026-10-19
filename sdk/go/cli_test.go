package agentproc

// End-to-end tests for the cmd/agentproc CLI: built once, driven against
// local profile YAMLs with a bash echo bridge. The hub subcommands are
// covered by hub_test.go through the library; here we assert the CLI's
// flag surface, output semantics (reply → stdout, events → stderr,
// agentproc:session trailer) and exit codes.

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func buildCLI(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "agentproc")
	cmd := exec.Command("go", "build", "-o", bin, "./cmd/agentproc")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build CLI: %v\n%s", err, out)
	}
	return bin
}

// echoBridge is the local smoke profile: a bash agent that echoes the turn
// message back as the reply body (the same shape as hub/echo-agent).
func echoBridge(t *testing.T, dir string) string {
	t.Helper()
	bridge := filepath.Join(dir, "bridge.sh")
	if err := os.WriteFile(bridge, []byte(
		"#!/usr/bin/env bash\n"+
			"read -r line\n"+
			"message=$(printf '%s' \"$line\" | sed -n 's/.*\"message\":\"\\([^\"]*\\)\".*/\\1/p')\n"+
			"printf '{\"type\":\"partial\",\"text\":\"echo: \"}\\n'\n"+
			"printf '{\"type\":\"result\",\"text\":\"echo: %s\",\"session_id\":\"cli-1\"}\\n' \"$message\"\n",
	), 0o755); err != nil {
		t.Fatal(err)
	}
	profile := filepath.Join(dir, "profile.yaml")
	if err := os.WriteFile(profile, []byte(
		"command: bash\n"+
			"args: [\""+bridge+"\"]\n"+
			"timeout_secs: 30\n",
	), 0o644); err != nil {
		t.Fatal(err)
	}
	return profile
}

func runCLI(t *testing.T, bin string, args ...string) (string, string, int) {
	t.Helper()
	cmd := exec.Command(bin, args...)
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	code := 0
	if exitErr, ok := err.(*exec.ExitError); ok {
		code = exitErr.ExitCode()
	} else if err != nil {
		t.Fatalf("run CLI: %v", err)
	}
	return stdout.String(), stderr.String(), code
}

func TestCLIVersionAndHelp(t *testing.T) {
	requireExecable(t)
	bin := buildCLI(t)
	out, _, code := runCLI(t, bin, "--version")
	if code != 0 || !strings.Contains(out, "agentproc "+SDKVersion) || !strings.Contains(out, "protocol "+PROTOCOL_VERSION) {
		t.Fatalf("version: %q exit=%d", out, code)
	}
	out, _, code = runCLI(t, bin, "--help")
	if code != 0 || !strings.Contains(out, "--profile") || !strings.Contains(out, "hub list") {
		t.Fatalf("help: %q exit=%d", out, code)
	}
	_, _, code = runCLI(t, bin)
	if code != 2 {
		t.Fatalf("no args must be a usage error (2), got %d", code)
	}
}

func TestCLIRunLocalProfile(t *testing.T) {
	requireExecable(t)
	bin := buildCLI(t)
	dir := t.TempDir()
	profile := echoBridge(t, dir)

	// Default (verbose, streaming): events are echoed to stderr; the reply
	// body stays empty on stdout because a partial was forwarded (spec reply
	// assembly — the Python CLI behaves the same way).
	out, stderr, code := runCLI(t, bin, "--profile", profile, "--prompt", "hello world")
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, stderr)
	}
	if !strings.Contains(stderr, `"type":"partial"`) {
		t.Fatalf("partial event not forwarded to stderr: %q", stderr)
	}
	if !strings.Contains(stderr, "agentproc:session:cli-1") {
		t.Fatalf("session trailer missing: %q", stderr)
	}

	// --no-stream: partials are suppressed, so the body lands on stdout.
	out, _, code = runCLI(t, bin, "--profile", profile, "--prompt", "hello world", "--no-stream")
	if code != 0 {
		t.Fatalf("no-stream exit=%d", code)
	}
	if !strings.Contains(out, "echo: hello world") {
		t.Fatalf("reply missing from stdout: %q", out)
	}
}

func TestCLIRawModeAndStdinPrompt(t *testing.T) {
	requireExecable(t)
	bin := buildCLI(t)
	dir := t.TempDir()
	profile := echoBridge(t, dir)

	// --raw --no-stream: only the reply body on stdout, no event echo.
	out, stderr, code := runCLI(t, bin, "--profile", profile, "--prompt", "raw hi", "--raw", "--no-stream")
	if code != 0 || out != "echo: raw hi\n" {
		t.Fatalf("raw: out=%q code=%d stderr=%s", out, code, stderr)
	}
	if strings.Contains(stderr, `"type":"partial"`) {
		t.Fatalf("raw mode must not echo events: %q", stderr)
	}

	// --stdin: the prompt arrives on stdin.
	cmd := exec.Command(bin, "--profile", profile, "--stdin", "--no-stream")
	cmd.Stdin = strings.NewReader("stdin prompt\n")
	var stdout strings.Builder
	cmd.Stdout = &stdout
	if err := cmd.Run(); err != nil {
		t.Fatalf("stdin run: %v", err)
	}
	if !strings.Contains(stdout.String(), "echo: stdin prompt") {
		t.Fatalf("stdin prompt reply: %q", stdout.String())
	}
}

func TestCLIMissingProfileAndEnvError(t *testing.T) {
	requireExecable(t)
	bin := buildCLI(t)
	_, _, code := runCLI(t, bin, "--profile", filepath.Join(t.TempDir(), "nope.yaml"), "--prompt", "hi")
	if code != 2 {
		t.Fatalf("missing profile must be a usage error (2), got %d", code)
	}
	dir := t.TempDir()
	profile := echoBridge(t, dir)
	_, stderr, code := runCLI(t, bin, "--profile", profile, "--prompt", "hi", "--env", "NO_EQUALS")
	if code != 2 || !strings.Contains(stderr, "KEY=VALUE") {
		t.Fatalf("--env validation: code=%d stderr=%s", code, stderr)
	}
}

func TestCLIJournalFile(t *testing.T) {
	requireExecable(t)
	bin := buildCLI(t)
	dir := t.TempDir()
	profile := echoBridge(t, dir)
	journal := filepath.Join(dir, "journal.jsonl")
	_, _, code := runCLI(t, bin, "--profile", profile, "--prompt", "j", "--journal", journal, "--quiet")
	if code != 0 {
		t.Fatalf("exit=%d", code)
	}
	raw, err := os.ReadFile(journal)
	if err != nil {
		t.Fatalf("journal missing: %v", err)
	}
	if !strings.Contains(string(raw), `"kind":"partial"`) || !strings.Contains(string(raw), `"kind":"result"`) {
		t.Fatalf("journal lacks classified events: %s", raw)
	}
}

func TestCLIHubUnknownSubcommand(t *testing.T) {
	requireExecable(t)
	bin := buildCLI(t)
	_, stderr, code := runCLI(t, bin, "hub", "frobnicate")
	if code != 2 || !strings.Contains(stderr, "unknown hub subcommand") {
		t.Fatalf("hub frobnicate: code=%d stderr=%s", code, stderr)
	}
	out, _, code := runCLI(t, bin, "hub", "--help")
	if code != 0 || !strings.Contains(out, "hub run") {
		t.Fatalf("hub help: %q exit=%d", out, code)
	}
}
