package agentproc

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"
)

// Built-in executor registry — a faithful port of the Python/Node SDKs'
// executors (sdk/python/src/agentproc/executors.py): same argv shapes, same
// env knobs, same parse rules, same posture capabilities (which executors
// declare a permission channel is pinned by spec/conformance/cases.json).
//
// All registrations live in init() so a binary that imports this package
// speaks to every supported CLI.

func init() {
	RegisterExecutor("claude-code", func() Executor { return &claudeCodeExecutor{} })
	RegisterExecutor("codebuddy", func() Executor { return &codebuddyExecutor{} })
	RegisterExecutor("codex", func() Executor { return &codexExecutor{} })
	RegisterExecutor("cursor", func() Executor { return &cursorExecutor{} })
	RegisterExecutor("gemini-cli", func() Executor { return &geminiCLIExecutor{} })
	RegisterExecutor("grok-build", func() Executor { return &grokBuildExecutor{} })
	RegisterExecutor("kimi-code", func() Executor { return &kimiCodeExecutor{} })
	RegisterExecutor("opencode", func() Executor { return &opencodeExecutor{} })
	RegisterExecutor("qwen-code", func() Executor { return &qwenCodeExecutor{} })
	RegisterExecutor("agy", func() Executor { return &agyExecutor{} })
	RegisterExecutor("aider", func() Executor { return &aiderExecutor{} })
	RegisterExecutor("deepseek", func() Executor { return &deepseekExecutor{} })
	RegisterExecutor("dsh", func() Executor { return &dshExecutor{} })
	RegisterExecutor("pi", func() Executor { return &piExecutor{} })
}

// envKnob reads an environment knob with the runner-side default (trimmed).
func envKnob(env map[string]string, name, fallback string) string {
	if v, ok := env[name]; ok {
		return strings.TrimSpace(v)
	}
	return fallback
}

// newUUIDv4 mints a stable per-turn session id for plain executors whose CLI
// takes a conversation flag (kimi-code, agy).
func newUUIDv4() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// messageContentBlocks joins the text blocks of an assistant message event
// (claude-code / codebuddy / cursor stream-json shape).
func messageContentBlocks(event map[string]any) string {
	msg, _ := event["message"].(map[string]any)
	content, _ := msg["content"].([]any)
	var sb strings.Builder
	for _, block := range content {
		b, ok := block.(map[string]any)
		if !ok || b["type"] != "text" {
			continue
		}
		if s, ok := b["text"].(string); ok {
			sb.WriteString(s)
		}
	}
	return sb.String()
}

// ---------------------------------------------------------------------------
// claude-code — the one executor with a mid-turn approval channel. Both modes
// deliver the user turn on stdin as one stream-json user line (the message
// never lands in argv).
// ---------------------------------------------------------------------------

type claudeCodeExecutor struct{}

func (e *claudeCodeExecutor) CLIName() string { return "claude" }
func (e *claudeCodeExecutor) InstallHint() string {
	return "Install: npm install -g @anthropic-ai/claude-code"
}
func (e *claudeCodeExecutor) Plain() bool              { return false }
func (e *claudeCodeExecutor) SupportsPermission() bool { return true }

func (e *claudeCodeExecutor) BuildArgs(message, sessionID string, env map[string]string, ctx TurnCtx) ([]string, error) {
	disallow := envKnob(env, "CLAUDE_DISALLOW_TOOLS", "AskUserQuestion")
	model := envKnob(env, "CLAUDE_MODEL", "")
	var args []string
	if ctx.Permission {
		// Bidirectional stream-json + stdio permission tool.
		args = []string{
			"claude", "--print",
			"--output-format", "stream-json",
			"--input-format", "stream-json",
			"--verbose",
			"--permission-prompt-tool", "stdio",
			"--permission-mode", "default",
		}
	} else {
		// Unattended: same stream-json input channel as the permission mode,
		// so the message never lands in argv.
		args = []string{
			"claude", "--print",
			"--output-format", "stream-json",
			"--input-format", "stream-json",
			// The claude CLI hard-requires --verbose with --print + stream-json.
			"--verbose",
			"--dangerously-skip-permissions",
		}
	}
	if disallow != "" {
		args = append(args, "--disallowed-tools", disallow)
	}
	if model != "" {
		args = append(args, "--model", model)
	}
	if sessionID != "" {
		args = append(args, "--resume", sessionID)
	}
	return args, nil
}

