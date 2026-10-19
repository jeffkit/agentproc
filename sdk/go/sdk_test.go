package agentproc

// SDK-entry conformance for spec/conformance/sdk.json: each scenario drives
// the Serve entry point as a subprocess (the built sdkharness binary) and
// asserts the exact stdout NDJSON lines + exit code. Python (_sdk_harness.py)
// and Node (sdk_harness.js) run the same fixture, so the user-facing SDK
// contract cannot drift.

import (
	"bufio"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// buildHarness compiles internal/sdkharness once per test run.
func buildHarness(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "sdkharness")
	cmd := exec.Command("go", "build", "-o", bin, "./internal/sdkharness")
	cmd.Env = append(os.Environ(), "GOFLAGS=-mod=mod")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build harness: %v\n%s", err, out)
	}
	return bin
}

type harnessResult struct {
	stdoutLines []string
	exitCode    int
}

// runHarness spawns the harness with the scenario's turn on stdin. For the
// permission roundtrip the response frame is written as soon as the request
// line has been observed on stdout (the pipe stays open for the traffic —
// the same sequencing the Python/Node drivers use).
func runHarness(t *testing.T, bin, kind string, turn map[string]any, stdinAfter []any) harnessResult {
	t.Helper()
	cmd := exec.Command(bin, kind)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatalf("stdin pipe: %v", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("stdout pipe: %v", err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}

	turnLine, _ := json.Marshal(turn)
	if _, err := stdin.Write(append(turnLine, '\n')); err != nil {
		t.Fatalf("write turn: %v", err)
	}

	reader := bufio.NewReader(stdout)
	var lines []string
	if len(stdinAfter) > 0 {
		// Keep stdin open; write the frames once the child asks for them.
		deadline := time.Now().Add(10 * time.Second)
		for len(stdinAfter) > 0 && time.Now().Before(deadline) {
			line, rerr := reader.ReadString('\n')
			if line != "" {
				lines = append(lines, strings.TrimRight(line, "\n"))
			}
			if rerr != nil {
				break
			}
			if strings.Contains(line, "permission_request") {
				// The fixture stores the frame as an already-encoded JSON
				// string — write it verbatim, one line.
				frame, _ := stdinAfter[0].(string)
				if _, err := stdin.Write([]byte(frame + "\n")); err != nil {
					t.Fatalf("write permission response: %v", err)
				}
				stdinAfter = stdinAfter[1:]
			}
		}
		stdin.Close()
	} else {
		stdin.Close()
	}

	rest, rerr := io.ReadAll(reader)
	if len(rest) > 0 {
		for _, line := range strings.Split(strings.TrimRight(string(rest), "\n"), "\n") {
			if line != "" {
				lines = append(lines, line)
			}
		}
	}
	if rerr != nil && rerr != io.EOF {
		t.Fatalf("read stdout: %v", rerr)
	}
	exitCode := 0
	if err := cmd.Wait(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		} else {
			t.Fatalf("wait: %v", err)
		}
	}
	return harnessResult{stdoutLines: lines, exitCode: exitCode}
}

func TestSDKEntryConformance(t *testing.T) {
	requireExecable(t)
	scenarios := fixtureList(t, "sdk.json", "scenarios")
	if len(scenarios) < 8 {
		t.Fatalf("sdk.json: expected >= 8 scenarios, got %d", len(scenarios))
	}
	bin := buildHarness(t)
	for _, raw := range scenarios {
		s := raw.(map[string]any)
		name, _ := s["name"].(string)
		kind, _ := s["handler"].(string)
		turn, _ := s["turn"].(map[string]any)
		expect, _ := s["expect"].(map[string]any)
		t.Run(name, func(t *testing.T) {
			var stdinAfter []any
			if frames, ok := s["stdin_after_turn"].([]any); ok {
				stdinAfter = frames
			}
			got := runHarness(t, bin, kind, turn, stdinAfter)
			wantExit := int(expect["exit"].(float64))
			if got.exitCode != wantExit {
				t.Fatalf("exit: got %d, expected %d", got.exitCode, wantExit)
			}
			wantLines, _ := expect["stdout_lines"].([]any)
			var want []string
			for _, l := range wantLines {
				want = append(want, l.(string))
			}
			if !reflect.DeepEqual(got.stdoutLines, want) {
				t.Fatalf("stdout lines:\n got %#v\nwant %#v", got.stdoutLines, want)
			}
		})
	}
}
