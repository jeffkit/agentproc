"""agentproc CLI — run any AgentProc profile against a message.

Usage:
    agentproc --profile <path.yaml> --prompt "hello" [options]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .runner import (
    PROTOCOL_VERSION,
    EXIT_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    RunOptions,
    run,
)


def _read_pkg_version() -> str:
    """Read installed package version.

    Primary: importlib.metadata (works after pip/pipx install).
    Fallback: parse pyproject.toml (works in source checkout, dev mode).
    """
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("agentproc")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    # Fallback for source checkout (development, not installed).
    try:
        toml_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if toml_path.exists():
            text = toml_path.read_text(encoding="utf-8")
            m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "0.0.0+unknown"


PKG_VERSION = _read_pkg_version()


# ---------------------------------------------------------------------------
# Minimal YAML parser (zero-dep; covers hub profile subset)
# ---------------------------------------------------------------------------

def parse_yaml(text: str) -> Dict[str, Any]:
    """Parse a YAML subset into a Python dict.

    Supports: nested maps, block scalars (|), inline flow sequences ([a, b]),
    block sequences (- item), quoted strings, booleans, null, ints, floats.
    """
    text_stripped = text.strip()
    if text_stripped.startswith("{") or text_stripped.startswith("["):
        try:
            return json.loads(text)
        except Exception:
            pass

    lines = text.splitlines()
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Any]] = [(-1, root)]

    def current(min_indent: int) -> Tuple[int, Any]:
        while len(stack) > 1 and stack[-1][0] >= min_indent:
            stack.pop()
        return stack[-1]

    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.strip() == "" or raw.strip().startswith("#"):
            i += 1
            continue
        indent = _leading_spaces(raw)
        content = raw[indent:].rstrip("\r")
        _, container = current(indent)

        if content == "-" or content.startswith("- "):
            if isinstance(container, list):
                rest = "" if content == "-" else content[2:]
                if rest.strip():
                    container.append(_strip_scalar(rest))
            i += 1
            continue

        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", content)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2)

        if val == "":
            j = i + 1
            while j < len(lines) and (lines[j].strip() == "" or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines):
                next_indent = _leading_spaces(lines[j])
                next_content = lines[j][next_indent:]
                if next_indent > indent and (next_content == "-" or next_content.startswith("- ")):
                    arr: List[Any] = []
                    if isinstance(container, dict):
                        container[key] = arr
                    stack.append((indent, arr))
                    i += 1
                    continue
                elif next_indent > indent:
                    child: Dict[str, Any] = {}
                    if isinstance(container, dict):
                        container[key] = child
                    stack.append((indent, child))
                    i += 1
                    continue
            if isinstance(container, dict):
                container[key] = ""
            i += 1
            continue

        if val in ("|", "|-", ">"):
            block_lines: List[str] = []
            j = i + 1
            while j < len(lines):
                nr = lines[j]
                if nr.strip() == "":
                    block_lines.append("")
                    j += 1
                    continue
                ni = _leading_spaces(nr)
                if ni <= indent:
                    break
                block_lines.append(nr[min(indent + 2, len(nr)):])
                j += 1
            joined = "\n".join(block_lines)
            if val == "|":
                value = re.sub(r"\n*$", "\n", joined)
            else:
                value = re.sub(r"\n*$", "", joined)
            if isinstance(container, dict):
                container[key] = value
            i = j
            continue

        if isinstance(container, dict):
            container[key] = _strip_scalar(val)
        i += 1

    return root


def _leading_spaces(s: str) -> int:
    n = 0
    for ch in s:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 2
        else:
            break
    return n


def _strip_scalar(v: str) -> Any:
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_strip_scalar(s.strip()) for s in inner.split(",")]
    lv = v.lower()
    if lv == "true":
        return True
    if lv == "false":
        return False
    if lv in ("null", "~", ""):
        return None
    if re.match(r"^[+-]?\d+$", v):
        return int(v)
    if re.match(r"^[+-]?\d+\.\d+$", v):
        return float(v)
    return v


# ---------------------------------------------------------------------------
# Argparse setup
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentproc",
        description="Run an AgentProc profile against a message.",
        add_help=False,
    )
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    p.add_argument("-p", "--profile")
    p.add_argument("--prompt")
    p.add_argument("--session", default="")
    p.add_argument("--session-name", default="default")
    p.add_argument("--from", dest="from_user", default="")
    p.add_argument("--cwd")
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--timeout", type=int)
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--raw", action="store_true")
    p.add_argument("--stdin", action="store_true")
    return p


def show_help() -> None:
    sys.stdout.write(f"""agentproc v{PKG_VERSION} (protocol {PROTOCOL_VERSION})

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
  --cwd <path>              Override profile.cwd
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
  agentproc --profile hub/echo-agent/profile.yaml --prompt "hi"
  agentproc -p hub/claude-code/profile.yaml --prompt "hello" --verbose
  cat prompt.txt | agentproc -p prof.yaml --stdin
