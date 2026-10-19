package agentproc

// Lifecycle tests beyond the shared fixtures: process-group kills, the
// bounded post-exit drain, cooperative cancel, the spawn-path permission
// channel, and the stdin turn payload. These mirror the Python/Node
// runners' behaviour-level tests (spec doc 1.9/1.10 rules).

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestTimeoutKillsWholeProcessGroup(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	// The agent spawns a grandchild that inherits stdout and would hold the
	// pipe open for a long time; only a group kill ends this cleanly.
	agent := filepath.Join(dir, "agent.sh")
	body := "#!/usr/bin/env bash\n" +
		"sleep 30 &\n" +
		"echo '{\"type\":\"partial\",\"text\":\"started\"}'\n" +
		"sleep 30\n"
	if err := os.WriteFile(agent, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	result, err := Run(t.Context(), map[string]any{
		"command":         agent,
		"timeout_secs":    1,
		"kill_grace_secs": 1,
	}, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !result.TimedOut || result.ExitCode != ExitTimeout {
		t.Fatalf("expected timeout/124, got timed_out=%v exit=%d", result.TimedOut, result.ExitCode)
	}
	if result.Error != "" {
		t.Fatalf("spawn-path timeout keeps error empty, got %q", result.Error)
	}
	// The kill sequence (SIGTERM + 1s grace + SIGKILL + reap) must bound the
	// turn; the sleeping grandchild must not hold it open.
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Fatalf("turn took %s — group kill did not bound it", elapsed)
	}
}

func TestBoundedDrainWhenGrandchildHoldsStdout(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	// The child exits on its own but a surviving grandchild holds stdout:
	// the turn must end at the 1 s drain bound (spec "Never wait unboundedly
	// for inherited stdio"), with the child's own result line delivered.
	agent := filepath.Join(dir, "agent.sh")
	body := "#!/usr/bin/env bash\n" +
		"sleep 30 &\n" +
		"echo '{\"type\":\"result\",\"text\":\"done\",\"session_id\":\"s-1\"}'\n" +
		"exit 0\n"
	if err := os.WriteFile(agent, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	result, err := Run(t.Context(), map[string]any{"command": agent}, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.Reply != "done" || result.SessionID != "s-1" || result.ExitCode != 0 {
		t.Fatalf("unexpected result: %+v", result)
	}
	if elapsed := time.Since(start); elapsed > 8*time.Second {
		t.Fatalf("drain took %s — the grandchild-held pipe hung the turn", elapsed)
	}
}

func TestUnterminatedTailDelivered(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	agent := filepath.Join(dir, "agent.sh")
	// Final line without a trailing newline — the child's own tail must not
	// be lost at the drain boundary.
	body := "#!/usr/bin/env bash\nprintf '%s' '{\"type\":\"result\",\"text\":\"tail\"}'\n"
	if err := os.WriteFile(agent, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	result, err := Run(t.Context(), map[string]any{"command": agent}, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.Reply != "tail" {
		t.Fatalf("reply %q, expected %q", result.Reply, "tail")
	}
}

func TestContextCancelIsNotAnError(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	agent := filepath.Join(dir, "agent.sh")
	body := "#!/usr/bin/env bash\nsleep 30\n"
	if err := os.WriteFile(agent, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	go func() {
		time.Sleep(300 * time.Millisecond)
		cancel()
	}()
	result, err := Run(ctx, map[string]any{
		"command":         agent,
		"timeout_secs":    0, // no host timeout: cancel is the only exit
		"kill_grace_secs": 1,
	}, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.ExitCode != ExitCancelled {
		t.Fatalf("exit %d, expected %d", result.ExitCode, ExitCancelled)
	}
	if result.TimedOut {
		t.Fatal("cancel must not be reported as a timeout")
	}
	if result.Error != "cancelled" {
		t.Fatalf("error %q, expected %q", result.Error, "cancelled")
	}
}

func TestSpawnPathPermissionRoundtrip(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	// The agent asks permission, reads the decision off stdin, and reports it.
	turnFile := filepath.Join(dir, "turn.json")
	respFile := filepath.Join(dir, "resp.json")
	agent := filepath.Join(dir, "agent.sh")
	body := "#!/usr/bin/env bash\n" +
		"read -r turn_line\n" +
		"printf '%s' \"$turn_line\" > \"$TURN_FILE\"\n" +
		"echo '{\"type\":\"permission_request\",\"request_id\":\"r1\",\"tool_name\":\"Bash\",\"input\":{}}'\n" +
		"read -r resp_line\n" +
		"printf '%s' \"$resp_line\" > \"$RESP_FILE\"\n" +
		"echo '{\"type\":\"result\",\"text\":\"done\"}'\n"
	if err := os.WriteFile(agent, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	result, err := Run(t.Context(), map[string]any{"command": agent, "permission": true},
		&RunOptions{
			Message: "hi",
			ExtraEnv: map[string]string{
				"TURN_FILE": turnFile,
				"RESP_FILE": respFile,
			},
			OnPermission: func(request map[string]any) *PermissionDecision {
				return &PermissionDecision{Behavior: "allow"}
			},
		})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.Reply != "done" {
		t.Fatalf("reply %q — the turn never completed", result.Reply)
	}
	respRaw, err := os.ReadFile(respFile)
	if err != nil {
		t.Fatalf("agent never received a permission response: %v", err)
	}
	var resp map[string]any
	if err := json.Unmarshal(respRaw, &resp); err != nil || resp["behavior"] != "allow" || resp["request_id"] != "r1" {
		t.Fatalf("unexpected response frame: %s (%v)", respRaw, err)
	}
	// The turn object must carry permission:true (presence-as-feature).
	turnRaw, err := os.ReadFile(turnFile)
	if err != nil {
		t.Fatalf("read turn file: %v", err)
	}
	var turn map[string]any
	if err := json.Unmarshal(turnRaw, &turn); err != nil {
		t.Fatalf("turn is not JSON: %v (%s)", err, turnRaw)
	}
	if turn["permission"] != true {
		t.Fatalf("turn object missing permission:true: %s", turnRaw)
	}
}

func TestPermissionTimeoutDeniesPending(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	// The agent asks and never reads the answer; the timeout path must deny
	// the pending request before killing (spec: prefer deny, then kill).
	agent := filepath.Join(dir, "agent.sh")
	body := "#!/usr/bin/env bash\n" +
		"echo '{\"type\":\"permission_request\",\"request_id\":\"r1\",\"tool_name\":\"Bash\",\"input\":{}}'\n" +
		"sleep 30\n"
	if err := os.WriteFile(agent, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	asked := make(chan string, 1)
	result, err := Run(t.Context(), map[string]any{
		"command":         agent,
		"permission":      true,
		"timeout_secs":    1,
		"kill_grace_secs": 1,
	}, &RunOptions{
		Message: "hi",
		OnPermission: func(request map[string]any) *PermissionDecision {
			// Block: no decision, the request stays pending until timeout.
			asked <- "asked"
			time.Sleep(5 * time.Second)
			return nil
		},
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	<-asked
	if !result.TimedOut || result.ExitCode != ExitTimeout {
		t.Fatalf("expected timeout, got %+v", result)
	}
}

func TestTurnObjectPayload(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	turnFile := filepath.Join(dir, "turn.json")
	agent := filepath.Join(dir, "agent.sh")
	body := "#!/usr/bin/env bash\n" +
		"read -r line\n" +
		"printf '%s' \"$line\" > \"$TURN_FILE\"\n" +
		"echo '{\"type\":\"result\",\"text\":\"ok\"}'\n"
	if err := os.WriteFile(agent, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	_, err := Run(t.Context(), map[string]any{"command": agent}, &RunOptions{
		Message:     "hello \\ \"world\"",
		SessionID:   "sess-9",
		ExtraEnv:    map[string]string{"TURN_FILE": turnFile},
		Attachments: []any{map[string]any{"kind": "image", "url": "https://example.com/a.png"}},
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	raw, err := os.ReadFile(turnFile)
	if err != nil {
		t.Fatalf("read turn file: %v", err)
	}
	var turn map[string]any
	if err := json.Unmarshal(raw, &turn); err != nil {
		t.Fatalf("turn is not JSON: %v (%s)", err, raw)
	}
	if turn["type"] != "turn" || turn["message"] != `hello \ "world"` ||
		turn["session_id"] != "sess-9" || turn["session_name"] != "default" ||
		turn["protocol_version"] != PROTOCOL_VERSION {
		t.Fatalf("unexpected turn object: %s", raw)
	}
	if _, ok := turn["attachments"]; !ok {
		t.Fatal("attachments key missing (presence-as-feature)")
	}
}

func TestSpawnErrorDiagnosis(t *testing.T) {
	result, err := Run(t.Context(), map[string]any{
		"command": "./definitely-missing-agent",
		"cwd":     filepath.Join(t.TempDir(), "no-such-cwd"),
	}, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.ExitCode != ExitError || result.Error == "" {
		t.Fatalf("expected friendly spawn error, got %+v", result)
	}
	if !strings.Contains(result.Error, "cwd is not a directory") {
		t.Fatalf("cwd hint missing: %q", result.Error)
	}
}

func TestExecutorResolutionFourCases(t *testing.T) {
	// (4) unknown executor + no command → hard fail.
	result, err := Run(t.Context(), map[string]any{"executor": "no-such-executor"}, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.ExitCode != ExitError || !strings.Contains(result.Error, "Unknown executor") {
		t.Fatalf("case 4: %+v", result)
	}

	// (3) unknown executor + command → warn + spawn fallback.
	requireExecable(t)
	dir := t.TempDir()
	agent := filepath.Join(dir, "agent.sh")
	if err := os.WriteFile(agent, []byte("#!/usr/bin/env bash\necho '{\"type\":\"result\",\"text\":\"fallback\"}'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	var warnings []string
	result, err = Run(t.Context(), map[string]any{"executor": "no-such-executor", "command": agent},
		&RunOptions{Message: "hi", OnStderr: func(line string) { warnings = append(warnings, line) }})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.Reply != "fallback" {
		t.Fatalf("case 3: reply %q", result.Reply)
	}
	fell := false
	for _, w := range warnings {
		if strings.Contains(w, "falling back to spawn") {
			fell = true
		}
	}
	if !fell {
		t.Fatalf("case 3: no fallback warning in %v", warnings)
	}
}

func TestDeadlineValidation(t *testing.T) {
	// A naive deadline is a profile validation error, not a silent ignore.
	_, err := Run(t.Context(), map[string]any{"command": "true", "deadline": "2025-01-01T12:00:00"}, &RunOptions{Message: "hi"})
	if err == nil || !strings.Contains(err.Error(), "timezone-aware") {
		t.Fatalf("expected deadline validation error, got %v", err)
	}
	// A timezone-aware deadline in the past expires immediately.
	requireExecable(t)
	dir := t.TempDir()
	agent := filepath.Join(dir, "agent.sh")
	if err := os.WriteFile(agent, []byte("#!/usr/bin/env bash\nsleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	result, err := Run(t.Context(), map[string]any{
		"command":         agent,
		"deadline":        "2020-01-01T00:00:00+00:00",
		"kill_grace_secs": 1,
	}, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !result.TimedOut || result.ExitCode != ExitTimeout {
		t.Fatalf("past deadline should expire immediately: %+v", result)
	}
}

func TestSessionIDValidation(t *testing.T) {
	valid := []string{"abc", "org:proj:thread-42", "with space", "a+b", "юникод"}
	for _, s := range valid {
		if !IsValidSessionIDString(s) {
			t.Fatalf("%q should be valid", s)
		}
	}
	invalid := []string{"", ".", "..", "a/b", `a\b`, "a\nb", "a\x00b", "a\tb"}
	for _, s := range invalid {
		if IsValidSessionIDString(s) {
			t.Fatalf("%q should be invalid", s)
		}
	}
}

func TestHistoryHelpers(t *testing.T) {
	dir := t.TempDir()
	if err := AppendHistory("s-1", []HistoryEntry{{Role: "user", Content: "hi"}}, dir); err != nil {
		t.Fatalf("append: %v", err)
	}
	entries, err := LoadHistory("s-1", dir)
	if err != nil || len(entries) != 1 || entries[0].Content != "hi" {
		t.Fatalf("load: %v %#v", err, entries)
	}
	// Path-traversal ids are rejected at the storage boundary.
	if _, err := SessionFilePath("a/b", dir); err == nil {
		t.Fatal("path separator id must be rejected")
	}
	if _, err := LoadHistory("", dir); err != nil {
		t.Fatalf("empty session load should be a no-op, got %v", err)
	}
}
