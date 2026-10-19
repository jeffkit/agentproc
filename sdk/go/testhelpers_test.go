package agentproc

// Shared test helpers: fixture loading, the fake bash agent used by the
// scenario driver, and expectation normalization.

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

const conformanceDir = "../../spec/conformance"

func loadFixture(t *testing.T, name string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(conformanceDir, name))
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		t.Fatalf("parse %s: %v", name, err)
	}
	return data
}

func fixtureList(t *testing.T, name, key string) []any {
	t.Helper()
	data := loadFixture(t, name)
	list, ok := data[key].([]any)
	if !ok {
		t.Fatalf("%s: missing %q list", name, key)
	}
	return list
}

// writeBashAgent writes the scenarios driver's fake agent: one printf per
// fixture line, then an optional sleep before exit 0.
func writeBashAgent(t *testing.T, dir string, lines []any, sleepSecs float64) string {
	t.Helper()
	body := "#!/usr/bin/env bash\n"
	for _, l := range lines {
		line, _ := l.(string)
		quoted := "'" + strings.ReplaceAll(line, "'", `'\'"'`) + "'"
		body += "printf '%s\\n' " + quoted + "\n"
	}
	if sleepSecs > 0 {
		body += "sleep " + trimFloat(sleepSecs) + "\n"
	}
	path := filepath.Join(dir, "agent.sh")
	if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
		t.Fatalf("write agent: %v", err)
	}
	return path
}

func trimFloat(f float64) string {
	b, _ := json.Marshal(f)
	return string(b)
}

func requireExecable(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("bash-agent driven conformance runs on POSIX")
	}
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash not on PATH")
	}
}

// normalizeJSON maps any JSON value through a round-trip so fixture numbers
// (float64) and executor-produced values compare with reflect.DeepEqual.
func normalizeJSON(t *testing.T, v any) any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}

func intPtr(i int) *int { return &i }

func runOptsFor(opts *RunOptions) *RunOptions {
	if opts == nil {
		return &RunOptions{}
	}
	return opts
}
