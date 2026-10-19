package agentproc

// Conformance driver for spec/conformance/cases.json — the single-line event
// classification, the shared child-env composition policy, the on_partial
// role passthrough, and the in-process executor permission-posture matrix.
// The Python / Node / Rust SDKs drive the same fixtures (see
// spec/conformance/README.md); a divergence turns at least one suite red.

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// --- cases.json: single-line classification ---------------------------------

func TestClassifyLineConformance(t *testing.T) {
	cases := fixtureList(t, "cases.json", "cases")
	if len(cases) < 30 {
		t.Fatalf("cases.json: expected >= 30 cases, got %d", len(cases))
	}
	for _, raw := range cases {
		c, _ := raw.(map[string]any)
		line, _ := c["line"].(string)
		expect, _ := c["expect"].(map[string]any)
		t.Run(truncForLog(line, 60), func(t *testing.T) {
			got := ClassifyLine(line)
			assertClassification(t, got, expect, line)
		})
	}
}

// assertClassification projects Event into the fixture's {kind, value[,
// role][, session_id][, usage]} shape and compares.
func assertClassification(t *testing.T, got Event, expect map[string]any, line string) {
	t.Helper()
	kind, _ := expect["kind"].(string)
	if got.Kind != kind {
		t.Fatalf("line=%q: kind %q, expected %q", line, got.Kind, kind)
	}
	switch kind {
	case KindPartial, KindResult, KindError, KindMalformed:
		wantValue := expect["value"]
		if kind == KindMalformed {
			if got.Value != line {
				t.Fatalf("malformed value should be the raw line")
			}
		} else {
			wantStr, _ := wantValue.(string)
			if got.Value != wantStr {
				t.Fatalf("line=%q: value %q, expected %q", line, got.Value, wantStr)
			}
		}
	}
	if roleRaw, ok := expect["role"]; ok {
		if wantRole, isStr := roleRaw.(string); isStr {
			if got.Role == nil || *got.Role != wantRole {
				t.Fatalf("line=%q: role %v, expected %q", line, got.Role, wantRole)
			}
		} else if got.Role != nil {
			// null: the runner must not synthesise "output".
			t.Fatalf("line=%q: role %q, expected nil", line, *got.Role)
		}
	} else if got.Role != nil {
		t.Fatalf("line=%q: unexpected role %q", line, *got.Role)
	}
	if sidRaw, ok := expect["session_id"]; ok {
		wantSid, _ := sidRaw.(string)
		if got.SessionID != wantSid {
			t.Fatalf("line=%q: session_id %q, expected %q", line, got.SessionID, wantSid)
		}
	}
	if usageRaw, ok := expect["usage"]; ok {
		if !reflect.DeepEqual(normalizeJSON(t, got.Usage), normalizeJSON(t, usageRaw)) {
			t.Fatalf("line=%q: usage %#v, expected %#v", line, got.Usage, usageRaw)
		}
	}
}

// --- cases.json: env_compose -------------------------------------------------

