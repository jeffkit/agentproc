package agentproc

// Native Go fuzz targets for the SDK's parsing surfaces:
//
//   - stdout-line classification (protocol.go: ParseJSONLine / ClassifyLine)
//   - profile YAML normalization (profile.go: ProfileFromYAML / NormalizeProfile)
//   - deadline parsing (profile.go: ParseDeadline)
//   - permission-request/response shaping (protocol.go: IsValidSessionIDString,
//     IsValidPermissionRequest, FormatPermissionResponse)
//   - hub metadata + jsDelivr tree flattening (hub.go: hubProfileFromYAML,
//     flattenJsDelivrTree)
//
// Every target asserts the same invariant: hostile input may be rejected with
// an error or classified as malformed, but it must NEVER panic the bridge
// process (a bridge crash takes the wrapped agent CLI down with it).
//
// Seed corpus = the real conformance fixture lines (../../spec/conformance/
// cases.json, loaded best-effort) plus adversarial cases: truncated JSON,
// deep nesting, oversized strings, unicode / control / invalid-UTF-8 bytes,
// and exotic numbers (negative zero, 1e400). Seeds also run as unit tests
// under plain `go test`, so they are deliberately kept small and must pass.
//
// Run (Go executes one fuzz target per invocation):
//
//	GOPROXY=https://goproxy.cn,direct go test -run '^$' -fuzz FuzzClassifyLine -fuzztime 45s .

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// truncSeed keeps failure messages bounded when an input is megabytes long.
func truncSeed(s string) string {
	if len(s) > 120 {
		return s[:120] + "..."
	}
	return s
}

// addConformanceLineSeeds loads the wire-0.4 conformance stdout lines as fuzz
// seeds. Best-effort: a missing/unreadable fixture just means fewer seeds.
func addConformanceLineSeeds(f *testing.F) {
	raw, err := os.ReadFile(filepath.Join(conformanceDir, "cases.json"))
	if err != nil {
		return
	}
	var data struct {
		Cases []struct {
			Line string `json:"line"`
		} `json:"cases"`
	}
	if json.Unmarshal(raw, &data) != nil {
		return
	}
	for _, c := range data.Cases {
		f.Add(c.Line)
	}
}

// FuzzClassifyLine: any stdout line, valid or not, must classify without
// panicking and yield one of the five closed Kind values.
func FuzzClassifyLine(f *testing.F) {
	addConformanceLineSeeds(f)
	// Adversarial seeds.
	f.Add(`{"type":"partial","text":`) // truncated mid-value
	f.Add(`{"type":"partial","text":"` + strings.Repeat("A", 4096) + `"}`)
	f.Add(`{"type":"result","usage":` + strings.Repeat(`{"a":`, 256) + `1` + strings.Repeat(`}`, 256) + `}`)
	f.Add(`{"type":"partial","text":"tab\tnewline\n\rf"}`)
	f.Add(`{"type":"partial","text":"unicode 你好 🚀 🙂‍↔️"}`)
	f.Add("\xff\xfe\x80 not utf8")
	f.Add(`{"type":"partial","text":-0}`)
	f.Add(`{"type":"partial","text":1e400}`) // overflows float64 → invalid JSON
	f.Add(`{"type":"permission_request","request_id":"r","tool_name":"Bash","input":{"cmd":["a",1,null]}}`)
	f.Add("\ufeff{}") // BOM before JSON
	f.Add("{\"type\":\"partial\",\"text\":\"ctl:\\u0000\\u001f\\u007f\"}")
	f.Add(strings.Repeat("[", 2048))
	f.Add(`{"session_id":"` + strings.Repeat("../", 300) + `"}`)
	f.Add(`{"type":"error","usage":{"tokens":1e308},"session_id":"s"}`)

	f.Fuzz(func(t *testing.T, line string) {
		if obj := ParseJSONLine(line); obj != nil {
			_ = obj["type"] // decoding must not panic either
		}
		e := ClassifyLine(line)
		switch e.Kind {
		case KindPartial, KindResult, KindError, KindPermissionRequest, KindMalformed:
			// ok
		default:
			t.Fatalf("ClassifyLine(%q) returned unknown kind %q", truncSeed(line), e.Kind)
		}
	})
}

