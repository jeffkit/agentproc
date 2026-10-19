package agentproc

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"sync"
)

// ProtocolError is the fatal error path: a handler that returns one gets its
// message emitted as {"type":"error",...} and the process exits non-zero.
// (The non-fatal path is ctx.SendError — non-terminal, the handler may
// continue and return a body, which the bridge discards per the spec's
// "result after error" rule.)
type ProtocolError struct{ Message string }

func (e *ProtocolError) Error() string {
	if e == nil || e.Message == "" {
		return "unknown error"
	}
	return e.Message
}

// AgentResult is the handler's return value. Returning a nil *AgentResult
// (with a nil error) means everything was already signalled via
// SendPartial / SendError — no result event is emitted.
type AgentResult struct {
	// Response is the final reply body.
	Response string
	// SessionID is persisted by the bridge and passed back next turn.
	SessionID string
}

// PermissionRequest is the agent's ask for tool-execution authorization
// (spec "Optional tool permission"). RequestID, ToolName, and Input are
// required; Description and ToolUseID are optional.
type PermissionRequest struct {
	RequestID   string
	ToolName    string
	Input       json.RawMessage
	Description string
	ToolUseID   string
}

// Context is the per-turn input passed to a Serve handler, plus the outbound
// event channel. All writes happen at call time (flushed immediately).
type Context struct {
	Message         string
	SessionID       string
	SessionName     string
	ProtocolVersion string
	Attachments     []any
	Permission      bool

	mu     sync.Mutex
	stdout *os.File
	stdin  *bufio.Reader
}

// SendPartial writes a {"type":"partial","text":...} line to stdout and
// flushes at call time; the bridge forwards it to the user without waiting
// for the process to exit. The optional role (e.g. "output" / "thinking")
// is forwarded as-is and MAY be rendered differently by the bridge. An empty
// text writes nothing.
func (c *Context) SendPartial(text string, role ...string) error {
	if text == "" {
		return nil
	}
	evt := orderedObject{{"type", "partial"}, {"text", text}}
	if len(role) > 0 && role[0] != "" {
		evt = append(evt, orderedField{"role", role[0]})
	}
	return c.writeLine(marshalCompact(evt))
}

// SendError writes a {"type":"error","message":...} line to stdout. Honored
// regardless of streaming mode, and non-terminal: the handler may continue
// and return a body (the bridge discards a result that follows an error).
// For a fatal path, return a *ProtocolError instead. An empty text writes
// nothing.
func (c *Context) SendError(text string) error {
	if text == "" {
		return nil
	}
	return c.writeLine(marshalCompact(orderedObject{
		{"type", "error"},
		{"message", text},
	}))
}

// SendPermissionRequest sends a tool-permission request to the bridge. Only
// valid when ctx.Permission is true (the profile set permission: true and
// the bridge enabled the channel). The matching decision arrives on stdin —
// read it with ReadPermissionResponse.
func (c *Context) SendPermissionRequest(req PermissionRequest) error {
	if !c.Permission {
		return fmt.Errorf("ctx.SendPermissionRequest() requires profile.permission: true — the bridge would otherwise not honor the request")
	}
	if req.RequestID == "" {
		return fmt.Errorf("req.RequestID is required")
	}
	evt := orderedObject{
		{"type", "permission_request"},
		{"request_id", req.RequestID},
		{"tool_name", req.ToolName},
	}
	if len(req.Input) > 0 {
		evt = append(evt, orderedField{"input", json.RawMessage(req.Input)})
	} else {
		evt = append(evt, orderedField{"input", map[string]any{}})
	}
	if req.Description != "" {
		evt = append(evt, orderedField{"description", req.Description})
	}
	if req.ToolUseID != "" {
		evt = append(evt, orderedField{"tool_use_id", req.ToolUseID})
	}
	return c.writeLine(marshalCompact(evt))
}

