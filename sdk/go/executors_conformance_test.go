package agentproc

// Executor-path scenario driver for spec/conformance/executors.json (issue
// #12): each scenario's `lines` are agent stdout NDJSON event objects fed
// through a fake executor, asserting the full RunResult. Python/Node/Rust
// drive the same fixture with the same embedded parse-rule table.

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
)

// fakeExecutor implements the fixture's shared rule table:
// {type:'partial',text} → partial_text; {type:'result',text?,session_id?,usage?}
// → final_text/session_id/usage; {type:'error',...} → error/session_id/usage;
// any other type → ignored.
type fakeExecutor struct {
	handlers *TurnHandlers
}

func (e *fakeExecutor) CLIName() string          { return "fake-cli" }
func (e *fakeExecutor) InstallHint() string      { return "" }
func (e *fakeExecutor) Plain() bool              { return false }
func (e *fakeExecutor) SupportsPermission() bool { return false }
func (e *fakeExecutor) BuildArgs(message, sessionID string, env map[string]string, ctx TurnCtx) ([]string, error) {
	if e.handlers != nil && e.handlers.BuildArgs != nil {
		return e.handlers.BuildArgs(message, sessionID, env, ctx)
	}
	return nil, fmt.Errorf("fake executor has no build_args")
}
func (e *fakeExecutor) ParseEvent(event map[string]any) *ParseResult {
	if e.handlers != nil && e.handlers.ParseEvent != nil {
		return e.handlers.ParseEvent(event)
	}
	return nil
}

// BuildInitialStdin delegates to the scenario's hook, mirroring how the
// runner picks it up via the InitialStdinBuilder interface.
func (e *fakeExecutor) BuildInitialStdin(message, sessionID string) (*string, error) {
	if e.handlers != nil && e.handlers.BuildInitialStdin != nil {
		return e.handlers.BuildInitialStdin(message, sessionID)
	}
	return nil, nil
}

// stdinEchoCLI is the fake CLI for initial_stdin scenarios: it echoes the one
// line read on stdin back as a result event; a null stdin (or a short read)
// prints NO_STDIN.
const stdinEchoCLI = `read -r line || line=NO_STDIN; printf '{"type":"result","text":"%s"}' "$line"`

func fixtureParseRules(event map[string]any) *ParseResult {
	t, _ := event["type"].(string)
	switch t {
	case "partial":
		if text, ok := event["text"].(string); ok {
			return &ParseResult{PartialText: strPtr(text)}
		}
		return &ParseResult{PartialText: strPtr("")}
	case "result":
		out := &ParseResult{}
		if text, ok := event["text"].(string); ok {
			out.FinalText = strPtr(text)
		}
		if sid, ok := event["session_id"].(string); ok && sid != "" {
			out.SessionID = sid
		}
		if usage, ok := event["usage"].(map[string]any); ok {
			out.Usage = usage
		}
		return out
	case "error":
		out := &ParseResult{}
		if msg, ok := event["message"].(string); ok {
			out.Error = msg
		}
		if sid, ok := event["session_id"].(string); ok && sid != "" {
			out.SessionID = sid
		}
		if usage, ok := event["usage"].(map[string]any); ok {
			out.Usage = usage
		}
		return out
	}
	return nil
}

func newFakeExecutor(scenario map[string]any) *fakeExecutor {
	lines, _ := scenario["lines"].([]any)
	var joinedLines []string
	for _, l := range lines {
		raw, _ := json.Marshal(l)
		joinedLines = append(joinedLines, string(raw))
	}
	joined := strings.Join(joinedLines, "\n")

	handlers := &TurnHandlers{
		ParseEvent: fixtureParseRules,
	}
	if initialStdinRaw, present := scenario["initial_stdin"]; present {
		// The scenario carries initial_stdin (a string, or nil for "the hook
		// returns no payload"): implement the hook and spawn a CLI that
		// echoes the one line it reads on stdin back as a result event.
		handlers.BuildArgs = func(message, sessionID string, env map[string]string, ctx TurnCtx) ([]string, error) {
			return []string{"/bin/sh", "-c", stdinEchoCLI}, nil
		}
		var payload *string
		if s, ok := initialStdinRaw.(string); ok {
			payload = strPtr(s)
		}
		handlers.BuildInitialStdin = func(message, sessionID string) (*string, error) {
			return payload, nil
		}
	} else {
		// Default: printf-backed, emitting one NDJSON line per entry.
		handlers.BuildArgs = func(message, sessionID string, env map[string]string, ctx TurnCtx) ([]string, error) {
			return []string{"printf", "%s\\n", joined}, nil
		}
	}
	return &fakeExecutor{handlers: handlers}
}

