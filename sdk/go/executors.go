package agentproc

import (
	"sync"
)

// TurnCtx carries the profile's permission value into BuildArgs (spec
// "Executor interface" — the ctx argument).
type TurnCtx struct {
	Permission bool
}

// ParseResult is what ParseEvent returns for one decoded CLI stdout event.
// All fields optional; a nil *ParseResult means "this event contributes
// nothing". PartialText and Error MUST NOT both be set — an error is terminal.
type ParseResult struct {
	// PartialText is a streaming chunk (forwarded as {"type":"partial"} and
	// accumulated for the final body).
	PartialText *string
	// FinalText is the terminal reply body; &"" (pointer to empty) is an
	// explicit "no text body this event" and counts as the turn's result.
	FinalText *string
	// SessionID is a session id to persist (first non-empty wins).
	SessionID string
	// Error is a terminal error message (the turn fails even on exit 0).
	Error string
	// Usage is token/cost stats attached to the run result.
	Usage map[string]any
}

// Executor is a named, in-process implementation of the bridge side of the
// protocol (spec "In-process executors"). The runner spawns the target CLI
// directly — no bridge subprocess, no shell — and decodes its output with
// ParseEvent (or, for plain CLI executors, treats stdout as the body).
type Executor interface {
	// CLIName is the CLI binary name, used in error messages.
	CLIName() string
	// InstallHint is appended to "CLI not found" errors.
	InstallHint() string
	// Plain is true when the CLI emits plain text (not NDJSON): the entire
	// stdout becomes the reply body and ParseEvent is never called.
	Plain() bool
	// SupportsPermission reports a mid-turn approval channel. The runner
	// refuses to run a profile with permission: true against an executor
	// that does not declare it.
	SupportsPermission() bool
	// BuildArgs builds the target CLI's argv. The returned argv is passed to
	// the OS without a shell; an empty return is a hard error.
	BuildArgs(message, sessionID string, env map[string]string, ctx TurnCtx) ([]string, error)
	// ParseEvent translates one decoded JSON object from the CLI's stdout.
	// Return nil for events the executor does not recognise. Unused when
	// Plain is true.
	ParseEvent(event map[string]any) *ParseResult
}

// PlainOutputParser is an optional Executor extension for Plain() executors
// whose stdout is not the reply itself but an envelope carrying it — e.g.
// recursive's native plain mode, where the trailing JSON result object holds
// the reply text plus session_id / usage / is_error. After the child exits
// and the pipes are drained, the runner hands the assembled plain stdout to
// ParsePlainOutput once; a non-nil result overrides the reply (FinalText)
// and contributes SessionID (wire-validated), Usage, and a terminal Error
// (a turn that reports is_error fails even on exit 0).
type PlainOutputParser interface {
	ParsePlainOutput(stdout string) *ParseResult
}

// TurnFactory is the optional makeHandlers equivalent: stateful executors
// (per-turn parse state, a session id minted in BuildArgs) implement it and
// the runner calls MakeTurn() once per turn, using the returned handlers for
// that turn only. Executors without it are used directly and MUST be
// stateless / re-entrant.
type TurnFactory interface {
	MakeTurn() *TurnHandlers
}

// InitialStdinBuilder is the optional buildInitialStdin hook: a non-nil
// return makes the runner pipe the CLI's stdin, write the payload plus one
// "\n", and close it — the interface's way of keeping the user message out
// of argv (spec "Message delivery and argv"). A nil payload leaves the CLI's
// stdin on the null device.
type InitialStdinBuilder interface {
	BuildInitialStdin(message, sessionID string) (*string, error)
}

// SessionIDGetter is the optional getSessionId hook for plain executors: the
// session id this turn's BuildArgs minted or reused, read by the runner once
// after the CLI exits.
type SessionIDGetter interface {
	GetSessionID() string
}

// TurnHandlers is the closed turn-local handler set. The runner reads it off
// TurnFactory.MakeTurn() when implemented, else synthesises it from the
// executor's own methods (plus the optional interfaces above).
type TurnHandlers struct {
	BuildArgs         func(message, sessionID string, env map[string]string, ctx TurnCtx) ([]string, error)
	ParseEvent        func(event map[string]any) *ParseResult
	GetSessionID      func() string
	BuildInitialStdin func(message, sessionID string) (*string, error)
}

// registry — name → factory. A factory (not an instance) so each turn gets a
// fresh executor object: stateful executors cannot leak state across turns.
var (
	registryMu    sync.RWMutex
	registry      = map[string]func() Executor{}
	registryOrder []string
)

// RegisterExecutor installs an executor factory under a profile `executor:`
// name. Registering an existing name replaces it. Call from an init() for
// application-local executors; the built-in registry (claude-code, codex, …)
// is registered the same way.
func RegisterExecutor(name string, factory func() Executor) {
	registryMu.Lock()
	defer registryMu.Unlock()
	if _, exists := registry[name]; !exists {
		registryOrder = append(registryOrder, name)
	}
	registry[name] = factory
}

func lookupExecutor(name string) (func() Executor, bool) {
	registryMu.RLock()
	defer registryMu.RUnlock()
	f, ok := registry[name]
	return f, ok
}

// ExecutorNames lists the registered executor names in registration order
// (built-ins first, mirroring the Python/Node registry listing).
func ExecutorNames() []string {
	registryMu.RLock()
	defer registryMu.RUnlock()
	out := make([]string, len(registryOrder))
	copy(out, registryOrder)
	return out
}

// resolveTurnHandlers applies the makeHandlers rule: a stateful executor
// provides a fresh handler set per turn; a stateless one is synthesised from
// its own methods plus the optional interfaces.
func resolveTurnHandlers(executor Executor) *TurnHandlers {
	if factory, ok := executor.(TurnFactory); ok {
		if h := factory.MakeTurn(); h != nil {
			return h
		}
	}
	h := &TurnHandlers{
		BuildArgs:  executor.BuildArgs,
		ParseEvent: executor.ParseEvent,
	}
	if g, ok := executor.(SessionIDGetter); ok {
		h.GetSessionID = g.GetSessionID
	}
	if b, ok := executor.(InitialStdinBuilder); ok {
		h.BuildInitialStdin = b.BuildInitialStdin
	}
	return h
}

func strPtr(s string) *string { return &s }