func (e *claudeCodeExecutor) ParseEvent(event map[string]any) *ParseResult {
	etype, _ := event["type"].(string)
	switch {
	case etype == "system" && event["subtype"] == "init":
		if sid, ok := event["session_id"].(string); ok && sid != "" {
			return &ParseResult{SessionID: sid}
		}
		return nil
	case etype == "assistant":
		if text := messageContentBlocks(event); text != "" {
			return &ParseResult{PartialText: strPtr(text)}
		}
		return nil
	case etype == "result":
		sid, _ := event["session_id"].(string)
		if isErr, _ := event["is_error"].(bool); isErr {
			msg, _ := event["result"].(string)
			if msg == "" {
				msg = "claude reported an error"
			}
			return &ParseResult{SessionID: sid, Error: msg}
		}
		out := &ParseResult{SessionID: sid}
		if s, ok := event["result"].(string); ok && s != "" {
			out.FinalText = strPtr(s)
		}
		return out
	}
	return nil
}

// BuildInitialStdin is the stream-json user turn Claude reads from stdin in
// both modes.
func (e *claudeCodeExecutor) BuildInitialStdin(message, sessionID string) (*string, error) {
	frame, err := json.Marshal(orderedObject{
		{"type", "user"},
		{"message", orderedObject{{"role", "user"}, {"content", message}}},
		{"parent_tool_use_id", nil},
		{"session_id", sessionID},
	})
	if err != nil {
		return nil, err
	}
	return strPtr(string(frame)), nil
}

// ---------------------------------------------------------------------------
// codebuddy
// ---------------------------------------------------------------------------

type codebuddyExecutor struct{}

func (e *codebuddyExecutor) CLIName() string { return "codebuddy" }
func (e *codebuddyExecutor) InstallHint() string {
	return "See your internal CodeBuddy installation docs."
}
func (e *codebuddyExecutor) Plain() bool              { return false }
func (e *codebuddyExecutor) SupportsPermission() bool { return false }

func (e *codebuddyExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	args := []string{"codebuddy", "-p", message, "--output-format", "stream-json", "--dangerously-skip-permissions"}
	if disallow := envKnob(env, "CODEBUDDY_DISALLOW_TOOLS", "AskUserQuestion"); disallow != "" {
		args = append(args, "--disallowedTools", disallow)
	}
	if model := envKnob(env, "CODEBUDDY_MODEL", ""); model != "" {
		args = append(args, "--model", model)
	}
	if sessionID != "" {
		args = append(args, "-r", sessionID)
	}
	return args, nil
}

func (e *codebuddyExecutor) ParseEvent(event map[string]any) *ParseResult {
	etype, _ := event["type"].(string)
	switch etype {
	case "assistant":
		if text := messageContentBlocks(event); text != "" {
			return &ParseResult{PartialText: strPtr(text)}
		}
	case "result":
		sid, _ := event["session_id"].(string)
		if isErr, _ := event["is_error"].(bool); isErr {
			msg, _ := event["result"].(string)
			if msg == "" {
				msg = "codebuddy reported an error"
			}
			return &ParseResult{SessionID: sid, Error: msg}
		}
		out := &ParseResult{SessionID: sid}
		if s, ok := event["result"].(string); ok && s != "" {
			out.FinalText = strPtr(s)
		}
		return out
	}
	return nil
}

// ---------------------------------------------------------------------------
// codex
// ---------------------------------------------------------------------------

type codexExecutor struct{}

func (e *codexExecutor) CLIName() string          { return "codex" }
func (e *codexExecutor) InstallHint() string      { return "Install: npm install -g @openai/codex" }
func (e *codexExecutor) Plain() bool              { return false }
func (e *codexExecutor) SupportsPermission() bool { return false }

func (e *codexExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	model := envKnob(env, "CODEX_MODEL", "")
	// "--" keeps a message that starts with "-" a positional, not a flag;
	// every flag must therefore precede it.
	if sessionID != "" {
		args := []string{"codex", "exec", "resume", "--json", sessionID}
		if model != "" {
			args = append(args, "-c", `model="`+model+`"`)
		}
		return append(args, "--", message), nil
	}
	args := []string{"codex", "exec", "--json"}
	if model != "" {
		args = append(args, "-c", `model="`+model+`"`)
	}
	return append(args, "--", message), nil
}

