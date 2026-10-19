// Command agentproc — run any AgentProc profile against a message (Go CLI).
//
// Port of the Python SDK's cli.py, at parity on flags, output semantics and
// exit codes:
//
//	agentproc --profile <path.yaml> --prompt "hello" [options]
//	agentproc hub <list|show|install|run> [args]
//
// Output semantics: protocol events go to stderr (NDJSON), the final reply
// body goes to stdout, exit 0 success / 1 error / 2 usage / 130 interrupted.
// The final session id is printed on stderr as: agentproc:session:<id>
//
// Hub divergence from Python/Node: no bundled profile copy (a Go module
// cannot embed files outside its subtree) — `hub` commands resolve via the
// jsDelivr CDN and cache for 24h, so first use needs network.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"syscall"

	"gopkg.in/yaml.v3"

	agentproc "github.com/jeffkit/agentproc/sdk/go"
)

// versionOverride is set at release-build time via -ldflags
// "-X main.versionOverride=<tag>"; when empty (go install / go build from
// source) the CLI falls back to the embedded build info or the SDK constant.
var versionOverride string

// realTagRe matches a plain semver tag only — local source builds embed
// pseudo-versions (v0.16.1-0.<ts>-<sha> [+dirty]) that would misreport.
var realTagRe = regexp.MustCompile(`^v\d+\.\d+\.\d+$`)

// cliVersion reports the CLI version: the release build's injected tag, else
// the module version embedded by `go install pkg@version` (when it is a real
// tag, not a local pseudo-version), else the SDK constant.
func cliVersion() string {
	if versionOverride != "" {
		return versionOverride
	}
	if info, ok := debug.ReadBuildInfo(); ok && realTagRe.MatchString(info.Main.Version) {
		return info.Main.Version
	}
	return agentproc.SDKVersion
}

type envFlags []string

func (e *envFlags) String() string     { return strings.Join(*e, ",") }
func (e *envFlags) Set(v string) error { *e = append(*e, v); return nil }

// timeoutFlag distinguishes "--timeout 0" (explicitly no limit, matching the
// Python/Node CLIs) from "--timeout absent" (use the profile value).
type timeoutFlag struct {
	set bool
	val int
}

func (t *timeoutFlag) String() string { return fmt.Sprintf("%d", t.val) }
func (t *timeoutFlag) Set(v string) error {
	n, err := strconv.Atoi(v)
	if err != nil {
		return err
	}
	t.set, t.val = true, n
	return nil
}

// stdinReader is the shared buffered reader for interactive permission
// answers — a fresh reader per prompt would discard buffered input and hang
// the second approval of a multi-request turn.
var (
	stdinReader   = bufio.NewReader(os.Stdin)
	stdinReaderMu sync.Mutex
)

func readPermissionAnswer() (string, error) {
	stdinReaderMu.Lock()
	defer stdinReaderMu.Unlock()
	line, err := stdinReader.ReadString('\n')
	if err != nil && line == "" {
		return "", err
	}
	return line, nil
}

type cliOptions struct {
	profile     string
	prompt      string
	session     string
	sessionName string
	imageURL    string
	fileURL     string
	cwd         string
	env         envFlags
	timeout     timeoutFlag
	journal     string
	noStream    bool
	verbose     bool
	quiet       bool
	raw         bool
	stdinMode   bool
	help        bool
	version     bool
}

