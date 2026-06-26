#!/usr/bin/env node
'use strict';
/**
 * agentproc CLI — drive any AgentProc profile against a message.
 *
 * Quick start (hub):
 *   agentproc hub list
 *   agentproc hub run echo-agent -p "hello"
 *   cd ~/projects/my-app && agentproc hub run claude-code -p "explain this"
 *
 * Advanced (local profile YAML, no hub fetch):
 *   agentproc --profile <path.yaml> --prompt "hello" [options]
 *
 * Options (local-profile mode):
 *   --profile, -p <path>      Profile YAML path (required)
 *   --prompt <text>           User message (required, unless --stdin)
 *   --session <id>            Previous session id for multi-turn
 *   --session-name <name>     Human-readable session name
 *   --from <user>             Sender identifier
 *   --cwd <path>              Override profile.cwd
 *   --env KEY=VALUE           Extra env var (repeatable)
 *   --timeout <secs>          Override profile.timeout_secs
 *   --no-stream               Disable streaming (set AGENT_STREAMING=0)
 *   --verbose                 Forward protocol lines to stderr (default)
 *   --quiet                   Suppress protocol lines on stderr
 *   --raw                     Don't parse stdout; forward agent output verbatim
 *   --stdin                   Read prompt from stdin instead of --prompt
 *   --version                 Print version and exit
 *   --help, -h                Show help
 *
 * Output (default mode):
 *   stderr  → protocol lines (AGENT_PARTIAL:, AGENT_SESSION:, AGENT_ERROR:) in real time
 *   stdout  → final reply body (printed after agent exits)
 *   exit    → 0 success, 1 error, 124 timeout
 *
 * Output (--raw mode):
 *   stdout  → agent's stdout, verbatim, no parsing
 *   exit    → agent's exit code
 *
 * The last AGENT_SESSION: id is also printed on stderr at the very end,
 * prefixed with "agentproc:session:" so shell scripts can capture it:
 *   session=$(agentproc ... 2>&1 | grep '^agentproc:session:' | cut -d: -f3)
 */

const fs = require('node:fs');
const path = require('node:path');

const runner = require('./runner.js');
const hub = require('./hub.js');
const { PROTOCOL_VERSION } = runner;

const PKG_VERSION = require('../package.json').version;

// ---------------------------------------------------------------------------
// Arg parsing — minimal hand-rolled parser, no deps
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const opts = {
    profile: null,
    prompt: null,
    session: '',
    sessionName: 'default',
    from: '',
    cwd: null,
    env: [],  // array of "KEY=VALUE" strings; --env can repeat
    timeout: null,
    stream: true,
    verbose: true,
    raw: false,
    stdin: false,
    help: false,
    version: false,
  };
  const extras = [];

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) throw new Error(`option ${a} requires a value`);
      return argv[++i];
    };
    switch (a) {
      case '-h': case '--help': opts.help = true; break;
      case '--version': opts.version = true; break;
      case '--profile': case '-p': opts.profile = next(); break;
      case '--prompt': opts.prompt = next(); break;
      case '--session': opts.session = next(); break;
      case '--session-name': opts.sessionName = next(); break;
      case '--from': opts.from = next(); break;
      case '--cwd': opts.cwd = next(); break;
      case '--env':
        opts.env.push(next());
        break;
      case '--timeout': opts.timeout = parseInt(next(), 10); break;
      case '--no-stream': opts.noStream = true; break;
      case '--verbose': opts.verbose = true; break;
      case '--quiet': opts.verbose = false; break;
      case '--raw': opts.raw = true; break;
      case '--stdin': opts.stdin = true; break;
      default:
        if (a === 'hub' && extras.length === 0) {
          opts.hub = true;
          opts.hubArgs = argv.slice(i + 1);
          return { opts, extras };
        }
        if (a.startsWith('--')) throw new Error(`unknown option: ${a}`);
        extras.push(a);
    }
  }
  return { opts, extras };
}

// ---------------------------------------------------------------------------
// Hub subcommand dispatcher
// ---------------------------------------------------------------------------