func (e *codexExecutor) ParseEvent(event map[string]any) *ParseResult {
	etype, _ := event["type"].(string)
	switch etype {
	case "thread.started":
		if tid, ok := event["thread_id"].(string); ok {
			return &ParseResult{SessionID: tid}
		}
	case "item.completed":
		item, _ := event["item"].(map[string]any)
		if t, _ := item["type"].(string); t == "agent_message" {
			if text, _ := item["text"].(string); text != "" {
				return &ParseResult{PartialText: strPtr(text)}
			}
		}
	case "turn.failed":
		msg := "codex turn failed"
		if errObj, ok := event["error"].(map[string]any); ok {
			if m, ok := errObj["message"].(string); ok && m != "" {
				msg = m
			}
		} else if m, ok := event["error"].(string); ok && m != "" {
			msg = m
		}
		return &ParseResult{Error: msg}
	}
	return nil
}

// ---------------------------------------------------------------------------
// cursor — emits a duplicate full-text assistant event at the end of a
// streamed turn; parse state per turn suppresses it.
// ---------------------------------------------------------------------------

type cursorExecutor struct{}

func (e *cursorExecutor) CLIName() string { return "agent" }
func (e *cursorExecutor) InstallHint() string {
	return "Install: brew install cursor-agent  (then run `agent login`)"
}
func (e *cursorExecutor) Plain() bool              { return false }
func (e *cursorExecutor) SupportsPermission() bool { return false }

func (e *cursorExecutor) MakeTurn() *TurnHandlers {
	var accumulated []string
	return &TurnHandlers{
		BuildArgs: func(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
			args := []string{"agent", "-p", message, "--output-format", "stream-json", "--stream-partial-output"}
			if envKnob(env, "CURSOR_FORCE", "1") == "1" {
				args = append(args, "--yolo")
			}
			if model := envKnob(env, "CURSOR_MODEL", ""); model != "" {
				args = append(args, "--model", model)
			}
			if sessionID != "" {
				args = append(args, "--resume", sessionID)
			}
			return args, nil
		},
		ParseEvent: func(event map[string]any) *ParseResult {
			etype, _ := event["type"].(string)
			switch etype {
			case "system":
				if event["subtype"] == "init" {
					if sid, ok := event["session_id"].(string); ok {
						return &ParseResult{SessionID: sid}
					}
				}
			case "assistant":
				text := messageContentBlocks(event)
				if text == "" {
					return nil
				}
				if text == strings.Join(accumulated, "") {
					return nil // duplicate full-text tail event
				}
				accumulated = append(accumulated, text)
				return &ParseResult{PartialText: strPtr(text)}
			case "result":
				sid, _ := event["session_id"].(string)
				isErr, _ := event["is_error"].(bool)
				sub, _ := event["subtype"].(string)
				if isErr || sub == "error" {
					msg, _ := event["result"].(string)
					if msg == "" {
						msg = "cursor agent reported an error"
					}
					return &ParseResult{SessionID: sid, Error: msg}
				}
				out := &ParseResult{SessionID: sid}
				if s, ok := event["result"].(string); ok && s != "" {
					out.FinalText = strPtr(s)
				}
				return out
			}
			return nil
		},
	}
}

// ---------------------------------------------------------------------------
// gemini-cli
// ---------------------------------------------------------------------------

type geminiCLIExecutor struct{}

func (e *geminiCLIExecutor) CLIName() string          { return "gemini" }
func (e *geminiCLIExecutor) InstallHint() string      { return "Install: npm install -g @google/gemini-cli" }
func (e *geminiCLIExecutor) Plain() bool              { return false }
func (e *geminiCLIExecutor) SupportsPermission() bool { return false }

func geminiStyleBuildArgs(cli, sandboxEnv, modelEnv, message, sessionID string, env map[string]string) []string {
	args := []string{cli, "-p", message, "--output-format", "stream-json", "--yolo"}
	if strings.ToLower(envKnob(env, sandboxEnv, "")) == "false" {
		args = append(args, "--sandbox", "false")
	}
	if model := envKnob(env, modelEnv, ""); model != "" {
		args = append(args, "--model", model)
	}
	if sessionID != "" {
		args = append(args, "--resume", sessionID)
	}
	return args
}