func newFlagSet(fs *flag.FlagSet, opts *cliOptions) {
	fs.StringVar(&opts.profile, "profile", "", "profile YAML path")
	fs.StringVar(&opts.profile, "p", "", "profile YAML path (shorthand)")
	fs.StringVar(&opts.prompt, "prompt", "", "user message")
	fs.StringVar(&opts.session, "session", "", "previous session id (multi-turn)")
	fs.StringVar(&opts.sessionName, "session-name", "default", "human-readable session name")
	fs.StringVar(&opts.imageURL, "image-url", "", "image attachment URL")
	fs.StringVar(&opts.fileURL, "file-url", "", "file attachment URL")
	fs.StringVar(&opts.cwd, "cwd", "", "override profile.cwd")
	fs.Var(&opts.env, "env", "extra env var KEY=VALUE (repeatable)")
	fs.Var(&opts.timeout, "timeout", "override profile.timeout_secs (0 = no limit)")
	fs.StringVar(&opts.journal, "journal", "", "append an NDJSON event journal to a file")
	fs.BoolVar(&opts.noStream, "no-stream", false, "disable streaming (ignore partial events)")
	fs.BoolVar(&opts.verbose, "verbose", false, "forward protocol lines to stderr (default)")
	fs.BoolVar(&opts.quiet, "quiet", false, "suppress protocol lines on stderr")
	fs.BoolVar(&opts.raw, "raw", false, "only print the final reply body")
	fs.BoolVar(&opts.stdinMode, "stdin", false, "read the prompt from stdin")
	fs.BoolVar(&opts.help, "help", false, "show help")
	fs.BoolVar(&opts.help, "h", false, "show help (shorthand)")
	fs.BoolVar(&opts.version, "version", false, "print version and exit")
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(argv []string) int {
	if len(argv) > 0 && argv[0] == "hub" {
		return runHubSubcommand(argv[1:])
	}

	opts := &cliOptions{}
	fs := flag.NewFlagSet("agentproc", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	newFlagSet(fs, opts)
	if err := fs.Parse(argv); err != nil && err != flag.ErrHelp {
		fmt.Fprintln(os.Stderr, "error:", err)
		fmt.Fprintln(os.Stderr)
		showHelp()
		return 2
	}

	if opts.version {
		fmt.Printf("agentproc %s (protocol %s)\n", cliVersion(), agentproc.PROTOCOL_VERSION)
		return 0
	}
	if opts.help {
		showHelp()
		return 0
	}
	if opts.profile == "" {
		fmt.Fprintln(os.Stderr, "error: --profile is required")
		fmt.Fprintln(os.Stderr)
		showHelp()
		return 2
	}
	return runAgentWithProfile(opts.profile, opts)
}

func runAgentWithProfile(profilePath string, opts *cliOptions) int {
	profileDir, err := filepath.Abs(filepath.Dir(profilePath))
	if err != nil {
		profileDir = filepath.Dir(profilePath)
	}
	rawYAML, readErr := os.ReadFile(profilePath)
	if readErr != nil {
		fmt.Fprintf(os.Stderr, "error: profile not found: %s\n", profilePath)
		return 2
	}
	var profileRaw map[string]any
	if err := yaml.Unmarshal(rawYAML, &profileRaw); err != nil {
		fmt.Fprintf(os.Stderr, "error: failed to parse profile %s: %v\n", profilePath, err)
		return 2
	}

	prompt := opts.prompt
	if opts.stdinMode {
		data, _ := io.ReadAll(os.Stdin)
		prompt = strings.TrimRight(string(data), "\n")
	}
	if prompt == "" && !opts.stdinMode {
		fmt.Fprintln(os.Stderr, "error: --prompt (or --stdin) is required")
		return 2
	}

	extraEnv := map[string]string{}
	for _, kv := range opts.env {
		eq := strings.IndexByte(kv, '=')
		if eq < 0 {
			fmt.Fprintf(os.Stderr, "error: --env expects KEY=VALUE, got: %s\n", kv)
			return 2
		}
		extraEnv[kv[:eq]] = kv[eq+1:]
	}

	var streaming *bool
	if opts.noStream {
		v := false
		streaming = &v
	}
	var timeoutOverride *int
	if opts.timeout.set {
		v := opts.timeout.val
		timeoutOverride = &v
	}

	var attachments []any
	if opts.imageURL != "" {
		attachments = append(attachments, map[string]any{"kind": "image", "url": opts.imageURL})
	}
	if opts.fileURL != "" {
		attachments = append(attachments, map[string]any{"kind": "file", "url": opts.fileURL})
	}

	// Ctrl-C / SIGTERM: cancel the run context — the runner performs the
	// graded group kill and returns 125; the CLI reports 130 (SIGINT) or
	// 143 (SIGTERM) per the spec exit-code table.
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	sigSeen := make(chan os.Signal, 1)
	signal.Notify(sigSeen, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(sigSeen)
	go func() {
		select {
		case s := <-sigSeen:
			sigSeen <- s // relay what we caught for the exit-code mapping
			stop()
		case <-ctx.Done():
		}
	}()

	runOpts := &agentproc.RunOptions{
		Message:     prompt,
		SessionID:   opts.session,
		SessionName: opts.sessionName,
		Streaming:   streaming,
		Cwd:         opts.cwd,
		ProfileDir:  profileDir,
		ExtraEnv:    extraEnv,
		Attachments: attachments,
		TimeoutSecs: timeoutOverride,
	}

	if !opts.raw {
		verbose := opts.verbose || !opts.quiet
		emit := func(obj map[string]any) {
			if verbose {
				line, _ := json.Marshal(obj)
				fmt.Fprintln(os.Stderr, string(line))
			}
		}
		runOpts.OnPartial = func(text string, role *string) { emit(map[string]any{"type": "partial", "text": text}) }
		runOpts.OnSession = func(sid string) { emit(map[string]any{"type": "session", "id": sid}) }
		runOpts.OnError = func(msg string) { emit(map[string]any{"type": "error", "message": msg}) }
		if verbose {
			runOpts.OnStderr = func(line string) { fmt.Fprintf(os.Stderr, "[agent stderr] %s\n", line) }
		}
		if permissionOn(profileRaw) {
			runOpts.OnPermission = interactivePermissionHandler
		}
	}
	if opts.journal != "" {
		jf, err := os.OpenFile(opts.journal, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: cannot open journal file %s: %v\n", opts.journal, err)
			return 2
		}
		defer jf.Close()
		enc := json.NewEncoder(jf)
		enc.SetEscapeHTML(false)
		runOpts.OnJournal = func(entry agentproc.JournalEntry) {
			_ = enc.Encode(entry)
		}
	}

	result, err := agentproc.Run(ctx, profileRaw, runOpts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 1
	}

	if opts.raw || result.Reply != "" {
		body := result.Reply
		fmt.Print(body)
		if body != "" && !strings.HasSuffix(body, "\n") {
			fmt.Println()
		}
	}
	if !opts.raw {
		if result.SessionID != "" {
			fmt.Fprintf(os.Stderr, "agentproc:session:%s\n", result.SessionID)
		}
		if result.Error != "" {
			fmt.Fprintf(os.Stderr, "agentproc:error:%s\n", result.Error)
		}
	}
	if ctx.Err() != nil {
		// Interrupted; the agent group has already been killed. The spec
		// reserves 130 for SIGINT and 143 for SIGTERM.
		select {
		case s := <-sigSeen:
			if s == syscall.SIGTERM {
				return 143
			}
		default:
		}
		return 130
	}
	if result.TimedOut {
		// The runner keeps Error empty on a spawn-path timeout (conformance);
		// the CLI still owes the operator a reason for exit 1.
		fmt.Fprintf(os.Stderr, "agentproc:error:turn timed out\n")
	}
	if result.ExitCode == 0 {
		return 0
	}
	return 1
}

// interactivePermissionHandler prompts y/N on a TTY; without a TTY (headless
// CI / pipes) it denies — mirrors the Python and Node CLIs.
func interactivePermissionHandler(req map[string]any) *agentproc.PermissionDecision {
	summary, _ := req["description"].(string)
	if summary == "" {
		input, _ := req["input"].(map[string]any)
		brief, _ := json.Marshal(input)
		summary = fmt.Sprintf("%s %s", req["tool_name"], firstN(string(brief), 120))
	}
	fmt.Fprintf(os.Stderr, "\n[agentproc] permission request %v: %v\n  %s\n", req["request_id"], req["tool_name"], summary)
	if !isTerminal(os.Stdin) || !isTerminal(os.Stderr) {
		fmt.Fprintln(os.Stderr, "[agentproc] no TTY — denying permission request")
		return &agentproc.PermissionDecision{Behavior: "deny", Message: "no TTY for interactive approval"}
	}
	line, err := readPermissionAnswer()
	if err != nil {
		return &agentproc.PermissionDecision{Behavior: "deny", Message: "stdin unavailable — denied"}
	}
	answer := strings.ToLower(strings.TrimSpace(line))
	if answer == "y" || answer == "yes" {
		input, _ := req["input"].(map[string]any)
		if input == nil {
			input = map[string]any{}
		}
		return &agentproc.PermissionDecision{Behavior: "allow", UpdatedInput: input}
	}
	return &agentproc.PermissionDecision{Behavior: "deny", Message: "denied by user"}
}

func firstN(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

func permissionOn(profileRaw map[string]any) bool {
	block := profileRaw
	if wrapper, ok := profileRaw["agentproc"].(map[string]any); ok {
		block = wrapper
	}
	v, _ := block["permission"].(bool)
	return v
}

func isTerminal(f *os.File) bool {
	info, err := f.Stat()
	if err != nil {
		return false
	}
	return info.Mode()&os.ModeCharDevice != 0
}

// --- hub subcommands --------------------------------------------------------

func runHubSubcommand(args []string) int {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" {
		showHubHelp()
		return 0
	}
	sub, rest := args[0], args[1:]
	for _, a := range rest {
		if a == "-h" || a == "--help" {
			showHubHelp()
			return 0
		}
	}
	refresh := false
	var positional, runnerArgs []string
	takesValue := map[string]bool{
		"--prompt": true, "-p": true, "--session": true, "--session-name": true,
		"--image-url": true, "--file-url": true, "--cwd": true, "--env": true,
		"--timeout": true, "--journal": true,
	}
	booleanFlags := map[string]bool{"--no-stream": true, "--verbose": true, "--quiet": true, "--raw": true, "--stdin": true}
	for i := 0; i < len(rest); i++ {
		a := rest[i]
		if a == "--refresh" {
			refresh = true
			continue
		}
		if takesValue[a] {
			name := a
			if a == "-p" {
				name = "--prompt" // in hub run context, -p means the prompt
			}
			runnerArgs = append(runnerArgs, name)
			if i+1 < len(rest) {
				runnerArgs = append(runnerArgs, rest[i+1])
				i++
			}
			continue
		}
		if booleanFlags[a] {
			runnerArgs = append(runnerArgs, a)
			continue
		}
		if strings.HasPrefix(a, "-") {
			fmt.Fprintf(os.Stderr, "error: unknown option: %s\n\n", a)
			showHubHelp()
			return 2
		}
		positional = append(positional, a)
	}

	log := func(msg string) { fmt.Fprintln(os.Stderr, msg) }

	switch sub {
	case "list":
		profiles, herr := agentproc.ListProfiles(refresh, log)
		if herr != nil {
			return hubErrorExit(herr)
		}
		fmt.Println("Available profiles in the official hub:")
		fmt.Println()
		for _, p := range profiles {
			fmt.Printf("  %-15s %-12s %s\n", p.Name, p.Tested, firstN(p.Description, 60))
		}
		fmt.Println()
		fmt.Println(`Run ` + "`agentproc hub run <name> -p \"hi\"`" + ` to use one.`)
		return 0

	case "show":
		if len(positional) == 0 {
			fmt.Fprintln(os.Stderr, "error: hub show requires a profile name")
			return 2
		}
		readme, herr := agentproc.ShowReadme(positional[0], refresh, log)
		if herr != nil {
			return hubErrorExit(herr)
		}
		fmt.Println(readme)
		return 0

	case "install":
		if len(positional) == 0 {
			fmt.Fprintln(os.Stderr, "error: hub install requires a profile name")
			return 2
		}
		cwd, _ := os.Getwd()
		dest, herr := agentproc.InstallProfile(positional[0], cwd, refresh, log)
		if herr != nil {
			return hubErrorExit(herr)
		}
		rel, err := filepath.Rel(cwd, filepath.Join(dest, "profile.yaml"))
		if err != nil {
			rel = filepath.Join(dest, "profile.yaml")
		}
		fmt.Fprintln(os.Stderr)
		fmt.Fprintf(os.Stderr, "Next: edit %s if you want, then run:\n", rel)
		fmt.Fprintf(os.Stderr, "  agentproc --profile %s --prompt \"hi\" --cwd <your-project>\n", rel)
		return 0

	case "run":
		if len(positional) == 0 {
			fmt.Fprintln(os.Stderr, "error: hub run requires a profile name")
			return 2
		}
		cacheDir, herr := agentproc.FetchProfile(positional[0], refresh, log)
		if herr != nil {
			return hubErrorExit(herr)
		}
		opts := &cliOptions{}
		fs := flag.NewFlagSet("agentproc hub run", flag.ContinueOnError)
		fs.SetOutput(io.Discard)
		newFlagSet(fs, opts)
		if err := fs.Parse(runnerArgs); err != nil && err != flag.ErrHelp {
			fmt.Fprintln(os.Stderr, "error:", err)
			return 2
		}
		if opts.prompt == "" && !opts.stdinMode {
			fmt.Fprintln(os.Stderr, "error: hub run requires --prompt <text> or --stdin")
			return 2
		}
		// hub run uses the user's current directory as the agent's cwd when
		// --cwd is not given — the right default for AI-CLI profiles that
		// operate on the user's project.
		if opts.cwd == "" {
			cwd, _ := os.Getwd()
			opts.cwd = cwd
		}
		return runAgentWithProfile(filepath.Join(cacheDir, "profile.yaml"), opts)

	default:
		fmt.Fprintf(os.Stderr, "error: unknown hub subcommand: %s\n\n", sub)
		showHubHelp()
		return 2
	}
}

func hubErrorExit(herr *agentproc.HubError) int {
	fmt.Fprintf(os.Stderr, "error: %s\n", herr.Message)
	if herr.Hint != "" {
		fmt.Fprintf(os.Stderr, "\n%s\n", herr.Hint)
	}
	return 1
}

func showHelp() {
	fmt.Printf(`agentproc v%s (protocol %s)

The fastest way in:
  agentproc hub list                          # see what's available
  agentproc hub run echo-agent -p "hello"     # smoke test (no API key)
  cd ~/projects/my-app && agentproc hub run claude-code -p "explain this"

The CLI fetches the profile from the hub CDN on first use and caches it at
~/.agentproc/cache/hub/<name>/ (24h TTL). Set AGENTPROC_HUB_REF to pin an
exact upstream version.

Hub subcommands:
  hub list                          List all profiles in the hub
  hub show <name>                   Show a profile's README
  hub run <name> [run-options]      Fetch (if needed) and run a profile
  hub install <name>                Copy a profile to the current directory

Advanced: run a local profile YAML directly (no hub fetch)

Usage:
  agentproc --profile <path.yaml> --prompt "hello" [options]

Session:
  --session <id>            Previous session id (multi-turn)
  --session-name <name>     Human-readable session name (default: "default")

Attachments:
  --image-url <url>         Image attachment URL (carried in the turn's attachments)
  --file-url <url>          File attachment URL (carried in the turn's attachments)

Execution:
  --cwd <path>              Override profile.cwd (relative paths resolve
                            against the profile.yaml's directory)
  --env KEY=VALUE           Extra env var (repeatable)
  --timeout <secs>          Override profile.timeout_secs
  --no-stream               Disable streaming (ignore partial events)

Output:
  --verbose                 Forward protocol lines to stderr (default)
  --quiet                   Suppress protocol lines on stderr
  --raw                     Only print the final reply body (no live events)
  --stdin                   Read prompt from stdin instead of --prompt
  --journal <path>          Append an NDJSON event/decision journal to <path> (opt-in)

Other:
  --version                 Print version and exit
  --help, -h                Show this help

Output semantics:
  stderr  → protocol events (NDJSON) + agentproc:session:<id> / agentproc:error:<msg>
  stdout  → final reply body
  exit    → 0 success · 1 error · 2 usage · 130 interrupted
`, agentproc.SDKVersion, agentproc.PROTOCOL_VERSION)
}

func showHubHelp() {
	fmt.Print(`agentproc hub — manage profiles from the official Hub

Usage:
  agentproc hub list                       List all profiles in the hub
  agentproc hub show <name>                Show a profile's README
  agentproc hub install <name>             Copy a profile to the current directory
  agentproc hub run <name> [run-options]   Fetch (if needed) and run a profile

Hub run options (same as the regular --profile runner):
  -p, --prompt <text>          User message (or use --stdin)
  --cwd <path>                 Override profile.cwd (default: current dir)
  --env KEY=VALUE              Extra env var (repeatable)
  --session <id>               Previous session id for multi-turn
  --image-url <url>            Image attachment URL
  --file-url <url>             File attachment URL
  --timeout <secs>             Override profile.timeout_secs
  --journal <path>             Append an NDJSON event/decision journal to <path>
  --no-stream                  Disable streaming
  --verbose / --quiet          Protocol line visibility (default: verbose)
  --stdin                      Read prompt from stdin

Common options:
  --refresh                    Force re-fetch from the CDN (ignore cache)
  -h, --help                   Show this help

Profiles are cached at ~/.agentproc/cache/hub/<name>/ (24h TTL).
`)
}
