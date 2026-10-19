// Package agentproc is the Go implementation of the AgentProc protocol
// (spec/protocol.md, wire protocol 0.4).
//
// The SDK covers both sides of the process boundary:
//
//   - Bridge side (agentproc.Run): load a profile, spawn the agent process,
//     deliver the turn on stdin, read the NDJSON event stream from stdout,
//     assemble the reply, enforce timeouts / budgets / deadlines with a
//     process-group kill, and surface session ids, usage, and errors.
//   - Agent side (agentproc.Serve): write an agent as a Go function; Serve
//     reads the turn object from stdin, calls the handler, and emits the
//     protocol events on stdout.
//
// The protocol is the source of truth (spec/protocol.md). Wire 0.4 is NDJSON
// in both directions: one {"type":"turn",...} line on stdin (then optional
// permission_response frames when permission is on), and one JSON object per
// line on stdout discriminated by "type": partial | result | error |
// permission_request.
package agentproc

// SDKVersion is the Go SDK's own version track (independent from the Python,
// Node, and Rust packages, which are released separately).
const SDKVersion = "0.1.2"

// PROTOCOL_VERSION is re-exported from runner.go where the single literal
// definition lives (mirrors the Python/Node convention that the runner owns
// the constant and the entry point imports it).
