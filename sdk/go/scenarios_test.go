package agentproc

// Multi-line scenario driver for spec/conformance/scenarios.json — full agent
// turns through runSpawn (the spawn-path engine), asserting reply,
// session_id, error, exit_code, and the partials delivered via OnPartial.
// Python (test_scenarios.py) and Node (scenarios.test.js) run the same
// fixture; multi-line interaction semantics cannot drift between SDKs.

import (
	"reflect"
	"testing"
)

func TestScenarioConformance(t *testing.T) {
	requireExecable(t)
	scenarios := fixtureList(t, "scenarios.json", "scenarios")
	if len(scenarios) < 13 {
		t.Fatalf("scenarios.json: expected >= 13 scenarios, got %d", len(scenarios))
	}
	for _, raw := range scenarios {
		s := raw.(map[string]any)
		name, _ := s["name"].(string)
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			lines, _ := s["lines"].([]any)
			sleepSecs, _ := s["sleep_secs"].(float64)
			agent := writeBashAgent(t, dir, lines, sleepSecs)

			expect, _ := s["expect"].(map[string]any)

			profileRaw := map[string]any{"command": agent}
			if overrides, ok := s["profile_overrides"].(map[string]any); ok {
				for k, v := range overrides {
					profileRaw[k] = v
				}
			}
			streaming := true
			if v, ok := s["streaming"].(bool); ok {
				streaming = v
			}

			partials := []string{}
			result, err := Run(t.Context(), profileRaw, &RunOptions{
				Message:     "hi",
				Streaming:   &streaming,
				TimeoutSecs: 30,
				OnPartial:   func(text string, role *string) { partials = append(partials, text) },
			})
			if err != nil {
				t.Fatalf("run: %v", err)
			}

			if result.Reply != expect["reply"].(string) {
				t.Fatalf("reply got %q, expected %q", result.Reply, expect["reply"].(string))
			}
			if result.SessionID != expect["session_id"].(string) {
				t.Fatalf("session_id got %q, expected %q", result.SessionID, expect["session_id"].(string))
			}
			if result.Error != expect["error"].(string) {
				t.Fatalf("error got %q, expected %q", result.Error, expect["error"].(string))
			}
			wantExit := int(expect["exit_code"].(float64))
			if result.ExitCode != wantExit {
				t.Fatalf("exit_code got %d, expected %d", result.ExitCode, wantExit)
			}

			// partials: `partials_any_of` accepts any of the listed candidate
			// sequences (timing scenarios); otherwise strict equality.
			if candidates, ok := expect["partials_any_of"].([]any); ok {
				matched := false
				for _, cand := range candidates {
					if reflect.DeepEqual(normalizeJSON(t, partials), normalizeJSON(t, cand)) {
						matched = true
						break
					}
				}
				if !matched {
					t.Fatalf("partials got %#v, expected any of %#v", partials, candidates)
				}
			} else if want, ok := expect["partials"].([]any); ok {
				if !reflect.DeepEqual(normalizeJSON(t, partials), normalizeJSON(t, want)) {
					t.Fatalf("partials got %#v, expected %#v", partials, want)
				}
			}
		})
	}
}