// FuzzNormalizeProfile: arbitrary YAML bytes through ProfileFromYAML. A yaml.v3
// panic on pathological input would be an upstream finding; anything else is
// ours. The deadline round-trip (ParseDeadline) is exercised here too so the
// normalization → deadline chain is covered in one pass.
func FuzzNormalizeProfile(f *testing.F) {
	f.Add([]byte("command: echo hi\ntimeout_secs: 30\nargs: [a, b]\nenv:\n  FOO: bar\n"))
	f.Add([]byte("agentproc:\n  command: x\n  deadline: 2025-01-01T00:00:00Z\n"))
	f.Add([]byte("command: x\ndeadline: \"not a date\"\n"))
	f.Add([]byte("command: x\ndeadline: 123\n"))
	f.Add([]byte("command: x\ndeadline: [1, 2]\n"))
	f.Add([]byte("command: x\ndeadline: {a: b}\n"))
	f.Add([]byte("command: x\ntimeout_secs: \"999\"\nkill_grace_secs: -5\nbudget_secs: 1e309\n"))
	f.Add([]byte("command: x\nargs: not-a-list\n"))
	f.Add([]byte("command: x\nenv: not-a-dict\n"))
	f.Add([]byte("executor: echo\ncwd: ~/x\npermission: true\nstreaming: false\n"))
	f.Add([]byte("a: &x 1\nb: *x\ncommand: echo\n")) // non-cyclic anchor
	f.Add([]byte("command: a\ncommand: b\n"))        // duplicate key
	f.Add([]byte("---\ncommand: x\n"))
	f.Add([]byte("\ufeffcommand: x\n")) // U+FEFF BOM before key
	f.Add([]byte("command: x\nargs:\n  - " + strings.Repeat("y", 2048) + "\n"))
	f.Add([]byte("k" + strings.Repeat("x", 2048) + ": " + strings.Repeat("v", 2048) + "\n"))
	f.Add([]byte("a: " + strings.Repeat("[", 512) + strings.Repeat("]", 512) + "\ncommand: x\n"))
	f.Add([]byte("command: x\ntimeout_secs: 999999999999999999999999\n"))
	f.Add([]byte("command: x\nbudget_secs: -0.0\ntimeout_secs: 1e300\n"))
	f.Add([]byte("command: x\nargs:\n  - {nested: [1, {deep: null}]}\n"))

	f.Fuzz(func(t *testing.T, raw []byte) {
		p, err := ProfileFromYAML(raw)
		if err != nil || p == nil {
			return // rejection is fine; panicking is not
		}
		_, _ = ParseDeadline(p.Deadline)
	})
}

// FuzzParseDeadline: any JSON value behind a "deadline" key, through
// NormalizeProfile then ParseDeadline — no panics, errors expected for junk.
func FuzzParseDeadline(f *testing.F) {
	seeds := []string{
		`"2025-01-01T00:00:00Z"`,
		`"2025-01-01 12:00:00+08:00"`, // space separator (Python-compatible)
		`"2025-01-01T00:00:00.123456789+00:00"`,
		`"0000-01-01T00:00:00Z"`,
		`"9999-12-31T23:59:59+23:59"`,
		`"2025-02-30T00:00:00Z"`,
		`"2025-01-01T00:00:00z"`, // lowercase z
		`""`,
		`" "`,
		`"T"`,
		`"12345"`,
		`"2025-13-45T99:99:99+25:00"`,
		`123`,
		`-0`,
		`1.5`,
		`true`,
		`null`,
		`[1, {"a": ["x"]}]`,
		`{"deadline": {"deep": [true, false, null]}}`,
		`{"command": "x", "deadline": 12345}`,
		`{"agentproc": {"command": "x", "deadline": "2020-01-01 00:00:00+00:00"}}`,
		`{"command": "x", "timeout_secs": 5, "budget_secs": 1.5, "deadline": "nope"}`,
	}
	for _, s := range seeds {
		f.Add([]byte(s))
	}

	f.Fuzz(func(t *testing.T, data []byte) {
		var v any
		if err := json.Unmarshal(data, &v); err != nil {
			return
		}
		var raw map[string]any
		if m, ok := v.(map[string]any); ok {
			raw = m // fuzzer controls every key
		} else {
			raw = map[string]any{"command": "true", "deadline": v}
		}
		p, err := NormalizeProfile(raw)
		if err != nil || p == nil {
			return
		}
		_, _ = ParseDeadline(p.Deadline)
	})
}

// FuzzIsValidSessionIDString: the storage-safety gate on session ids —
// regexp + path-traversal checks on arbitrary strings, including huge ones.
func FuzzIsValidSessionIDString(f *testing.F) {
	f.Add("sess-1")
	f.Add(".")
	f.Add("..")
	f.Add("a/b")
	f.Add(`a\b`)
	f.Add("colons:and:slashes-ok")
	f.Add("multi word id")
	f.Add("unicode 你好 🚀")
	f.Add("a\x00b")
	f.Add("\x1f")
	f.Add(strings.Repeat("../", 1024))
	f.Add(strings.Repeat("x", 4096))
	f.Add("\ufeffid")

	f.Fuzz(func(t *testing.T, s string) {
		ok := IsValidSessionIDString(s)
		if ok != IsValidSessionID(s) {
			t.Fatalf("IsValidSessionIDString(%q)=%v disagrees with IsValidSessionID", truncSeed(s), ok)
		}
		if ok && (s == "" || s == "." || s == "..") {
			t.Fatalf("IsValidSessionIDString accepted %q", s)
		}
	})
}