func geminiStyleParseEvent(event map[string]any, cliLabel string) *ParseResult {
	etype, _ := event["type"].(string)
	switch etype {
	case "init":
		if sid, ok := event["session_id"].(string); ok {
			return &ParseResult{SessionID: sid}
		}
	case "message":
		if role, _ := event["role"].(string); role != "assistant" {
			return nil
		}
		text, _ := event["content"].(string)
		if text == "" {
			return nil
		}
		if delta, _ := event["delta"].(bool); delta {
			return &ParseResult{PartialText: strPtr(text)}
		}
		return &ParseResult{FinalText: strPtr(text)}
	case "error":
		if severity, _ := event["severity"].(string); severity == "error" {
			msg, _ := event["message"].(string)
			if msg == "" {
				msg = cliLabel + " reported an error"
			}
			return &ParseResult{Error: msg}
		}
	case "result":
		if status, _ := event["status"].(string); status == "error" {
			msg := cliLabel + " turn failed"
			if errObj, ok := event["error"].(map[string]any); ok {
				if m, ok := errObj["message"].(string); ok && m != "" {
					msg = m
				}
			}
			return &ParseResult{Error: msg}
		}
	}
	return nil
}

func (e *geminiCLIExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	return geminiStyleBuildArgs("gemini", "GEMINI_SANDBOX", "GEMINI_MODEL", message, sessionID, env), nil
}

func (e *geminiCLIExecutor) ParseEvent(event map[string]any) *ParseResult {
	return geminiStyleParseEvent(event, "gemini")
}

// ---------------------------------------------------------------------------
// qwen-code — same wire shape as gemini-cli.
// ---------------------------------------------------------------------------

type qwenCodeExecutor struct{}

func (e *qwenCodeExecutor) CLIName() string { return "qwen" }
func (e *qwenCodeExecutor) InstallHint() string {
	return "Install: npm install -g @qwen-code/qwen-code"
}
func (e *qwenCodeExecutor) Plain() bool              { return false }
func (e *qwenCodeExecutor) SupportsPermission() bool { return false }

func (e *qwenCodeExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	return geminiStyleBuildArgs("qwen", "QWEN_SANDBOX", "QWEN_MODEL", message, sessionID, env), nil
}

func (e *qwenCodeExecutor) ParseEvent(event map[string]any) *ParseResult {
	return geminiStyleParseEvent(event, "qwen")
}

// ---------------------------------------------------------------------------
// kimi-code — stateful: the session id is minted in build_args and stamped
// on the assistant events it parses.
// ---------------------------------------------------------------------------

type kimiCodeExecutor struct{}

func (e *kimiCodeExecutor) CLIName() string { return "kimi" }
func (e *kimiCodeExecutor) InstallHint() string {
	return "See https://moonshotai.github.io/kimi-cli for installation"
}
func (e *kimiCodeExecutor) Plain() bool              { return false }
func (e *kimiCodeExecutor) SupportsPermission() bool { return false }

func (e *kimiCodeExecutor) MakeTurn() *TurnHandlers {
	sessionID := ""
	return &TurnHandlers{
		BuildArgs: func(message, sid string, env map[string]string, _ TurnCtx) ([]string, error) {
			if sid != "" {
				sessionID = sid
			} else {
				sessionID = newUUIDv4()
			}
			args := []string{"kimi", "--print", "-p", message, "--output-format=stream-json", "--session", sessionID}
			if model := envKnob(env, "KIMI_MODEL", ""); model != "" {
				args = append(args, "--model", model)
			}
			return args, nil
		},
		ParseEvent: func(event map[string]any) *ParseResult {
			if role, _ := event["role"].(string); role == "assistant" {
				if content, _ := event["content"].(string); content != "" {
					return &ParseResult{PartialText: strPtr(content), FinalText: strPtr(content), SessionID: sessionID}
				}
			}
			return nil
		},
	}
}

// ---------------------------------------------------------------------------
// opencode
// ---------------------------------------------------------------------------

type opencodeExecutor struct{}

func (e *opencodeExecutor) CLIName() string { return "opencode" }
func (e *opencodeExecutor) InstallHint() string {
	return "Install: npm install -g opencode-ai  (or: curl -fsSL https://opencode.ai/install | bash)"
}
func (e *opencodeExecutor) Plain() bool              { return false }
func (e *opencodeExecutor) SupportsPermission() bool { return false }

func (e *opencodeExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	// Flags precede "--": everything after it is positional, so a message
	// that starts with "-" cannot be parsed as a flag.
	args := []string{"opencode", "run", "--auto", "--format", "json"}
	if sessionID != "" {
		args = append(args, "--session", sessionID)
	}
	if model := envKnob(env, "OPENCODE_MODEL", ""); model != "" {
		args = append(args, "--model", model)
	}
	return append(args, "--", message), nil
}