// ReadPermissionResponse blocks until the next permission_response frame
// arrives on stdin (or EOF). Returns nil at EOF or on a malformed line.
// Only meaningful when ctx.Permission is true — in the default
// permission:false mode the bridge closes stdin after the turn line, so this
// returns nil immediately.
func (c *Context) ReadPermissionResponse() map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdin == nil {
		return nil
	}
	line, err := c.stdin.ReadString('\n')
	if err != nil && line == "" {
		return nil
	}
	var v map[string]any
	if json.Unmarshal([]byte(line), &v) != nil {
		return nil
	}
	return v
}

func (c *Context) writeLine(line string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdout == nil {
		return fmt.Errorf("agentproc: stdout not available")
	}
	_, err := c.stdout.WriteString(line + "\n")
	return err
}

// Handler is the agent's per-turn function. Return a *AgentResult (or nil
// when everything was signalled via ctx), and a *ProtocolError to fail the
// turn with a user-readable message.
type Handler func(ctx *Context) (*AgentResult, error)

// Serve reads the turn object from stdin, calls the handler, writes the
// protocol events to stdout, and returns the process exit code. Wire 0.4:
// exit 0 success, 1 error. Typical main:
//
//	os.Exit(agentproc.Serve(handler))
func Serve(handler Handler) int {
	return serve(os.Stdin, os.Stdout, os.Stderr, handler)
}

func serve(stdin *os.File, stdout, stderr *os.File, handler Handler) int {
	ctx := readTurn(bufio.NewReader(stdin), stdout, stderr)

	ret, err := handler(ctx)
	if err != nil {
		if pe, ok := err.(*ProtocolError); ok {
			message := pe.Error()
			line := marshalCompact(orderedObject{{"type", "error"}, {"message", message}})
			stdout.WriteString(line + "\n")
			return 1
		}
		fmt.Fprintf(stderr, "[agentproc] handler error: %v\n", err)
		return 1
	}
	if ret == nil {
		return 0
	}
	if ret.Response != "" || ret.SessionID != "" {
		evt := orderedObject{{"type", "result"}, {"text", ret.Response}}
		if ret.SessionID != "" {
			evt = append(evt, orderedField{"session_id", ret.SessionID})
		}
		stdout.WriteString(marshalCompact(evt) + "\n")
	}
	return 0
}

func readTurn(reader *bufio.Reader, stdout, stderr *os.File) *Context {
	ctx := &Context{
		Message:         "",
		SessionID:       "",
		SessionName:     "default",
		ProtocolVersion: PROTOCOL_VERSION,
		Attachments:     []any{},
		stdout:          stdout,
		stdin:           reader,
	}
	line, err := reader.ReadString('\n')
	if err != nil && line == "" {
		return ctx
	}
	var t map[string]any
	if json.Unmarshal([]byte(line), &t) != nil || t == nil {
		return ctx
	}
	if s, ok := t["message"].(string); ok {
		ctx.Message = s
	}
	if s, ok := t["session_id"].(string); ok {
		ctx.SessionID = s
	}
	if s, ok := t["session_name"].(string); ok {
		ctx.SessionName = s
	}
	if s, ok := t["protocol_version"].(string); ok && s != "" {
		ctx.ProtocolVersion = s
		// Diagnostic only (spec Versioning): one stderr line, then continue
		// best-effort — it never gates or alters what the handler may send.
		if s != PROTOCOL_VERSION && stderr != nil {
			fmt.Fprintf(stderr, "[agentproc] protocol_version %q does not match this agent SDK's %q; continuing best-effort (fail-soft)\n", s, PROTOCOL_VERSION)
		}
	} else {
		ctx.ProtocolVersion = PROTOCOL_VERSION
	}
	if list, ok := t["attachments"].([]any); ok {
		ctx.Attachments = list
	}
	perm, _ := t["permission"].(bool)
	ctx.Permission = perm
	return ctx
}
