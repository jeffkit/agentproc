package agentproc

import (
	"bytes"
	"encoding/json"
	"regexp"
	"strings"
	"unicode"
)

// Event kinds returned by ClassifyLine. The set is closed (wire 0.4): partial,
// result, error, permission_request, plus malformed for anything that is not a
// valid JSON object event. Legacy wire 0.3 "session"/"text" events are
// malformed in 0.4.
const (
	KindPartial           = "partial"
	KindResult            = "result"
	KindError             = "error"
	KindPermissionRequest = "permission_request"
	KindMalformed         = "malformed"
)

// Event is one classified stdout line.
//
//   - Kind: one of the Kind* constants.
//   - Value: the partial/result "text" (or "" when missing or not a string),
//     the error "message", the raw line for malformed, or (for
//     permission_request) unused — the decoded object is in Obj.
//   - Role: the partial's "role" field; nil when absent or not a string.
//   - SessionID: the optional non-empty "session_id" carried by the event
//     ("" when absent). Not validated here — validity is a runner concern.
//   - Usage: the optional "usage" object on result/error events.
//   - Obj: the whole decoded object (permission_request only).
type Event struct {
	Kind      string
	Value     string
	Role      *string
	SessionID string
	Usage     map[string]any
	Obj       map[string]any
}

// ParseJSONLine parses one stdout line as a JSON object; nil on failure
// (empty line, invalid JSON, or valid JSON that is not an object).
func ParseJSONLine(line string) map[string]any {
	text := strings.TrimSpace(line)
	if text == "" {
		return nil
	}
	var v any
	if err := json.Unmarshal([]byte(text), &v); err != nil {
		return nil
	}
	obj, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	return obj
}

// ClassifyLine classifies one stdout line into a typed event (wire 0.4).
//
// A conformant bridge treats every stdout line as JSON: partial / result /
// error / permission_request by "type", everything else (non-JSON, non-object,
// missing or unknown "type" — including legacy "session"/"text") as malformed,
// which is logged and ignored — never appended to the reply body.
func ClassifyLine(line string) Event {
	obj := ParseJSONLine(line)
	if obj == nil {
		return Event{Kind: KindMalformed, Value: line}
	}
	t, ok := obj["type"].(string)
	if !ok {
		return Event{Kind: KindMalformed, Value: line}
	}
	attachSession := func(e *Event) {
		if sid, ok := obj["session_id"].(string); ok && sid != "" {
			e.SessionID = sid
		}
	}
	attachUsage := func(e *Event) {
		if usage, ok := obj["usage"].(map[string]any); ok {
			e.Usage = usage
		}
	}
	switch t {
	case KindPartial:
		e := Event{Kind: KindPartial}
		if s, ok := obj["text"].(string); ok {
			e.Value = s
		}
		if r, ok := obj["role"].(string); ok {
			e.Role = &r
		}
		attachSession(&e)
		return e
	case KindResult:
		e := Event{Kind: KindResult}
		if s, ok := obj["text"].(string); ok {
			e.Value = s
		}
		attachUsage(&e)
		attachSession(&e)
		return e
	case KindError:
		e := Event{Kind: KindError}
		if s, ok := obj["message"].(string); ok {
			e.Value = s
		}
		attachUsage(&e)
		attachSession(&e)
		return e
	case KindPermissionRequest:
		return Event{Kind: KindPermissionRequest, Obj: obj}
	default:
		return Event{Kind: KindMalformed, Value: line}
	}
}

// _SESSION_ID_RE mirror: the storage-safety constraint enforced at the wire
// classification step — a session id containing a path separator, or a NUL /
// control character, would path-traverse out of the flat <id>.jsonl sessions
// directory. Colons, spaces, "+", and unicode are all fine.
var sessionIDForbidden = regexp.MustCompile(`[\/\\\x00-\x1f]`)

// IsValidSessionID reports whether value is a wire-valid session id:
// non-empty, no path separators or control characters, and not "." or "..".
func IsValidSessionID(value any) bool {
	s, ok := value.(string)
	if !ok || s == "" {
		return false
	}
	return IsValidSessionIDString(s)
}

// IsValidSessionIDString is IsValidSessionID for a value already known to be
// a string.
func IsValidSessionIDString(s string) bool {
	if s == "" || s == "." || s == ".." {
		return false
	}
	return !sessionIDForbidden.MatchString(s)
}

var whitespaceControlRe = regexp.MustCompile(`[\s\r\n\x00-\x1f]`)

// IsValidPermissionRequest reports whether obj carries the required
// permission_request fields: a non-blank request_id without whitespace or
// control characters, a non-empty tool_name, and an object input.
func IsValidPermissionRequest(obj any) bool {
	m, ok := obj.(map[string]any)
	if !ok {
		return false
	}
	rid, ok := m["request_id"].(string)
	if !ok || strings.TrimSpace(rid) == "" {
		return false
	}
	if whitespaceControlRe.MatchString(rid) {
		return false
	}
	tool, ok := m["tool_name"].(string)
	if !ok || tool == "" {
		return false
	}
	if _, ok := m["input"].(map[string]any); !ok {
		return false
	}
	return true
}

// FormatPermissionResponse renders a {"type":"permission_response",...} line
// for stdin from a bridge-side decision. behavior is "allow" or anything else
// (which is rendered as "deny", mirroring the reference runners). An
// UpdatedInput that is non-nil and an object is echoed as "updated_input" —
// when the bridge omits it the agent MUST fall back to the request's original
// input, so the runner never auto-fills it.
func FormatPermissionResponse(decision map[string]any) string {
	payload := orderedObject{
		{"type", "permission_response"},
		{"request_id", toString(decision["request_id"])},
		{"behavior", "deny"},
	}
	if decision["behavior"] == "allow" {
		payload[2].Value = "allow"
	}
	if updated, ok := decision["updated_input"].(map[string]any); ok {
		payload = append(payload, orderedField{"updated_input", updated})
	}
	if message, ok := decision["message"].(string); ok && message != "" {
		payload = append(payload, orderedField{"message", message})
	}
	return marshalCompact(payload)
}

// orderedField / orderedObject: JSON objects whose key order is the
// insertion order. The reference SDKs serialize compactly with fixed key
// order (Python dict / JS object insertion order); the conformance fixtures
// assert exact bytes, so emitted events use these instead of maps.
type orderedField struct {
	Key   string
	Value any
}

type orderedObject []orderedField

// MarshalJSON renders the object with keys in insertion order and HTML
// escaping off (Python's ensure_ascii=False / Node's JSON.stringify analog).
func (o orderedObject) MarshalJSON() ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, f := range o {
		if i > 0 {
			buf.WriteByte(',')
		}
		k, err := json.Marshal(f.Key)
		if err != nil {
			return nil, err
		}
		buf.Write(k)
		buf.WriteByte(':')
		v, err := marshalCompactValue(f.Value)
		if err != nil {
			return nil, err
		}
		buf.Write(v)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

func marshalCompact(v any) string {
	b, err := marshalCompactValue(v)
	if err != nil {
		return ""
	}
	return string(b)
}

func marshalCompactValue(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	// Encode appends a newline; the caller decides on line endings.
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

func toString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if v == nil {
		return ""
	}
	b, err := marshalCompactValue(v)
	if err != nil {
		return ""
	}
	return string(b)
}

// hasControlChar reports whether s contains an ASCII control character
// (U+0000–U+001F). Used by the permission-request diagnostics path.
func hasControlChar(s string) bool {
	for _, r := range s {
		if r <= unicode.MaxASCII && r < 0x20 {
			return true
		}
	}
	return false
}