func TestEnvComposeConformance(t *testing.T) {
	cases := fixtureList(t, "cases.json", "env_compose")
	if len(cases) < 1 {
		t.Fatal("cases.json: expected env_compose cases")
	}
	for _, raw := range cases {
		c := raw.(map[string]any)
		t.Run("env_compose", func(t *testing.T) {
			// ComposeEnv reads the live process environment both for ${VAR}
			// expansion and for the infra set, so the fake host vars go in
			// via t.Setenv. Only the specific keys are asserted, so the rest
			// of the developer environment cannot flip the result.
			hostEnv, _ := c["host_env"].(map[string]any)
			for name, value := range hostEnv {
				t.Setenv(name, value.(string))
			}
			t.Setenv("_AGENTPROC_FAKE_MARKER", "1")

			profileEnv := map[string]string{}
			for k, v := range c["profile_env"].(map[string]any) {
				profileEnv[k] = v.(string)
			}
			var allowlist []string
			if rawAllow, ok := c["env_allowlist"].([]any); ok {
				for _, a := range rawAllow {
					allowlist = append(allowlist, a.(string))
				}
			}
			extraEnv := map[string]string{}
			for k, v := range c["extra_env"].(map[string]any) {
				extraEnv[k] = v.(string)
			}

			var warnings []string
			env := ComposeEnv(profileEnv, allowlist, extraEnv,
				map[string]string{"message": "hi", "session_id": "", "session_name": "default", "profile_dir": ""},
				func(line string) { warnings = append(warnings, line) })

			for name, value := range c["expect_contains"].(map[string]any) {
				if env[name] != value.(string) {
					t.Fatalf("%s: expected %q, got %q", name, value.(string), env[name])
				}
			}
			for _, name := range c["expect_absent"].([]any) {
				if _, ok := env[name.(string)]; ok {
					t.Fatalf("%s leaked into the composed child env", name)
				}
			}
			// The infra set never includes the marker.
			if _, ok := env["_AGENTPROC_FAKE_MARKER"]; ok {
				t.Fatal("host marker leaked into the composed child env")
			}
			// A blocked name must have produced the allowlist warning.
			if allowlist != nil {
				found := false
				for _, w := range warnings {
					if strings.Contains(w, "env_allowlist blocked") {
						found = true
					}
				}
				if !found {
					t.Fatalf("expected an env_allowlist blocked warning, got %v", warnings)
				}
			}
		})
	}
}

// --- cases.json: partial_role_cases ------------------------------------------

func TestPartialRoleConformance(t *testing.T) {
	requireExecable(t)
	cases := fixtureList(t, "cases.json", "partial_role_cases")
	if len(cases) < 3 {
		t.Fatalf("cases.json: expected >= 3 partial_role_cases, got %d", len(cases))
	}
	for _, raw := range cases {
		c := raw.(map[string]any)
		name, _ := c["name"].(string)
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			line, _ := c["line"].(string)
			scriptPath := writeBashAgent(t, dir, []any{line, `{"type":"result","text":""}`}, 0)
			type call struct {
				text string
				role *string
			}
			var seen []call
			opts := &RunOptions{
				Message:     "hello",
				TimeoutSecs: 10,
				OnPartial: func(text string, role *string) {
					seen = append(seen, call{text, role})
				},
			}
			profile, err := NormalizeProfile(map[string]any{"command": scriptPath})
			if err != nil {
				t.Fatalf("profile: %v", err)
			}
			result, err := runSpawn(t.Context(), profile, opts)
			if err != nil {
				t.Fatalf("runSpawn: %v", err)
			}
			if result.Error != "" {
				t.Fatalf("run error: %s", result.Error)
			}
			wantText, _ := c["expect_text"].(string)
			wantRoleRaw, hasRole := c["expect_role"]
			if len(seen) != 1 {
				t.Fatalf("on_partial calls: got %d, expected 1 (%v)", len(seen), seen)
			}
			if seen[0].text != wantText {
				t.Fatalf("on_partial text %q, expected %q", seen[0].text, wantText)
			}
			if s, isStr := wantRoleRaw.(string); hasRole && isStr {
				if seen[0].role == nil || *seen[0].role != s {
					t.Fatalf("on_partial role %v, expected %q", seen[0].role, s)
				}
			} else if seen[0].role != nil {
				// null: the runner MUST NOT synthesise "output".
				t.Fatalf("on_partial role %q, expected nil", *seen[0].role)
			}
		})
	}
}

// --- cases.json: auto_approve_flags + posture_cases --------------------------

func TestAutoApproveFlagsMatchFixture(t *testing.T) {
	data := loadFixture(t, "cases.json")
	fixtureFlags, _ := data["auto_approve_flags"].([]any)
	var want []string
	for _, f := range fixtureFlags {
		want = append(want, f.(string))
	}
	if !reflect.DeepEqual(AutoApproveFlags, want) {
		t.Fatalf("embedded AutoApproveFlags must match auto_approve_flags item for item:\n got %#v\nwant %#v", AutoApproveFlags, want)
	}
}

// postureShim is the fake CLI placed on PATH under the executor's cli_name:
// it records every argv token, then reports a clean claude-style turn.
const postureShim = `#!/usr/bin/env bash
printf '%s\n' "$@" > {{ARGV_FILE}}
echo '{"type":"result","result":"ok","session_id":"sess-1"}'
`