async function runHubSubcommand(args) {
  const sub = args[0];
  const rest = args.slice(1);

  if (!sub || sub === '--help' || sub === '-h') {
    showHubHelp();
    return 0;
  }

  // Any subcommand with --help/-h shows the hub help (covers `hub run --help`,
  // `hub install --help`, etc. — useful for muscle-memory discovery).
  if (rest.includes('--help') || rest.includes('-h')) {
    showHubHelp();
    return 0;
  }

  // Parse hub args. We can't reuse parseArgs() here because it throws on
  // any unknown `--flag`, and hub supports `--refresh` which the runner
  // parser doesn't know about. Instead, walk the args ourselves, separating
  // hub-level flags (--refresh, --help) from runner-level flags (-p/--prompt,
  // --cwd, --env, ...) and positional args (the profile name).
  const refresh = rest.includes('--refresh');
  const positional = [];
  const runnerArgs = [];
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    if (a === '--refresh' || a === '-h' || a === '--help') continue;
    // Runner flags that take a value (next arg): match both long and short.
    // In the `hub run` context, `-p` means `--prompt` (the profile name is
    // positional, not a path), so we normalize it before handing off.
    const takesValue =
      a === '--prompt' || a === '-p' ||
      a === '--session' || a === '--session-name' || a === '--from' ||
      a === '--cwd' || a === '--env' || a === '--timeout';
    if (takesValue) {
      runnerArgs.push(a === '-p' ? '--prompt' : a);
      if (i + 1 < rest.length) runnerArgs.push(rest[++i]);
      continue;
    }
    // Other known runner flags (boolean).
    if (a === '--no-stream' || a === '--verbose' || a === '--quiet' ||
        a === '--raw' || a === '--stdin') {
      runnerArgs.push(a);
      continue;
    }
    // Anything else starting with - or -- is unknown to us — surface it
    // as an explicit error instead of silently treating it as a positional.
    if (a.startsWith('-')) {
      process.stderr.write(`error: unknown option: ${a}\n\n`);
      showHubHelp();
      return 2;
    }
    positional.push(a);
  }

  if (sub === 'list') {
    const profiles = await hub.listProfiles({
      onLog: m => process.stderr.write(m + '\n'),
    });
    process.stdout.write('Available profiles in the official hub:\n\n');
    for (const p of profiles) {
      process.stdout.write(
        `  ${p.name.padEnd(15)} ${p.tested.padEnd(12)} ${p.description.slice(0, 60)}\n`
      );
    }
    process.stdout.write(`\nRun \`agentproc hub run <name> -p "hi"\` to use one.\n`);
    return 0;
  }

  if (sub === 'show') {
    if (!positional[0]) {
      process.stderr.write('error: hub show requires a profile name\n');
      return 2;
    }
    const readme = await hub.showReadme(positional[0], {
      refresh,
      onLog: m => process.stderr.write(m + '\n'),
    });
    process.stdout.write(readme);
    if (!readme.endsWith('\n')) process.stdout.write('\n');
    return 0;
  }

  if (sub === 'install') {
    if (!positional[0]) {
      process.stderr.write('error: hub install requires a profile name\n');
      return 2;
    }
    const target = process.cwd();
    const dest = await hub.installProfile(positional[0], target, {
      refresh,
      onLog: m => process.stderr.write(m + '\n'),
    });
    // After-the-fact hint: tell the user exactly what they got and how to
    // run it. Without this, "installed to: ./echo-agent/" leaves them
    // guessing what to type next.
    process.stderr.write(`\n`);
    process.stderr.write(`Next: edit ${path.relative(target, path.join(dest, 'profile.yaml'))} if you want, then run:\n`);
    process.stderr.write(`  agentproc --profile ${path.relative(target, path.join(dest, 'profile.yaml'))} --prompt "hi" --cwd <your-project>\n`);
    return 0;
  }

  if (sub === 'run') {
    if (!positional[0]) {
      process.stderr.write('error: hub run requires a profile name\n');
      return 2;
    }
    const profileName = positional[0];
    const cacheDir = await hub.fetchProfile(profileName, {
      refresh,
      onLog: m => process.stderr.write(m + '\n'),
    });
    const profilePath = path.join(cacheDir, 'profile.yaml');

    // Parse the runner-level flags we separated out above.
    const { opts: runOpts } = parseArgs(runnerArgs);
    if (!runOpts.prompt && !runOpts.stdin) {
      process.stderr.write('error: hub run requires --prompt <text> or --stdin\n');
      return 2;
    }

    // hub run uses the user's current directory as the agent's cwd when
    // --cwd is not given. This matches the hub docs ("uses your current
    // directory as cwd") and is the right default for AI-CLI profiles
    // where the agent should operate on the user's project.
    if (!runOpts.cwd) {
      runOpts.cwd = process.cwd();
    }

    return await runAgent(profilePath, runOpts);
  }

  process.stderr.write(`error: unknown hub subcommand: ${sub}\n\n`);
  showHubHelp();
  return 2;
}

