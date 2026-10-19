package agentproc

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Profile is a validated, normalized agent profile (spec/protocol.md
// "Profile YAML"). Build one programmatically, or parse YAML with
// ProfileFromYAML / a raw map with NormalizeProfile.
type Profile struct {
	// Command is the executable (argv[0]) — one token, never split. Required
	// unless Executor is set to a name the SDK recognises.
	Command string
	// Executor is the optional in-process executor name (SDK-specific registry).
	Executor string
	// Args is argv[1..]; defaults to empty.
	Args []string
	// Cwd is the working directory; "" = the bridge's process cwd. Relative
	// paths are resolved against the profile's own directory at run time.
	Cwd string
	// Env is the extra environment block (secrets / config), ${VAR}-expanded.
	Env map[string]string
	// EnvAllowlist, when non-nil, shrinks ${VAR} expansion in Env to exactly
	// these names (blocked names expand to empty + a stderr warning). nil =
	// expand against the full bridge environment.
	EnvAllowlist []string
	// Permission enables the optional tool-permission channel (opt-in).
	Permission bool
	// TimeoutSecs is the per-turn wall-clock timeout; 0 = no host-side limit.
	// Default DefaultTimeoutSecs (1800) when absent from the profile.
	TimeoutSecs int
	// KillGraceSecs is the SIGTERM → SIGKILL grace period. Default
	// DefaultKillGraceSecs (5).
	KillGraceSecs int
	// BudgetSecs is the optional per-turn wall-clock budget (earliest-expiry
	// wins with TimeoutSecs and Deadline); nil = absent.
	BudgetSecs *float64
	// Deadline is the optional absolute ISO-8601 timestamp (timezone-aware).
	// Kept raw: a value that is present but not a valid timestamp string is
	// a profile validation error at Run time (spec: MUST NOT be silently
	// ignored), never dropped during normalization.
	Deadline any
	// Streaming forwards {"type":"partial"} events in real time. Default true.
	Streaming bool
}

// DefaultTimeoutSecs / DefaultKillGraceSecs mirror the reference runners.
const (
	DefaultTimeoutSecs   = 1800
	DefaultKillGraceSecs = 5
)

// ProfileFromYAML parses profile YAML text (or JSON — YAML is a superset)
// into a Profile. A top-level "agentproc:" wrapper key is unwrapped like the
// reference SDKs do.
func ProfileFromYAML(raw []byte) (*Profile, error) {
	var doc map[string]any
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("profile is not valid YAML: %w", err)
	}
	return NormalizeProfile(doc)
}

// isIntLike mirrors the Python runner's scalar coercion: a bool is never a
// number; an int is; a string of digits (optionally signed) is.
func isIntLike(v any) bool {
	switch t := v.(type) {
	case bool:
		return false
	case int:
		return true
	case int64:
		return true
	case float64:
		return t == float64(int64(t))
	case string:
		s := strings.TrimSpace(t)
		s = strings.TrimPrefix(s, "-")
		if s == "" {
			return false
		}
		for _, r := range s {
			if r < '0' || r > '9' {
				return false
			}
		}
		return true
	default:
		return false
	}
}

func isNumLike(v any) bool {
	switch t := v.(type) {
	case bool, nil:
		return false
	case int, int64, float64:
		return true
	case string:
		_, err := strconv.ParseFloat(strings.TrimSpace(t), 64)
		return err == nil
	default:
		return false
	}
}

func toInt(v any) int {
	switch t := v.(type) {
	case int:
		return t
	case int64:
		return int(t)
	case float64:
		return int(t)
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(t))
		if err != nil {
			return 0
		}
		return n
	default:
		return 0
	}
}

func toFloat(v any) float64 {
	switch t := v.(type) {
	case int:
		return float64(t)
	case int64:
		return float64(t)
	case float64:
		return t
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(t), 64)
		if err != nil {
			return 0
		}
		return f
	default:
		return 0
	}
}

