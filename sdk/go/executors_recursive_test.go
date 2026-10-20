package agentproc

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRecursiveBuildArgs(t *testing.T) {
	e := &recursiveExecutor{}

	// Defaults: headless automation flags, workspace ".", json output, auto
	// permission mode, `run --` ahead of the positional message.
	args, err := e.BuildArgs("do the thing", "", map[string]string{}, TurnCtx{})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	want := []string{
		"recursive", "--workspace", ".", "--output-format", "json",
		"--permission-mode", "auto", "run", "--", "do the thing",
	}
	if strings.Join(args, " ") != strings.Join(want, " ") {
		t.Fatalf("defaults argv mismatch:\n got: %v\nwant: %v", args, want)
	}

	// Env knobs: bin/workspace/output-format/model/max-steps.
	env := map[string]string{
		"RECURSIVE_BIN":           "/opt/recursive/bin/recursive",
		"RECURSIVE_WORKSPACE":     "/repo",
		"RECURSIVE_OUTPUT_FORMAT": "stream-json",
		"RECURSIVE_MODEL":         "glm-5.3-flash",
		"RECURSIVE_MAX_STEPS":     "80",
	}
	args, err = e.BuildArgs("hi", "", env, TurnCtx{})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	got := strings.Join(args, " ")
	for _, frag := range []string{
		"/opt/recursive/bin/recursive", "--workspace /repo",
		"--output-format stream-json", "--model glm-5.3-flash", "--max-steps 80",
	} {
		if !strings.Contains(got, frag) {
			t.Fatalf("argv missing %q: %v", frag, args)
		}
	}

	// Resume form: native session id + --message (L2 breakpoint resume).
	args, err = e.BuildArgs("continue", "sess-9", map[string]string{}, TurnCtx{})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	want = []string{
		"recursive", "--workspace", ".", "--output-format", "json",
		"--permission-mode", "auto", "resume", "sess-9", "--message", "continue",
	}
	if strings.Join(args, " ") != strings.Join(want, " ") {
		t.Fatalf("resume argv mismatch:\n got: %v\nwant: %v", args, want)
	}

	// Empty env values fall back to defaults (envOr semantics), and an empty
	// RECURSIVE_MODEL / RECURSIVE_MAX_STEPS adds no flag.
	args, err = e.BuildArgs("hi", "", map[string]string{"RECURSIVE_MODEL": "", "RECURSIVE_MAX_STEPS": ""}, TurnCtx{})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if strings.Contains(strings.Join(args, " "), "--model") || strings.Contains(strings.Join(args, " "), "--max-steps") {
		t.Fatalf("empty knobs must not add flags: %v", args)
	}
}

func TestRecursiveParsePlainOutput(t *testing.T) {
	e := &recursiveExecutor{}

	// Trailing result object after log noise; line-start scan, last-first.
	res := e.ParsePlainOutput("booting...\nloading context\n{\"session_id\": \"s1\", \"usage\": {\"input\": 3}, \"result\": \"ok\"}\n")
	if res.Error != "" || res.FinalText == nil || *res.FinalText != "ok" {
		t.Fatalf("trailing object: %+v", res)
	}
	if res.SessionID != "s1" || res.Usage == nil || res.Usage["input"] != float64(3) {
		t.Fatalf("session/usage: %+v", res)
	}

	// Pretty-printed multi-line object still balances.
	res = e.ParsePlainOutput("noise\n{\n  \"result\": \"multi\"\n}\n")
	if res.FinalText == nil || *res.FinalText != "multi" {
		t.Fatalf("multiline: %+v", res)
	}

	// Mid-line object via the `{"` fallback (log noise shares the line).
	res = e.ParsePlainOutput("step 1 done {\"result\": \"mid\"} trailing")
	if res.FinalText == nil || *res.FinalText != "mid" {
		t.Fatalf("mid-line: %+v", res)
	}

	// Raw fallback: no JSON anywhere → the whole stdout is the reply.
	res = e.ParsePlainOutput("just plain words\nsecond line")
	if res.FinalText == nil || *res.FinalText != "just plain words\nsecond line" {
		t.Fatalf("raw fallback: %+v", res)
	}

	// is_error is a terminal turn error; truthiness per Python parity.
	res = e.ParsePlainOutput("{\"is_error\": true, \"result\": \"kaput\"}")
	if res.Error == "" || !strings.Contains(res.Error, "kaput") || res.FinalText != nil {
		t.Fatalf("is_error: %+v", res)
	}
}

// End-to-end through Run: a fake `recursive` binary proves the argv shape and
// that the trailing result object becomes Reply/SessionID/Usage — and that
// the second turn resumes via `resume <session> --message`.
func TestRecursiveExecutorRunAndResume(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.log")
	script := filepath.Join(dir, "fake-recursive")
	body := "#!/usr/bin/env bash\n" +
		"printf '%s\\n' \"$*\" >> '" + argvFile + "'\n" +
		"echo 'loading context...'\n" +
		"echo '{\"session_id\": \"sess-r1\", \"usage\": {\"input\": 7}, \"result\": \"answer one\"}'\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}

	profile := map[string]any{
		"executor": "recursive",
		"env":      map[string]any{"RECURSIVE_BIN": script},
	}

	res, err := Run(t.Context(), profile, &RunOptions{Message: "do the thing"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if res.Reply != "answer one" || res.SessionID != "sess-r1" || res.Usage == nil {
		t.Fatalf("turn 1: %+v", res)
	}
	if res.ExitCode != ExitSuccess || res.Error != "" {
		t.Fatalf("turn 1 exit: %+v", res)
	}

	raw, err := os.ReadFile(argvFile)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(lines) != 1 {
		t.Fatalf("argv log: %q", raw)
	}
	for _, frag := range []string{
		"--workspace .", "--output-format json", "--permission-mode auto",
		"run -- do the thing",
	} {
		if !strings.Contains(lines[0], frag) {
			t.Fatalf("argv missing %q: %s", frag, lines[0])
		}
	}

	// Turn 2: resume the session natively.
	res2, err := Run(t.Context(), profile, &RunOptions{Message: "continue please", SessionID: "sess-r1"})
	if err != nil {
		t.Fatalf("run 2: %v", err)
	}
	if res2.Reply != "answer one" || res2.SessionID != "sess-r1" {
		t.Fatalf("turn 2: %+v", res2)
	}
	raw, err = os.ReadFile(argvFile)
	if err != nil {
		t.Fatal(err)
	}
	lines = strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(lines) != 2 || !strings.Contains(lines[1], "resume sess-r1 --message continue please") {
		t.Fatalf("resume argv: %q", lines)
	}
}

// A recursive turn reporting is_error must fail even when the process exits 0
// (plain output is an envelope; the error lives inside it).
func TestRecursiveExecutorIsErrorFailsTurn(t *testing.T) {
	requireExecable(t)
	dir := t.TempDir()
	script := filepath.Join(dir, "fake-recursive")
	body := "#!/usr/bin/env bash\n" +
		"echo '{\"is_error\": true, \"result\": \"kaput\"}'\n" +
		"exit 0\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	profile := map[string]any{
		"executor": "recursive",
		"env":      map[string]any{"RECURSIVE_BIN": script},
	}
	res, err := Run(t.Context(), profile, &RunOptions{Message: "hi"})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if res.ExitCode == ExitSuccess || !strings.Contains(res.Error, "kaput") {
		t.Fatalf("is_error turn must fail: %+v", res)
	}
}
