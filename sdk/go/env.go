package agentproc

import (
	"fmt"
	"os"
	"regexp"
	"strings"
)

// ENV_INFRA_VARS mirror (wire 0.3): the operational variables a bridge copies
// from its own environment into the child. None of them are credential-bearing.
// The infra set is always the base of the child env — there is no
// "inherit everything" mode in 0.3+.
var EnvInfraVars = []string{
	"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
	"LC_MESSAGES", "TERM", "TMPDIR", "TZ", "PWD",
	// Windows infra
	"SystemRoot", "TEMP", "TMP", "USERPROFILE", "USERNAME", "PATHEXT",
	"COMSPEC", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
	"PROCESSOR_ARCHITECTURE", "OS",
}

// BuildBaseEnv builds the child process base env — the infra set, always,
// copied from the bridge's own environment (names only when set).
func BuildBaseEnv() map[string]string {
	base := make(map[string]string)
	for _, name := range EnvInfraVars {
		if v, ok := os.LookupEnv(name); ok {
			base[name] = v
		}
	}
	return base
}

var envRefRe = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// ExpandEnvRef expands ${VAR} references in value against env.
//
// When allowlist is non-nil, a reference whose name is NOT in the set expands
// to the empty string and onBlocked (when given) is called with the name —
// the spec's "env_allowlist blocked ${NAME}; expanded to empty" warning.
// When allowlist is nil, all references expand normally (unknown names expand
// to empty, POSIX-shell semantics — never to the literal ${VAR}).
func ExpandEnvRef(value string, env map[string]string, allowlist map[string]bool, onBlocked func(name string)) string {
	return envRefRe.ReplaceAllStringFunc(value, func(m string) string {
		name := envRefRe.FindStringSubmatch(m)[1]
		if allowlist != nil && !allowlist[name] {
			if onBlocked != nil {
				onBlocked(name)
			}
			return ""
		}
		return env[name]
	})
}

// ComposeEnv is the single shared three-layer child-env policy (both the
// spawn path and the executor path MUST use this — spec: "no third
// composition implementation"):
//
//  1. infra set (BuildBaseEnv),
//  2. the profile env block (${VAR} expanded against the bridge's full
//     environment, env_allowlist-filtered — a blocked name expands to the
//     empty string but the key is still set),
//  3. extraEnv from the CLI --env flag (later layers override earlier).
//
// profileEnv values may carry {{...}} placeholders, which are substituted
// with substCtx before ${VAR} expansion. onStderr (when given) receives the
// env_allowlist blocking warnings.
func ComposeEnv(profileEnv map[string]string, allowlist []string, extraEnv map[string]string, substCtx map[string]string, onStderr func(line string)) map[string]string {
	env := BuildBaseEnv()
	var allowSet map[string]bool
	if allowlist != nil {
		allowSet = make(map[string]bool, len(allowlist))
		for _, name := range allowlist {
			allowSet[name] = true
		}
	}
	for k, v := range profileEnv {
		expanded := ExpandEnvRef(Substitute(v, substCtx), osEnviron(), allowSet, func(name string) {
			if onStderr != nil {
				onStderr(fmt.Sprintf("[agentproc runner] env_allowlist blocked ${%s} (not in allowlist); expanded to empty", name))
			}
		})
		env[k] = expanded
	}
	for k, v := range extraEnv {
		env[k] = v
	}
	return env
}

// osEnviron snapshots the process environment as a map — the ${VAR}
// expansion source (the bridge's own full environment).
func osEnviron() map[string]string {
	m := make(map[string]string)
	for _, kv := range os.Environ() {
		if i := strings.IndexByte(kv, '='); i >= 0 {
			m[kv[:i]] = kv[i+1:]
		}
	}
	return m
}