func TestExecutorPathConformance(t *testing.T) {
	scenarios := fixtureList(t, "executors.json", "scenarios")
	if len(scenarios) < 13 {
		t.Fatalf("executors.json: expected >= 13 scenarios, got %d", len(scenarios))
	}
	for _, raw := range scenarios {
		s := raw.(map[string]any)
		name, _ := s["name"].(string)
		t.Run(name, func(t *testing.T) {
			exp, _ := s["expect"].(map[string]any)
			partials := []string{}
			opts := &RunOptions{
				Message:     "hello",
				TimeoutSecs: intPtr(10),
				Streaming:   boolPtr(s["streaming"].(bool)),
				OnPartial:   func(text string, role *string) { partials = append(partials, text) },
			}
			result := RunViaExecutor(t.Context(), newFakeExecutor(s), opts, nil)
			if result.Reply != exp["reply"].(string) {
				t.Fatalf("reply: got %q, expected %q", result.Reply, exp["reply"].(string))
			}
			if result.SessionID != exp["session_id"].(string) {
				t.Fatalf("session_id: got %q, expected %q", result.SessionID, exp["session_id"].(string))
			}
			if result.Error != exp["error"].(string) {
				t.Fatalf("error: got %q, expected %q", result.Error, exp["error"].(string))
			}
			if want := int(exp["exit_code"].(float64)); result.ExitCode != want {
				t.Fatalf("exit_code: got %d, expected %d", result.ExitCode, want)
			}
			if !reflect.DeepEqual(normalizeJSON(t, result.Usage), normalizeJSON(t, exp["usage"])) {
				t.Fatalf("usage: got %#v, expected %#v", result.Usage, exp["usage"])
			}
			if !reflect.DeepEqual(normalizeJSON(t, partials), normalizeJSON(t, exp["partials"])) {
				t.Fatalf("partials: got %#v, expected %#v", partials, exp["partials"])
			}
		})
	}
}

func boolPtr(b bool) *bool { return &b }

// --- Review round 2: malformed/adversarial parity pins (canonical = Python/Node truthiness) ---

func TestIsErrorTruthyParity(t *testing.T) {
	// Python truthiness: the STRING "false" is truthy → the error branch.
	e := &claudeCodeExecutor{}
	for _, tc := range []struct {
		val     any
		wantErr bool
	}{
		{"false", true},
		{"true", true},
		{float64(1), true},
		{float64(0), false},
		{"", false},
		{false, false},
		{true, true},
		{nil, false},
	} {
		out := e.ParseEvent(map[string]any{"type": "result", "is_error": tc.val, "result": "x"})
		if out == nil {
			t.Fatalf("is_error=%v: nil", tc.val)
		}
		if (out.Error != "") != tc.wantErr {
			t.Fatalf("is_error=%#v: error=%q wantErr=%v", tc.val, out.Error, tc.wantErr)
		}
	}
}

func TestDeltaTruthyParity(t *testing.T) {
	// The STRING "false" is truthy → partial; a real false → final.
	e := &geminiCLIExecutor{}
	for _, tc := range []struct {
		delta   any
		partial bool
	}{
		{"false", true},
		{"true", true},
		{"", false},
		{false, false},
		{true, true},
		{nil, false},
	} {
		event := map[string]any{"type": "message", "role": "assistant", "content": "x"}
		if tc.delta != nil {
			event["delta"] = tc.delta
		}
		out := e.ParseEvent(event)
		if out == nil {
			t.Fatalf("delta=%v: nil", tc.delta)
		}
		if (out.PartialText != nil) != tc.partial {
			t.Fatalf("delta=%#v: got %+v, partial=%v", tc.delta, out, tc.partial)
		}
	}
}