function showHubHelp() {
  process.stdout.write(`agentproc hub — manage profiles from the official Hub

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
  --timeout <secs>             Override profile.timeout_secs
  --no-stream                  Disable streaming
  --verbose / --quiet          Protocol line visibility (default: verbose)
  --stdin                      Read prompt from stdin

Common options:
  --refresh                    Force re-fetch from GitHub (ignore cache)
  -h, --help                   Show this help

Examples:
  agentproc hub list
  agentproc hub run echo-agent -p "hello"
  cd ~/projects/my-app && agentproc hub run claude-code -p "explain this" --env ANTHROPIC_API_KEY=$KEY
  agentproc hub show codex
  agentproc hub install agy

Profiles are cached at ~/.agentproc/cache/hub/<name>/ (24h TTL).
`);
}

/**
 * Shared runner logic used by both `agentproc --profile` and `agentproc hub run`.
 * Kept here for the hub subcommand to reuse; the legacy main() path also calls it.
 */
async function runAgent(profilePath, opts) {
  let profileRaw;
  const profileDir = path.dirname(path.resolve(profilePath));
  try {
    const yamlText = fs.readFileSync(path.resolve(profilePath), 'utf8');
    profileRaw = parseYaml(yamlText);
  } catch (e) {
    process.stderr.write(`error: failed to read profile ${profilePath}: ${e.message}\n`);
    return 2;
  }

  // Read prompt.
  let prompt = opts.prompt;
  if (opts.stdin) {
    prompt = fs.readFileSync(0, 'utf8').replace(/\n$/, '');
  }
  if (prompt == null) {
    process.stderr.write('error: --prompt (or --stdin) is required\n');
    return 2;
  }

  // opts.env is an array of "KEY=VALUE" strings (from repeated --env flags)
  const extraEnv = {};
  for (const kv of opts.env || []) {
    const eq = kv.indexOf('=');
    if (eq < 0) {
      process.stderr.write(`error: --env expects KEY=VALUE, got: ${kv}\n`);
      return 2;
    }
    extraEnv[kv.slice(0, eq)] = kv.slice(eq + 1);
  }

  const streaming = opts.noStream ? false : null;

  if (opts.raw) {
    const r = await runner.run(profileRaw, {
      message: prompt,
      sessionId: opts.session || '',
      sessionName: opts.sessionName || 'default',
      fromUser: opts.from || '',
      streaming,
      cwd: opts.cwd,
      profileDir,
      extraEnv,
      timeoutSecs: opts.timeout,
    });
    process.stdout.write(r.reply);
    if (r.reply && !r.reply.endsWith('\n')) process.stdout.write('\n');
    return r.exitCode === 0 ? 0 : 1;
  }

  // verbose: default true, --verbose keeps it true, --quiet sets it false.
  const verbose = opts.verbose !== false;

  const r = await runner.run(profileRaw, {
    message: prompt,
    sessionId: opts.session || '',
    sessionName: opts.sessionName || 'default',
    fromUser: opts.from || '',
    streaming,
    cwd: opts.cwd,
    profileDir,
    extraEnv,
    timeoutSecs: opts.timeout,
    onPartial: (t) => { if (verbose) process.stderr.write(`AGENT_PARTIAL:${JSON.stringify(t)}\n`); },
    onSession: (id) => { if (verbose) process.stderr.write(`AGENT_SESSION:${id}\n`); },
    onError: (msg) => { if (verbose) process.stderr.write(`AGENT_ERROR:${JSON.stringify(msg)}\n`); },
    onStderr: (line) => { if (verbose) process.stderr.write(`[agent stderr] ${line}\n`); },
  });

  if (r.reply) {
    process.stdout.write(r.reply);
    if (!r.reply.endsWith('\n')) process.stdout.write('\n');
  }
  if (r.sessionId) process.stderr.write(`agentproc:session:${r.sessionId}\n`);
  if (r.error) process.stderr.write(`agentproc:error:${r.error}\n`);
  return r.exitCode === 0 ? 0 : 1;
}