// FuzzFormatPermissionResponse: an arbitrary JSON object through the
// permission-request validators and the stdin response formatter. The emitted
// line must itself be parseable JSON (the bridge writes it to the agent's
// stdin; unparseable output would hang the permission round-trip).
func FuzzFormatPermissionResponse(f *testing.F) {
	seeds := []string{
		`{"request_id":"1","behavior":"allow","tool_name":"Bash","input":{}}`,
		`{"request_id":2,"behavior":"deny"}`,
		`{"behavior":"allow","updated_input":{"cmd":"ls"},"message":"ok"}`,
		`{"request_id":{"nested":[1,2]},"behavior":null}`,
		`{"request_id":"f","message":"\n\t"}`,
		`{"updated_input":42}`,
		`{"behavior":"ALLOW"}`,
		`{"request_id":-0}`,
		`{"request_id":"r","tool_name":"Bash","input":{"deep":{"deeper":[1,{"x":null}]}}}`,
		`{"session_id":"../etc/passwd","request_id":"r","tool_name":"t","input":{}}`,
		`{"request_id":"` + strings.Repeat("r", 2048) + `"}`,
	}
	for _, s := range seeds {
		f.Add([]byte(s))
	}

	f.Fuzz(func(t *testing.T, data []byte) {
		var m map[string]any
		if err := json.Unmarshal(data, &m); err != nil {
			return
		}
		_ = IsValidPermissionRequest(m)
		_ = IsValidSessionID(m["session_id"])
		out := FormatPermissionResponse(m)
		if out == "" {
			return
		}
		var back map[string]any
		if err := json.Unmarshal([]byte(out), &back); err != nil {
			t.Fatalf("FormatPermissionResponse emitted non-JSON %q: %v", truncSeed(out), err)
		}
		if back["type"] != "permission_response" {
			t.Fatalf("FormatPermissionResponse wrong type field: %q", truncSeed(out))
		}
	})
}

// FuzzHubProfileFromYAML: hostile hub profile.yaml text through the metadata
// parser — must fall back gracefully, never panic, never lose the name.
func FuzzHubProfileFromYAML(f *testing.F) {
	f.Add("name: claude-code\ndescription: Claude Code bridge\ncli: claude\ntested: 2026-01-01")
	f.Add("description: [a, b]")
	f.Add("description: {x: 1}")
	f.Add("name: 123\ntested: true")
	f.Add("name: \"\"\ndescription: null")
	f.Add("{")
	f.Add("a: " + strings.Repeat("[", 512) + strings.Repeat("]", 512))
	f.Add(strings.Repeat("d", 4096))
	f.Add("\xff\xfe")
	f.Add("- a\n- b")
	f.Add("&a [*a]") // cyclic alias (self-referencing sequence)

	f.Fuzz(func(t *testing.T, text string) {
		p := hubProfileFromYAML("fuzz-fallback", text)
		if p.Name == "" {
			t.Fatal("hubProfileFromYAML dropped the fallback name")
		}
	})
}

// FuzzHubTree: hostile jsDelivr data-API responses through the tree
// flattener (recursive) and the metadata parser. flattenJsDelivrTree recurses
// per directory level; encoding/json caps nesting at 10000, so this also
// probes the recursion bound.
func FuzzHubTree(f *testing.F) {
	f.Add([]byte(`{"files":[{"type":"directory","name":"hub","files":[{"type":"blob","name":"profile.yaml"},{"type":"directory","name":"_shared","files":[{"type":"blob","name":"stream_utils.py"}]}]}]}`))
	f.Add([]byte(`{"files":null}`))
	f.Add([]byte(`{"files":[null,42,"x",[1],{}]}`))
	f.Add([]byte(`{"files":[{"type":"directory","name":"a"}]}`))
	f.Add([]byte(`{"files":[{"name":"` + strings.Repeat("d/", 512) + `"}]}`))
	f.Add([]byte(`{"files":` + strings.Repeat(`[{"type":"directory","name":"n","files":`, 128) + `[]` + strings.Repeat(`}]`, 128) + `}`))
	f.Add([]byte(`{"files":[{"type":"DIRECTORY","name":"Ünïcode 🚀"}]}`))
	f.Add([]byte(`{"files":[{"type":"blob","name":"a"},{"type":"blob","name":"a"}]}`))
	f.Add([]byte("not json at all"))
	f.Add([]byte(`{"files":[{"type":"blob","name":""}]}`))

	f.Fuzz(func(t *testing.T, data []byte) {
		var m map[string]any
		if err := json.Unmarshal(data, &m); err != nil {
			return
		}
		files, _ := m["files"].([]any)
		tree := flattenJsDelivrTree(files, "")
		for _, e := range tree {
			if e.Type != "blob" && e.Type != "tree" {
				t.Fatalf("flattenJsDelivrTree produced unknown entry type %q", e.Type)
			}
		}
		_ = hubProfileFromYAML("fuzz", string(data))
	})
}