func TestEnvToggleKnobsAreVerbatim(t *testing.T) {
	// Python `(env.get(X) or "1")`: empty → default, raw compare (no trim).
	runs := []struct {
		name   string
		build  func(map[string]string) ([]string, error)
		env    map[string]string
		want   string // argv token that must be present
		banned string // argv token that must be absent
	}{
		{"cursor force empty→yolo", func(e map[string]string) ([]string, error) {
			return resolveTurnHandlers(&cursorExecutor{}).BuildArgs("m", "", e, TurnCtx{})
		}, map[string]string{"CURSOR_FORCE": ""}, "--yolo", ""},
		{"cursor force spaced≠1→no yolo", func(e map[string]string) ([]string, error) {
			return resolveTurnHandlers(&cursorExecutor{}).BuildArgs("m", "", e, TurnCtx{})
		}, map[string]string{"CURSOR_FORCE": " 1 "}, "", "--yolo"},
		{"agy skip empty→dangerous", func(e map[string]string) ([]string, error) {
			return resolveTurnHandlers(&agyExecutor{}).BuildArgs("m", "", e, TurnCtx{})
		}, map[string]string{"AGY_DANGEROUSLY_SKIP_PERMISSIONS": ""}, "--dangerously-skip-permissions", ""},
		{"agy skip spaced≠1→clean", func(e map[string]string) ([]string, error) {
			return resolveTurnHandlers(&agyExecutor{}).BuildArgs("m", "", e, TurnCtx{})
		}, map[string]string{"AGY_DANGEROUSLY_SKIP_PERMISSIONS": " 1 "}, "", "--dangerously-skip-permissions"},
		{"pi noext empty→extensions", func(e map[string]string) ([]string, error) {
			return resolveTurnHandlers(&piExecutor{}).BuildArgs("m", "", e, TurnCtx{})
		}, map[string]string{"PI_NO_EXTENSIONS": ""}, "--no-extensions", ""},
		{"pi noext spaced 0→kept", func(e map[string]string) ([]string, error) {
			return resolveTurnHandlers(&piExecutor{}).BuildArgs("m", "", e, TurnCtx{})
		}, map[string]string{"PI_NO_EXTENSIONS": " 0 "}, "--no-extensions", ""},
		{"pi noext 0→dropped", func(e map[string]string) ([]string, error) {
			return resolveTurnHandlers(&piExecutor{}).BuildArgs("m", "", e, TurnCtx{})
		}, map[string]string{"PI_NO_EXTENSIONS": "0"}, "", "--no-extensions"},
	}
	for _, tc := range runs {
		argv, err := tc.build(tc.env)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if tc.want != "" {
			found := false
			for _, a := range argv {
				if a == tc.want {
					found = true
				}
			}
			if !found {
				t.Fatalf("%s: %v missing %s", tc.name, argv, tc.want)
			}
		}
		if tc.banned != "" {
			for _, a := range argv {
				if a == tc.banned {
					t.Fatalf("%s: %v must not contain %s", tc.name, argv, tc.banned)
				}
			}
		}
	}
}

func TestClaudeStdinFrameByteParity(t *testing.T) {
	// HTML-unsafe characters must arrive raw (ensure_ascii=False parity).
	e := &claudeCodeExecutor{}
	frame, err := e.BuildInitialStdin(`<a href="x">&</a>`+"ok", "s-1")
	if err != nil {
		t.Fatal(err)
	}
	for _, banned := range []string{`\u003c`, `\u003e`, `\u0026`} {
		if strings.Contains(*frame, banned) {
			t.Fatalf("frame over-escaped (%s): %s", banned, *frame)
		}
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(*frame), &parsed); err != nil {
		t.Fatalf("frame is not JSON: %s", *frame)
	}
}

func TestCodexTurnFailedErrorStringify(t *testing.T) {
	e := &codexExecutor{}
	out := e.ParseEvent(map[string]any{"type": "turn.failed", "error": "quota exceeded"})
	if out == nil || out.Error != "quota exceeded" {
		t.Fatalf("string error: %+v", out)
	}
	out = e.ParseEvent(map[string]any{"type": "turn.failed", "error": map[string]any{"message": "quota exceeded"}})
	if out == nil || out.Error != "quota exceeded" {
		t.Fatalf("object error with message: %+v", out)
	}
	out = e.ParseEvent(map[string]any{"type": "turn.failed", "error": map[string]any{"code": 42}})
	if out == nil || out.Error == "" || out.Error == "codex turn failed" {
		t.Fatalf("object error without message should be stringified: %+v", out)
	}
	out = e.ParseEvent(map[string]any{"type": "turn.failed"})
	if out == nil || out.Error != "codex turn failed" {
		t.Fatalf("absent error: %+v", out)
	}
}