function showHelp() {
  process.stdout.write(`agentproc v${PKG_VERSION} (protocol ${PROTOCOL_VERSION})

The fastest way in:
  agentproc hub list                          # see what's available
  agentproc hub run echo-agent -p "hello"     # smoke test (no API key)
  cd ~/projects/my-app && agentproc hub run claude-code -p "explain this"

The CLI fetches the profile from the GitHub hub on first use, caches it at
~/.agentproc/cache/hub/<name>/ (24h TTL), and uses your current directory as
the agent's cwd. Set GITHUB_TOKEN to raise the rate limit (see \`agentproc hub --help\`).

Hub subcommands:
  hub list                          List all profiles in the hub
  hub show <name>                   Show a profile's README
  hub run <name> [run-options]      Fetch (if needed) and run a profile
  hub install <name>                Copy a profile to the current directory

Run \`agentproc hub --help\` for the full hub reference.

───────────────────────────────────────────────────────────────────────────────

Advanced: run a local profile YAML directly (no hub fetch)

Usage:
  agentproc --profile <path.yaml> --prompt "hello" [options]

Required:
  --profile, -p <path>      Profile YAML path
  --prompt <text>           User message (or use --stdin)

Session:
  --session <id>            Previous session id (multi-turn)
  --session-name <name>     Human-readable session name (default: "default")
  --from <user>             Sender identifier

Execution:
  --cwd <path>              Override profile.cwd (relative paths resolve
                            against the profile.yaml's directory)
  --env KEY=VALUE           Extra env var (repeatable)
  --timeout <secs>          Override profile.timeout_secs
  --no-stream               Set AGENT_STREAMING=0

Output:
  --verbose                 Forward protocol lines to stderr (default)
  --quiet                   Suppress protocol lines on stderr
  --raw                     Don't parse stdout; forward agent output verbatim
  --stdin                   Read prompt from stdin instead of --prompt

Other:
  --version                 Print version and exit
  --help, -h                Show this help

Output semantics:
  stderr  → protocol lines (AGENT_PARTIAL:, AGENT_SESSION:, AGENT_ERROR:)
  stdout  → final reply body (non-protocol lines)
  exit    → 0 success · 1 error · 124 timeout (per spec)

The final session id is printed on stderr as: agentproc:session:<id>

Examples:
  # Local profile (relative cwd resolves next to profile.yaml):
  agentproc --profile ./hub/echo-agent/profile.yaml --prompt "hi"

  # Local claude-code profile, claude runs against your project:
  agentproc --profile ./hub/claude-code/profile.yaml \\
            --prompt "explain this codebase" \\
            --cwd /path/to/your/project

  # Prompt from stdin:
  cat prompt.txt | agentproc --profile prof.yaml --stdin
`);
}

function showVersion() {
  process.stdout.write(`agentproc ${PKG_VERSION} (protocol ${PROTOCOL_VERSION})\n`);
}

// ---------------------------------------------------------------------------
// YAML parsing — minimal hand-rolled, supports the subset hub profiles use
// ---------------------------------------------------------------------------

/**
 * Parse a YAML profile file into a JS object.
 *
 * We deliberately avoid a YAML dependency to keep the SDK zero-dep.
 * The subset we parse: nested maps, scalar values, block scalars (|), arrays
 * of scalars (under `args:` and `tags:`). This covers every hub/ profile
 * and every spec/protocol.md example.
 *
 * For anything more complex, users are encouraged to pre-parse their YAML
 * and pass the object directly to the runner.
 */
