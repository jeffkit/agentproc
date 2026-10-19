// Command sdkharness is the SDK-entry conformance harness (Go side): it maps
// a scenario kind from spec/conformance/sdk.json to a handler and runs it
// under agentproc.Serve. The conformance test (sdk_test.go) builds and spawns
// this binary and asserts the exact stdout NDJSON + exit code.
//
// The fixture's async_*/sync_* distinction is a Python/Node concern (sync vs
// async handlers); Go handlers are synchronous by construction, so both
// families map here — covering the same observable return/error paths.
package main

import (
	"os"

	agentproc "github.com/jeffkit/agentproc/sdk/go"
)

func main() {
	kind := ""
	if len(os.Args) > 1 {
		kind = os.Args[1]
	}
	handlers := map[string]agentproc.Handler{
		"async_string": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			return &agentproc.AgentResult{Response: "hello world"}, nil
		},
		"async_result": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			return &agentproc.AgentResult{Response: "hi there", SessionID: "sess-abc"}, nil
		},
		"async_none_partial": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			if err := ctx.SendPartial("streaming chunk"); err != nil {
				return nil, err
			}
			return nil, nil
		},
		"async_protocol_error": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			return nil, &agentproc.ProtocolError{Message: "bad thing"}
		},
		"async_send_error_then_return": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			if err := ctx.SendError("warn"); err != nil {
				return nil, err
			}
			return &agentproc.AgentResult{Response: "after"}, nil
		},
		"sync_string": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			return &agentproc.AgentResult{Response: "hello from sync"}, nil
		},
		"sync_partial_bare": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			// Bare call (no channel semantics in Go) — the write happens at
			// call time, then the handler returns the body.
			if err := ctx.SendPartial("sync chunk"); err != nil {
				return nil, err
			}
			return &agentproc.AgentResult{Response: "done"}, nil
		},
		"async_partial_with_role": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			if err := ctx.SendPartial("thinking...", "thinking"); err != nil {
				return nil, err
			}
			return &agentproc.AgentResult{Response: "answer"}, nil
		},
		"async_permission_roundtrip": func(ctx *agentproc.Context) (*agentproc.AgentResult, error) {
			if err := ctx.SendPermissionRequest(agentproc.PermissionRequest{
				RequestID:   "r1",
				ToolName:    "Bash",
				Input:       []byte(`{"command":"echo ok"}`),
				Description: "echo",
			}); err != nil {
				return nil, err
			}
			resp := ctx.ReadPermissionResponse()
			behavior := "none"
			if resp != nil {
				if b, ok := resp["behavior"].(string); ok && b != "" {
					behavior = b
				}
			}
			return &agentproc.AgentResult{Response: "perm=" + behavior}, nil
		},
	}
	handler, ok := handlers[kind]
	if !ok {
		os.Stderr.WriteString("unknown kind: " + kind + "\n")
		os.Exit(2)
	}
	os.Exit(agentproc.Serve(handler))
}
