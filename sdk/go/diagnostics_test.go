package agentproc

// Diagnostics conformance for spec/conformance/diagnostics.json: the
// embedded StderrDiagnostics copy must match the fixture rule-for-rule, and
// each rule's sample must produce the expected hint.

import (
	"strings"
	"testing"
)

func TestEmbeddedDiagnosticsMirrorFixture(t *testing.T) {
	rawRules := fixtureList(t, "diagnostics.json", "rules")
	if len(rawRules) != len(StderrDiagnostics) {
		t.Fatalf("rule count mismatch: embedded %d, fixture %d", len(StderrDiagnostics), len(rawRules))
	}
	for i, raw := range rawRules {
		want := raw.(map[string]any)
		got := StderrDiagnostics[i]
		if got.ID != want["id"].(string) {
			t.Fatalf("rule %d id: got %q, expected %q", i, got.ID, want["id"].(string))
		}
		if got.Pattern != want["pattern"].(string) {
			t.Fatalf("rule %d (%s) pattern:\n got %q\nwant %q", i, got.ID, got.Pattern, want["pattern"].(string))
		}
		if got.Hint != want["hint"].(string) {
			t.Fatalf("rule %d (%s) hint:\n got %q\nwant %q", i, got.ID, got.Hint, want["hint"].(string))
		}
		wantFlags, _ := want["flags"].(string)
		if got.Flags != wantFlags {
			t.Fatalf("rule %d (%s) flags: got %q, expected %q", i, got.ID, got.Flags, wantFlags)
		}
	}
}

func TestDiagnosticSamplesProduceExpectedHints(t *testing.T) {
	rawRules := fixtureList(t, "diagnostics.json", "rules")
	for _, raw := range rawRules {
		rule := raw.(map[string]any)
		id, _ := rule["id"].(string)
		sample, _ := rule["sample"].(string)
		expect, _ := rule["expect"].(string)
		if got := DiagnoseStderrFailure(sample); got != expect {
			t.Fatalf("%s: got %q, expected %q", id, got, expect)
		}
	}
}

func TestDiagnoseStderrFailureEmptyAndUnrecognized(t *testing.T) {
	if got := DiagnoseStderrFailure(""); got != "" {
		t.Fatalf("empty input: got %q", got)
	}
	if got := DiagnoseStderrFailure("totally fine, nothing to see"); got != "" {
		t.Fatalf("unrecognized input: got %q", got)
	}
}

func TestExtractFailureDiagnostics(t *testing.T) {
	// Diagnostic lines win over banners (the plaita#121 shape).
	stderr := "checkpoint: per-turn snapshots active\n" +
		"retrying model call\n" +
		"transient HTTP error 503 upstream\n"
	got := ExtractFailureDiagnostics(stderr, failureTailLines)
	if got != "transient HTTP error 503 upstream" {
		t.Fatalf("diagnostic extraction: got %q", got)
	}

	// No diagnostic match → the tail with the explicit marker.
	got = ExtractFailureDiagnostics("banner one\nbanner two\n", failureTailLines)
	if !strings.Contains(got, "not necessarily the cause") || !strings.Contains(got, "banner two") {
		t.Fatalf("tail fallback: got %q", got)
	}

	// Empty input keeps the historical wording.
	if got := ExtractFailureDiagnostics("", failureTailLines); got != "(no stderr)" {
		t.Fatalf("empty: got %q", got)
	}
}