func runPostureCase(t *testing.T, c map[string]any) (*RunResult, []string) {
	t.Helper()
	requireExecable(t)
	executorName, _ := c["executor"].(string)
	factory, ok := lookupExecutor(executorName)
	if !ok {
		t.Fatalf("unknown executor %q in posture case %v", executorName, c["name"])
	}
	cliName := factory().CLIName()

	caseEnv := map[string]string{}
	if env, ok := c["env"].(map[string]any); ok {
		for k, v := range env {
			caseEnv[k] = v.(string)
		}
	}
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	shimBody := strings.ReplaceAll(postureShim, "{{ARGV_FILE}}", argvFile)
	shim := filepath.Join(dir, cliName)
	if err := os.WriteFile(shim, []byte(shimBody), 0o755); err != nil {
		t.Fatalf("write shim: %v", err)
	}

	// `env` is written to the runner process environment (the knob is
	// process-side) and to the per-run env extras; cases without the knob
	// get it blanked so the ambient environment cannot leak a posture
	// switch into the fixture.
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	knob := ""
	if v, ok := caseEnv["AGENTPROC_AUTO_APPROVE"]; ok {
		knob = v
	}
	t.Setenv("AGENTPROC_AUTO_APPROVE", knob)
	for k, v := range caseEnv {
		t.Setenv(k, v)
	}

	profileRaw := map[string]any{"executor": executorName}
	if _, hasPermission := c["permission"]; hasPermission {
		profileRaw["permission"] = c["permission"]
	}
	result, err := Run(t.Context(), profileRaw, &RunOptions{Message: "hi", ExtraEnv: caseEnv})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	var argv []string
	if raw, readErr := os.ReadFile(argvFile); readErr == nil {
		for _, line := range strings.Split(strings.TrimRight(string(raw), "\n"), "\n") {
			if line != "" {
				argv = append(argv, line)
			}
		}
	}
	return result, argv
}

func TestPostureConformance(t *testing.T) {
	cases := fixtureList(t, "cases.json", "posture_cases")
	if len(cases) < 10 {
		t.Fatalf("cases.json: expected >= 10 posture_cases, got %d", len(cases))
	}
	for _, raw := range cases {
		c := raw.(map[string]any)
		name, _ := c["name"].(string)
		t.Run(name, func(t *testing.T) {
			result, argv := runPostureCase(t, c)
			expect, _ := c["expect"].(map[string]any)
			if refused, _ := expect["refused"].(bool); refused {
				if argv != nil {
					t.Fatalf("CLI was spawned with %v", argv)
				}
				if result.Error == "" {
					t.Fatal("expected a hard failure")
				}
				if result.ExitCode == 0 {
					t.Fatalf("expected a non-zero exit code, got %d", result.ExitCode)
				}
				return
			}
			if argv == nil {
				t.Fatalf("CLI was never spawned (error: %q)", result.Error)
			}
			if errExpected, _ := expect["error"].(bool); errExpected {
				if result.Error == "" {
					t.Fatal("expected an error")
				}
				if result.ExitCode == 0 {
					t.Fatalf("expected a non-zero exit code, got %d", result.ExitCode)
				}
			}
			if exitZero, _ := expect["exit_zero"].(bool); exitZero {
				if result.Error != "" {
					t.Fatalf("unexpected error: %q", result.Error)
				}
				if result.ExitCode != 0 {
					t.Fatalf("exit %d", result.ExitCode)
				}
			}
			if reply, ok := expect["reply"]; ok {
				if result.Reply != reply.(string) {
					t.Fatalf("reply %q, expected %q", result.Reply, reply.(string))
				}
			}
			for _, tok := range expect["argv_contains"].([]any) {
				found := false
				for _, a := range argv {
					if a == tok.(string) {
						found = true
					}
				}
				if !found {
					t.Fatalf("%s missing from %v", tok, argv)
				}
			}
			for _, tok := range expect["argv_excludes"].([]any) {
				for _, a := range argv {
					if a == tok.(string) {
						t.Fatalf("%s present in %v", tok, argv)
					}
				}
			}
		})
	}
}