function parseYamlSimple(text) {
  // First try JSON (also valid YAML for simple cases).
  try { return JSON.parse(text); } catch {}

  const lines = text.split(/\r?\n/);
  const root = {};
  const stack = [{ indent: -1, obj: root, key: null }];

  function currentContainer(minIndent) {
    while (stack.length > 1 && stack[stack.length - 1].indent >= minIndent) {
      stack.pop();
    }
    return stack[stack.length - 1];
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    if (raw.trim() === '' || raw.trim().startsWith('#')) continue;
    const indent = raw.match(/^[ \t]*/)[0].replace(/\t/g, '  ').length;
    const content = raw.trim();

    // key: value
    const m = content.match(/^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$/);
    if (!m) {
      // Skip unparseable lines (e.g., complex flow scalars); don't crash.
      continue;
    }
    const [, key, val] = m;
    const container = currentContainer(indent);

    if (val === '') {
      // Could be a nested map, a block scalar, or a sequence. Look ahead.
      const nextRaw = lines[i + 1] || '';
      const nextIndent = nextRaw.match(/^[ \t]*/)[0].replace(/\t/g, '  ').length;
      if (nextIndent > indent) {
        if (nextRaw.trim().startsWith('- ')) {
          // Sequence
          const arr = [];
          container.obj[key] = arr;
          stack.push({ indent, obj: { __seq: arr }, key });
        } else {
          // Nested map
          const child = {};
          container.obj[key] = child;
          stack.push({ indent, obj: child, key });
        }
      } else {
        // Empty value, no children
        container.obj[key] = '';
      }
    } else if (val === '|' || val === '|-') {
      // Block scalar — consume subsequent more-indented lines.
      const blockLines = [];
      let j = i + 1;
      for (; j < lines.length; j++) {
        const nr = lines[j];
        if (nr.trim() === '' && j === lines.length - 1) break;
        const ni = nr.match(/^[ \t]*/)[0].replace(/\t/g, '  ').length;
        if (ni <= indent && nr.trim() !== '') break;
        blockLines.push(nr.slice(Math.min(indent + 2, nr.length)));
      }
      container.obj[key] = blockLines.join('\n').replace(/\n+$/, val === '|' ? '\n' : '');
      i = j - 1;
    } else if (val.startsWith('- ')) {
      // Inline sequence element on same line — rare but handle it.
      if (!Array.isArray(container.obj[key])) container.obj[key] = [];
      container.obj[key].push(stripScalar(val.slice(2)));
    } else {
      container.obj[key] = stripScalar(val);
    }
  }

  // Post-process: walk and lift any __seq arrays.
  function liftSeqs(o) {
    if (Array.isArray(o)) {
      o.forEach(liftSeqs);
    } else if (o && typeof o === 'object') {
      for (const k of Object.keys(o)) {
        const v = o[k];
        if (v && typeof v === 'object' && '__seq' in v) {
          o[k] = v.__seq;
          liftSeqs(o[k]);
        } else {
          liftSeqs(v);
        }
      }
    }
  }
  liftSeqs(root);
  return root;
}

function stripScalar(v) {
  // Quoted string — return inner content verbatim.
  if ((v.startsWith('"') && v.endsWith('"')) ||
      (v.startsWith("'") && v.endsWith("'"))) {
    return v.slice(1, -1);
  }
  // Flow sequence: [a, b, c]
  if (v.startsWith('[') && v.endsWith(']')) {
    const inner = v.slice(1, -1).trim();
    if (inner === '') return [];
    return inner.split(',').map(s => stripScalar(s.trim()));
  }
  // Booleans / null
  const lv = v.toLowerCase();
  if (lv === 'true') return true;
  if (lv === 'false') return false;
  if (lv === 'null' || lv === '~') return null;
  // Numbers (int / float, optional sign)
  if (/^[+-]?\d+$/.test(v)) return parseInt(v, 10);
  if (/^[+-]?\d+\.\d+$/.test(v)) return parseFloat(v);
  return v;
}

// ---------------------------------------------------------------------------
// Sequence continuation: collect "- ..." entries under a key whose value
// became { __seq: [...] }. Done above. But standalone "- ..." lines (when
// the container's current key already has an array) need handling.
// ---------------------------------------------------------------------------

