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
				TimeoutSecs: 10,
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