// NormalizeProfile validates and normalizes a raw profile dict (from YAML,
// JSON, or programmatic construction).
func NormalizeProfile(raw map[string]any) (*Profile, error) {
	if raw == nil {
		return nil, errors.New("profile must be a dict")
	}
	src := raw
	if wrapper, ok := raw["agentproc"].(map[string]any); ok {
		src = wrapper
	}

	p := &Profile{Env: map[string]string{}}

	// executor: optional SDK-registered name for in-process execution.
	if v, ok := src["executor"]; ok && v != nil {
		if s, ok := v.(string); ok {
			p.Executor = strings.TrimSpace(s)
		}
	}

	command, _ := src["command"].(string)
	if p.Executor == "" && strings.TrimSpace(command) == "" {
		return nil, errors.New("profile.command must be a non-empty string (or set executor: to use an in-process executor)")
	}
	p.Command = strings.TrimSpace(command)

	switch args := src["args"].(type) {
	case nil:
		p.Args = []string{}
	case []any:
		p.Args = make([]string, 0, len(args))
		for _, a := range args {
			p.Args = append(p.Args, toString(a))
		}
	default:
		return nil, errors.New("profile.args must be a list")
	}

	if cwd, ok := src["cwd"].(string); ok && cwd != "" {
		p.Cwd = ExpandPath(cwd)
	}

	switch env := src["env"].(type) {
	case nil:
		// leave empty map
	case map[string]any:
		for k, v := range env {
			p.Env[k] = toString(v)
		}
	case map[string]string:
		for k, v := range env {
			p.Env[k] = v
		}
	default:
		return nil, errors.New("profile.env must be a dict")
	}

	switch allow := src["env_allowlist"].(type) {
	case nil:
		p.EnvAllowlist = nil
	case []any:
		p.EnvAllowlist = make([]string, 0, len(allow))
		for _, a := range allow {
			p.EnvAllowlist = append(p.EnvAllowlist, toString(a))
		}
	default:
		return nil, errors.New("profile.env_allowlist must be a list")
	}

	if v, ok := src["permission"].(bool); ok {
		p.Permission = v
	}

	if isIntLike(src["timeout_secs"]) {
		p.TimeoutSecs = toInt(src["timeout_secs"])
	} else {
		p.TimeoutSecs = DefaultTimeoutSecs
	}
	if isIntLike(src["kill_grace_secs"]) {
		p.KillGraceSecs = toInt(src["kill_grace_secs"])
	} else {
		p.KillGraceSecs = DefaultKillGraceSecs
	}
	if isNumLike(src["budget_secs"]) {
		b := toFloat(src["budget_secs"])
		p.BudgetSecs = &b
	}
	if d, ok := src["deadline"]; ok && d != nil {
		p.Deadline = d
	}

	// Bridge-side hint: only the literal false disables streaming (mirrors
	// `src.get("streaming", True) is not False`).
	p.Streaming = true
	if v, ok := src["streaming"].(bool); ok && !v {
		p.Streaming = false
	}
	return p, nil
}

// ParseDeadline parses a profile deadline into epoch seconds; 0 when absent.
// Timezone-aware ISO-8601 only; a naive timestamp or unparseable value is a
// profile validation error (spec: bridges MUST NOT silently ignore it).
func ParseDeadline(value any) (float64, error) {
	if value == nil {
		return 0, nil
	}
	if t, ok := value.(time.Time); ok {
		// yaml.v3 decodes an unquoted RFC3339 scalar into time.Time. Accepted
		// even though Python errors on non-string deadlines — the scalar was
		// already a real timestamp; refusing it here would only surprise.
		return float64(t.UnixNano()) / 1e9, nil
	}
	s, ok := value.(string)
	if !ok {
		return 0, errors.New("profile.deadline must be an ISO-8601 timestamp string")
	}
	// Python's fromisoformat accepts a space as the date/time separator —
	// normalize once so profiles portable across SDKs keep working.
	text := strings.ReplaceAll(strings.TrimSpace(s), " ", "T")
	if text == "" || text == "T" {
		return 0, fmt.Errorf("profile.deadline must be a timezone-aware ISO-8601 timestamp, got %q", value)
	}
	if strings.HasSuffix(text, "Z") || strings.HasSuffix(text, "z") {
		text = text[:len(text)-1] + "+00:00"
	}
	t, err := time.Parse(time.RFC3339, text)
	if err != nil {
		return 0, fmt.Errorf("profile.deadline must be a timezone-aware ISO-8601 timestamp, got %q", value)
	}
	return float64(t.UnixNano()) / 1e9, nil
}

// ExpandPath expands a leading "~" / "~/" to the current user's home.
func ExpandPath(p string) string {
	home, err := os.UserHomeDir()
	if err != nil {
		return p
	}
	if p == "~" {
		return home
	}
	if strings.HasPrefix(p, "~/") {
		return filepath.Join(home, p[2:])
	}
	return p
}

// Substitute replaces the {{SESSION_ID}} / {{SESSION_NAME}} / {{PROFILE_DIR}}
// placeholders. {{MESSAGE}} is intentionally not substituted — the message
// travels via stdin only (never argv, never env).
func Substitute(value string, ctx map[string]string) string {
	get := func(k string) string {
		if ctx == nil {
			return ""
		}
		return ctx[k]
	}
	out := strings.ReplaceAll(value, "{{SESSION_ID}}", get("session_id"))
	out = strings.ReplaceAll(out, "{{SESSION_NAME}}", get("session_name"))
	out = strings.ReplaceAll(out, "{{PROFILE_DIR}}", get("profile_dir"))
	return out
}