// Re-do the parsing with proper sequence handling.
function parseYaml(text) {
  try { return JSON.parse(text); } catch {}

  // Use a line-based state machine that handles sequences better.
  const lines = text.split(/\r?\n/);
  const root = {};
  /** @type {Array<{indent:number, obj:Object|Array, parentKey:string|null, parent:Object|null}>} */
  const stack = [{ indent: -1, obj: root, parentKey: null, parent: null }];

  function top() { return stack[stack.length - 1]; }
  function popUntil(minIndent) {
    while (stack.length > 1 && top().indent >= minIndent) stack.pop();
    return top();
  }

  function getIndent(s) {
    return s.match(/^[ \t]*/)[0].replace(/\t/g, '  ').length;
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    if (raw.trim() === '' || raw.trim().startsWith('#')) continue;
    const indent = getIndent(raw);
    const content = raw.slice(indent).replace(/\r$/, '');
    const cont = popUntil(indent);

    // Sequence item: "- value" or "-"
    if (content.startsWith('- ') || content === '-') {
      // Find the parent object that has a key awaiting a sequence value.
      // Strategy: if top().obj is an array, push to it; else we need to
      // convert — but our key: line lookahead already created the array.
      if (Array.isArray(cont.obj)) {
        const rest = content === '-' ? '' : content.slice(2);
        if (rest.trim() === '') {
          // Map under sequence — rare in our profiles, skip gracefully.
          continue;
        }
        cont.obj.push(stripScalar(rest));
      }
      continue;
    }

    // key: value
    const m = content.match(/^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$/);
    if (!m) continue;
    const [, key, val] = m;

    if (val === '') {
      // Look ahead: is the next non-empty, more-indented line a sequence?
      let j = i + 1;
      while (j < lines.length && (lines[j].trim() === '' || lines[j].trim().startsWith('#'))) j++;
      const nextRaw = lines[j] || '';
      const nextIndent = getIndent(nextRaw);
      const nextContent = nextRaw.slice(nextIndent);
      if (nextIndent > indent && (nextContent.startsWith('- ') || nextContent === '-')) {
        const arr = [];
        cont.obj[key] = arr;
        stack.push({ indent, obj: arr, parentKey: key, parent: cont.obj });
      } else if (nextIndent > indent) {
        const child = {};
        cont.obj[key] = child;
        stack.push({ indent, obj: child, parentKey: key, parent: cont.obj });
      } else {
        cont.obj[key] = '';
      }
    } else if (val === '|' || val === '|-' || val === '>') {
      const blockLines = [];
      let j = i + 1;
      for (; j < lines.length; j++) {
        const nr = lines[j];
        const ni = getIndent(nr);
        if (nr.trim() === '') { blockLines.push(''); continue; }
        if (ni <= indent) break;
        blockLines.push(nr.slice(Math.min(indent + 2, nr.length)));
      }
      const joined = blockLines.join('\n');
      container_set(cont.obj, key, val === '|'
        ? joined.replace(/\n*$/, '\n')
        : joined.replace(/\n*$/, ''));
      i = j - 1;
    } else {
      container_set(cont.obj, key, stripScalar(val));
    }
  }

  return root;
}

function container_set(obj, key, value) {
  obj[key] = value;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const { opts } = parseArgs(process.argv.slice(2));

  if (opts.help) { showHelp(); process.exit(0); }
  if (opts.version) { showVersion(); process.exit(0); }

  // `agentproc hub <subcommand>` — defer to hub dispatcher.
  if (opts.hub) {
    return await runHubSubcommand(opts.hubArgs);
  }

  if (!opts.profile) {
    process.stderr.write('error: --profile is required\n\n');
    showHelp();
    process.exit(2);
  }

  // Read prompt from --stdin if requested.
  let prompt = opts.prompt;
  if (opts.stdin) {
    prompt = fs.readFileSync(0, 'utf8').replace(/\n$/, '');
  }
  if (prompt == null) {
    process.stderr.write('error: --prompt (or --stdin) is required\n');
    process.exit(2);
  }

  // Read & parse profile YAML, then delegate to the shared runner path.
  try {
    return await runAgent(opts.profile, opts);
  } catch (e) {
    process.stderr.write(`error: ${e.message}\n`);
    return 1;
  }
}

// Run main() only when invoked directly as a script, not when required for tests.
if (require.main === module) {
  main().then(
    (code) => {
      // main() returns an explicit exit code from its various return paths;
      // honor it so shell scripts can distinguish success from failure.
      process.exit(typeof code === 'number' ? code : 0);
    },
    (e) => {
      // Friendly handling for known hub errors: print the message + remediation
      // hint, never a raw Node stack trace.
      if (e && e.name === 'HubError') {
        process.stderr.write(`error: ${e.message}\n`);
        if (e.hint) process.stderr.write(`\n${e.hint}\n`);
        process.exit(1);
        return;
      }
      // For fetch() network errors wrapped by hub.js (also HubError, but be
      // defensive in case some path throws a plain TypeError from fetch).
      if (e && typeof e.message === 'string' && /fetch failed|ENOTFOUND|ECONNREFUSED|ECONNRESET/.test(e.message)) {
        process.stderr.write(`error: network error talking to GitHub: ${e.message}\n`);
        process.stderr.write(`\nThis is usually transient. Re-run the command, or run against a local checkout:\n`);
        process.stderr.write(`  agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"\n`);
        process.exit(1);
        return;
      }
      // Everything else: still avoid dumping the stack. Show the message only.
      process.stderr.write(`error: ${e && (e.message || e)}\n`);
      process.exit(1);
    }
  );
}

module.exports = { parseArgs, parseYaml, showHelp, main };
