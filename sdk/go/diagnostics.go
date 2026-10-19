package agentproc

import (
	"fmt"
	"regexp"
	"strings"
)

// StderrDiagnosticRule is one (pattern, hint) post-mortem rule. This is the
// runtime-embedded copy of spec/conformance/diagnostics.json — the single
// source of truth; the conformance test asserts the two stay in sync. Rules
// are evaluated in order; first match wins. A "{n}" token in the hint is
// replaced by capture group n; "{{PROFILE_DIR}}" is a literal, not a format
// token (only numeric {n} tokens are substituted).
type StderrDiagnosticRule struct {
	ID      string `json:"id"`
	Pattern string `json:"pattern"`
	Flags   string `json:"flags,omitempty"`
	Hint    string `json:"hint"`
	Sample  string `json:"sample,omitempty"`
	Expect  string `json:"expect,omitempty"`
}

// StderrDiagnostics is the embedded mirror of spec/conformance/diagnostics.json.
var StderrDiagnostics = []StderrDiagnosticRule{
	{
		ID:      "python-open-file",
		Pattern: `(?:can'?t|cannot) open file '([^']+)': \[Errno 2\] No such file or directory`,
		Hint:    "agent script not found: {1}. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).",
	},
	{
		ID:      "node-cannot-find-module",
		Pattern: `Cannot find module '([^']+)'`,
		Hint:    "agent script not found: {1}. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).",
	},
	{
		ID:      "bash-line-no-such-file",
		Pattern: `(?:^|\n)[^:]+: line \d+: ([^:]+): No such file or directory`,
		Hint:    "agent script not found: {1}. Check the profile's command path.",
	},
	{
		ID:      "generic-enoent",
		Pattern: `errno 2|enoent|no such file or directory`,
		Flags:   "i",
		Hint:    "agent reported a missing file. Check the profile's command and cwd.",
	},
}

// DiagnoseStderrFailure is a best-effort pattern check against the agent's
// accumulated stderr. It catches "bridge file not found" failures that the
// wrapped interpreter writes to its own stderr before exiting non-zero.
// Returns a friendly hint, or "" if nothing recognisable.
func DiagnoseStderrFailure(stderrText string) string {
	if stderrText == "" {
		return ""
	}
	for _, rule := range StderrDiagnostics {
		pattern := rule.Pattern
		if rule.Flags == "i" {
			pattern = "(?i)" + pattern
		}
		re, err := regexp.Compile(pattern)
		if err != nil {
			continue
		}
		m := re.FindStringSubmatchIndex(stderrText)
		if m == nil {
			continue
		}
		return formatHint(rule.Hint, re, stderrText, m)
	}
	return ""
}

func formatHint(hint string, re *regexp.Regexp, input string, loc []int) string {
	return regexp.MustCompile(`\{(\d+)\}`).ReplaceAllStringFunc(hint, func(tok string) string {
		var n int
		if _, err := fmt.Sscanf(tok, "{%d}", &n); err != nil {
			return tok
		}
		if n < 0 || 2*n+1 >= len(loc) || loc[2*n] < 0 {
			return ""
		}
		return input[loc[2*n]:loc[2*n+1]]
	})
}

// FailureDiagnosticPattern mirrors FAILURE_DIAGNOSTIC_PATTERNS — the
// diagnostic-line shapes preferred when reporting a non-zero executor exit
// (plaita#121): the *head* of stderr is often a startup banner while the real
// cause (an LLM transient HTTP error, a panic, …) sits further down.
var FailureDiagnosticPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)transient HTTP error`),
	regexp.MustCompile(`\b429\b`),
	regexp.MustCompile(`\b5\d{2}\b`), // 5xx status (status=500 / HTTP 503)
	regexp.MustCompile(`thread '[^']*' panicked`),
	regexp.MustCompile(`(?i)\bpanic(?:ked)?\b`),
	regexp.MustCompile(`(?i)\b(?:error|exception|fatal|failed|failure)\b`),
}

const (
	failureTailLines          = 20
	failureMaxDiagnosticLines = 10
)

// ExtractFailureDiagnostics picks the useful failure signal out of an
// executor's stderr. Returns the diagnostic-looking lines (order preserved,
// de-duplicated, capped at failureMaxDiagnosticLines) when any matches
// FailureDiagnosticPatterns; otherwise the last maxTailLines lines prefixed
// with an explicit "not necessarily the cause" marker. Falls back to
// "(no stderr)" for empty input so callers keep the historical wording.
func ExtractFailureDiagnostics(stderrText string, maxTailLines int) string {
	var lines []string
	for _, ln := range strings.Split(stderrTextOrEmpty(stderrText), "\n") {
		trimmed := strings.TrimRight(ln, "\r")
		if strings.TrimSpace(trimmed) == "" {
			continue
		}
		lines = append(lines, trimmed)
	}
	if len(lines) == 0 {
		return "(no stderr)"
	}
	hits := make([]string, 0, 4)
	seen := map[string]bool{}
	for _, line := range lines {
		matched := false
		for _, p := range FailureDiagnosticPatterns {
			if p.MatchString(line) {
				matched = true
				break
			}
		}
		if !matched {
			continue
		}
		key := strings.TrimSpace(line)
		if seen[key] {
			continue
		}
		seen[key] = true
		hits = append(hits, line)
		if len(hits) >= failureMaxDiagnosticLines {
			break
		}
	}
	if len(hits) > 0 {
		return strings.Join(hits, "\n")
	}
	if maxTailLines <= 0 || maxTailLines > len(lines) {
		maxTailLines = len(lines)
	}
	tail := lines[len(lines)-maxTailLines:]
	return fmt.Sprintf("(no diagnostic line matched; last %d stderr lines follow — not necessarily the cause)\n%s",
		len(tail), strings.Join(tail, "\n"))
}

func stderrTextOrEmpty(s string) string {
	return s
}