func (e *opencodeExecutor) ParseEvent(event map[string]any) *ParseResult {
	etype, _ := event["type"].(string)
	sid, _ := event["sessionID"].(string)
	part, _ := event["part"].(map[string]any)
	switch etype {
	case "text":
		text, _ := part["text"].(string)
		if text != "" {
			return &ParseResult{SessionID: sid, PartialText: strPtr(text)}
		}
		if sid != "" {
			return &ParseResult{SessionID: sid}
		}
	case "step_start", "step_finish", "tool_use":
		if sid != "" {
			return &ParseResult{SessionID: sid}
		}
	case "error":
		msg := "opencode reported an error"
		if part != nil {
			if m, ok := part["message"].(string); ok && m != "" {
				msg = m
			}
		}
		if errObj, ok := event["error"].(map[string]any); ok {
			if m, ok := errObj["message"].(string); ok && m != "" {
				msg = m
			}
		}
		return &ParseResult{SessionID: sid, Error: msg}
	}
	return nil
}

// ---------------------------------------------------------------------------
// Plain-text bridges (no NDJSON; the entire stdout is the reply body)
// ---------------------------------------------------------------------------

// BuildArgs / ParseEvent are never called on the stateful executors
// (cursor / grok-build / kimi-code / agy — MakeTurn supplies the per-turn
// handlers), but the Executor interface requires the methods.