""")


def show_version() -> None:
    sys.stdout.write(f"agentproc {PKG_VERSION} (protocol {PROTOCOL_VERSION})\n")


# ---------------------------------------------------------------------------
# Hub subcommand dispatcher
# ---------------------------------------------------------------------------

def _run_hub_subcommand(args: List[str]) -> int:
    """Handle `agentproc hub <list|show|install|run> [args]`."""
    from . import hub as hub_mod

    if not args or args[0] in ("-h", "--help"):
        _show_hub_help()
        return 0

    sub = args[0]
    rest = args[1:]

    # Common flag
    refresh = "--refresh" in rest
    positional = [a for a in rest if not a.startswith("--")]

    def _log(msg: str) -> None:
        sys.stderr.write(msg + "\n")

    if sub == "list":
        profiles = hub_mod.list_profiles(on_log=_log)
        sys.stdout.write("Available profiles in the official hub:\n\n")
        for p in profiles:
            sys.stdout.write(
                f"  {p['name']:<15} {p['tested']:<12} {p['description'][:60]}\n"
            )
        sys.stdout.write('\nRun `agentproc hub run <name> -p "hi"` to use one.\n')
        return 0

    if sub == "show":
        if not positional:
            sys.stderr.write("error: hub show requires a profile name\n")
            return 2
        readme = hub_mod.show_readme(positional[0], refresh=refresh, on_log=_log)
        sys.stdout.write(readme)
        if not readme.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    if sub == "install":
        if not positional:
            sys.stderr.write("error: hub install requires a profile name\n")
            return 2
        hub_mod.install_profile(positional[0], Path.cwd(), refresh=refresh, on_log=_log)
        return 0

    if sub == "run":
        if not positional:
            sys.stderr.write("error: hub run requires a profile name\n")
            return 2
        profile_name = positional[0]
        cache_dir = hub_mod.fetch_profile(profile_name, refresh=refresh, on_log=_log)
        profile_path = str(cache_dir / "profile.yaml")

        # Re-parse remaining args (excluding the profile name) as runner options.
        parser = build_parser()
        # Drop the first positional (profile name) and --refresh from rest.
        runner_args = [a for i, a in enumerate(rest) if not (
            (not a.startswith("--") and i == 0) or a == "--refresh"
        )]
        opts = parser.parse_args(runner_args)

        if not opts.prompt and not opts.stdin:
            sys.stderr.write("error: hub run requires --prompt <text> or --stdin\n")
            return 2

        return _run_agent_with_profile(profile_path, opts)

    sys.stderr.write(f"error: unknown hub subcommand: {sub}\n\n")
    _show_hub_help()
    return 2


def _show_hub_help() -> None:
    sys.stdout.write("""agentproc hub — manage profiles from the official Hub

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
""")


def _run_agent_with_profile(profile_path: str, opts) -> int:
    """Shared runner logic for both --profile path and hub run path."""
    try:
        yaml_text = Path(profile_path).resolve().read_text(encoding="utf-8")
        profile_raw = parse_yaml(yaml_text)
    except FileNotFoundError:
        sys.stderr.write(f"error: profile not found: {profile_path}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"error: failed to parse profile {profile_path}: {e}\n")
        return 2

    # Read prompt.
    prompt = opts.prompt
    if opts.stdin:
        try:
            prompt = sys.stdin.read().rstrip("\n")
        except KeyboardInterrupt:
            return 1
    if prompt is None:
        sys.stderr.write("error: --prompt (or --stdin) is required\n")
        return 2

    extra_env: Dict[str, str] = {}
    for kv in opts.env or []:
        eq = kv.find("=")
        if eq < 0:
            sys.stderr.write(f"error: --env expects KEY=VALUE, got: {kv}\n")
            return 2
        extra_env[kv[:eq]] = kv[eq + 1:]

    streaming = False if opts.no_stream else None

    if opts.raw:
        r = run(
            profile_raw,
            RunOptions(
                message=prompt,
                session_id=opts.session,
                session_name=opts.session_name,
                from_user=opts.from_user,
                streaming=streaming,
                cwd=opts.cwd,
                extra_env=extra_env,
                timeout_secs=opts.timeout,
            ),
        )
        sys.stdout.write(r.reply)
        if r.reply and not r.reply.endswith("\n"):
            sys.stdout.write("\n")
        return 0 if r.exit_code == 0 else 1

    verbose = opts.verbose or not opts.quiet

    r = run(
        profile_raw,
        RunOptions(
            message=prompt,
            session_id=opts.session,
            session_name=opts.session_name,
            from_user=opts.from_user,
            streaming=streaming,
            cwd=opts.cwd,
            extra_env=extra_env,
            timeout_secs=opts.timeout,
            on_partial=lambda t: verbose and sys.stderr.write(
                f"AGENT_PARTIAL:{json.dumps(t, ensure_ascii=False)}\n"
            ),
            on_session=lambda sid: verbose and sys.stderr.write(f"AGENT_SESSION:{sid}\n"),
            on_error=lambda msg: verbose and sys.stderr.write(
                f"AGENT_ERROR:{json.dumps(msg, ensure_ascii=False)}\n"
            ),
            on_stderr=lambda line: verbose and sys.stderr.write(f"[agent stderr] {line}\n"),
        ),
    )

    if r.reply:
        sys.stdout.write(r.reply)
        if not r.reply.endswith("\n"):
            sys.stdout.write("\n")
    if r.session_id:
        sys.stderr.write(f"agentproc:session:{r.session_id}\n")
    if r.error:
        sys.stderr.write(f"agentproc:error:{r.error}\n")
    return 0 if r.exit_code == 0 else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # `agentproc hub <subcommand>` — defer to hub dispatcher.
    if argv and argv[0] == "hub":
        return _run_hub_subcommand(argv[1:])

    parser = build_parser()
    opts = parser.parse_args(argv)

    if opts.help:
        show_help()
        return 0
    if opts.version:
        show_version()
        return 0

    if not opts.profile:
        sys.stderr.write("error: --profile is required\n\n")
        show_help()
        return 2

    return _run_agent_with_profile(opts.profile, opts)


if __name__ == "__main__":
    sys.exit(main())