func (e *cursorExecutor) BuildArgs(string, string, map[string]string, TurnCtx) ([]string, error) {
	return nil, nil
}
func (e *cursorExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

func (e *grokBuildExecutor) BuildArgs(string, string, map[string]string, TurnCtx) ([]string, error) {
	return nil, nil
}
func (e *grokBuildExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

func (e *kimiCodeExecutor) BuildArgs(string, string, map[string]string, TurnCtx) ([]string, error) {
	return nil, nil
}
func (e *kimiCodeExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

func (e *agyExecutor) BuildArgs(string, string, map[string]string, TurnCtx) ([]string, error) {
	return nil, nil
}
func (e *agyExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

// agy — supports --conversation <id> for resuming; the session id is minted
// or reused in build_args and surfaced via GetSessionID after exit.
type agyExecutor struct{}

func (e *agyExecutor) CLIName() string { return "agy" }
func (e *agyExecutor) InstallHint() string {
	return "See the agy project for installation instructions."
}
func (e *agyExecutor) Plain() bool              { return true }
func (e *agyExecutor) SupportsPermission() bool { return false }

func (e *agyExecutor) MakeTurn() *TurnHandlers {
	sessionID := ""
	return &TurnHandlers{
		BuildArgs: func(message, sid string, env map[string]string, _ TurnCtx) ([]string, error) {
			if sid != "" {
				sessionID = sid
			} else {
				sessionID = newUUIDv4()
			}
			// Message last, after "--", so a message starting with "-" stays
			// positional.
			args := []string{"agy", "--print", "--conversation", sessionID}
			if envKnob(env, "AGY_DANGEROUSLY_SKIP_PERMISSIONS", "1") == "1" {
				args = append(args, "--dangerously-skip-permissions")
			}
			if model := envKnob(env, "AGY_MODEL", ""); model != "" {
				args = append(args, "--model", model)
			}
			return append(args, "--", message), nil
		},
		GetSessionID: func() string { return sessionID },
	}
}

type aiderExecutor struct{}

func (e *aiderExecutor) CLIName() string          { return "aider" }
func (e *aiderExecutor) InstallHint() string      { return "Install: pip install aider-chat" }
func (e *aiderExecutor) Plain() bool              { return true }
func (e *aiderExecutor) SupportsPermission() bool { return false }

func (e *aiderExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	args := []string{"aider", "--message", message, "--yes-always", "--no-show-release-notes", "--no-stream"}
	if model := envKnob(env, "AIDER_MODEL", ""); model != "" {
		args = append(args, "--model", model)
	}
	return args, nil
}

func (e *aiderExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

type deepseekExecutor struct{}

func (e *deepseekExecutor) CLIName() string { return "deepseek" }
func (e *deepseekExecutor) InstallHint() string {
	return "Install from https://deepseek.com/downloads or: brew install deepseek"
}
func (e *deepseekExecutor) Plain() bool              { return true }
func (e *deepseekExecutor) SupportsPermission() bool { return false }

func (e *deepseekExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	args := []string{"deepseek", "exec", "-p", message}
	if model := envKnob(env, "DEEPSEEK_MODEL", ""); model != "" {
		args = append(args, "--model", model)
	}
	return args, nil
}

func (e *deepseekExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

type dshExecutor struct{}

func (e *dshExecutor) CLIName() string          { return "dsh" }
func (e *dshExecutor) InstallHint() string      { return "Install: npm install -g @deepseek-ai/dsh" }
func (e *dshExecutor) Plain() bool              { return true }
func (e *dshExecutor) SupportsPermission() bool { return false }

func (e *dshExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	// "--" so a task starting with "-" stays a positional (hub/dsh does the same).
	return []string{"dsh", "--profile", "headless", "--", message}, nil
}

func (e *dshExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

type piExecutor struct{}

func (e *piExecutor) CLIName() string { return "pi" }
func (e *piExecutor) InstallHint() string {
	return "Install: npm install -g @earendil-works/pi-coding-agent"
}
func (e *piExecutor) Plain() bool              { return true }
func (e *piExecutor) SupportsPermission() bool { return false }

func (e *piExecutor) BuildArgs(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
	args := []string{"pi", "-p", message, "--approve"}
	if envKnob(env, "PI_NO_EXTENSIONS", "1") != "0" {
		args = append(args, "--no-extensions")
	}
	if model := envKnob(env, "PI_MODEL", ""); model != "" {
		args = append(args, "--model", model)
	}
	return args, nil
}

func (e *piExecutor) ParseEvent(map[string]any) *ParseResult { return nil }

// ---------------------------------------------------------------------------
// grok-build — chunk-flushing parse state: text accumulates until a soft/hard
// character boundary, and "end" flushes the leftover with the full body.
// ---------------------------------------------------------------------------

const (
	grokSoftChars = 40
	grokHardChars = 80
)

func grokShouldFlush(buf string) bool {
	if buf == "" {
		return false
	}
	if utf8.RuneCountInString(buf) >= grokHardChars {
		return true
	}
	if len(buf) > 0 {
		last, _ := utf8.DecodeLastRuneInString(buf)
		if strings.ContainsRune(grokBoundary, last) && utf8.RuneCountInString(buf) >= grokSoftChars {
			return true
		}
		return last == '\n'
	}
	return false
}

const grokBoundary = "\n。！？；.!?;"

type grokBuildExecutor struct{}

func (e *grokBuildExecutor) CLIName() string { return "grok" }
func (e *grokBuildExecutor) InstallHint() string {
	return "Install: curl -fsSL https://x.ai/cli/install.sh | bash"
}
func (e *grokBuildExecutor) Plain() bool              { return false }
func (e *grokBuildExecutor) SupportsPermission() bool { return false }

func (e *grokBuildExecutor) MakeTurn() *TurnHandlers {
	var full []string
	pending := ""
	flushPending := func() (string, bool) {
		if pending == "" {
			return "", false
		}
		chunk := pending
		pending = ""
		return chunk, true
	}
	return &TurnHandlers{
		BuildArgs: func(message, sessionID string, env map[string]string, _ TurnCtx) ([]string, error) {
			args := []string{"grok", "-p", message, "--output-format", "streaming-json", "--always-approve", "--no-auto-update"}
			if model := envKnob(env, "GROK_MODEL", ""); model != "" {
				args = append(args, "-m", model)
			}
			if sessionID != "" {
				args = append(args, "-r", sessionID)
			}
			return args, nil
		},
		ParseEvent: func(event map[string]any) *ParseResult {
			etype, _ := event["type"].(string)
			switch etype {
			case "text":
				data, _ := event["data"].(string)
				if data == "" {
					return nil
				}
				full = append(full, data)
				pending += data
				if grokShouldFlush(pending) {
					if chunk, ok := flushPending(); ok {
						return &ParseResult{PartialText: strPtr(chunk)}
					}
				}
				return nil
			case "thought":
				return nil
			case "end":
				sid, _ := event["sessionId"].(string)
				out := &ParseResult{FinalText: strPtr(strings.Join(full, ""))}
				if chunk, ok := flushPending(); ok {
					out.PartialText = strPtr(chunk)
				}
				if sid != "" {
					out.SessionID = sid
				}
				return out
			case "error":
				sid, _ := event["sessionId"].(string)
				pending = ""
				msg, _ := event["message"].(string)
				if msg == "" {
					msg = "grok reported an error"
				}
				return &ParseResult{SessionID: sid, Error: msg}
			}
			return nil
		},
	}
}
