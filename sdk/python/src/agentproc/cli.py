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
from typing import Dict, List, Optional

from .runner import (
    PROTOCOL_VERSION,
    EXIT_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    RunOptions,
    run,
)
from .yaml import parse_yaml


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


def _build_attachments(opts) -> List[Dict[str, str]]:
    """Build the turn's `attachments` array from --image-url / --file-url.

    Wire 0.3 carries attachments as a single `attachments` list; there are no
    separate AGENT_IMAGE_URL / AGENT_FILE_URL channels. Returns an empty list
    when no attachment flags were given (the runner omits the key when empty).
    """
    a: List[Dict[str, str]] = []
    if getattr(opts, "image_url", ""):
        a.append({"kind": "image", "url": opts.image_url})
    if getattr(opts, "file_url", ""):
        a.append({"kind": "file", "url": opts.file_url})
    return a


def _make_permission_handler() -> Optional[object]:
    """Interactive tool authorization for profiles that opt in.

    On a TTY we prompt y/N; otherwise we deny (headless CI / pipes cannot
    answer mid-turn). Mirrors the Node CLI's onPermission.
    """
    def on_permission(req: Dict[str, object]):
        summary = req.get("description") or (
            f"{req.get('tool_name', '')} {json.dumps(req.get('input') or {})[:120]}"
        )
        sys.stderr.write(
            f"\n[agentproc] permission request {req.get('request_id')}: "
            f"{req.get('tool_name', '')}\n  {summary}\n"
        )
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            sys.stderr.write("[agentproc] no TTY — denying permission request\n")
            return {"behavior": "deny", "message": "no TTY for interactive approval"}
        try:
            answer = input("Allow? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return {"behavior": "deny", "message": "denied by user"}
        if re.match(r"^y(es)?$", (answer or "").strip(), re.IGNORECASE):
            return {"behavior": "allow", "updated_input": req.get("input") or {}}
        return {"behavior": "deny", "message": "denied by user"}
    return on_permission


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
    p.add_argument("--image-url", dest="image_url", default="")
    p.add_argument("--file-url", dest="file_url", default="")
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

The fastest way in:
  agentproc hub list                          # see what's available
  agentproc hub run echo-agent -p "hello"     # smoke test (no API key)
  cd ~/projects/my-app && agentproc hub run claude-code -p "explain this"

The CLI fetches the profile from the GitHub hub on first use, caches it at
~/.agentproc/cache/hub/<name>/ (24h TTL), and uses your current directory as
the agent's cwd. Set GITHUB_TOKEN to raise the rate limit (see `agentproc hub --help`).

Hub subcommands:
  hub list                          List all profiles in the hub
  hub show <name>                   Show a profile's README
  hub run <name> [run-options]      Fetch (if needed) and run a profile
  hub install <name>                Copy a profile to the current directory

Run `agentproc hub --help` for the full hub reference.

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

Attachments:
  --image-url <url>         Image attachment URL (carried in the turn's attachments)
  --file-url <url>          File attachment URL (carried in the turn's attachments)

Execution:
  --cwd <path>              Override profile.cwd (relative paths resolve
                            against the profile.yaml's directory)
  --env KEY=VALUE           Extra env var (repeatable)
  --timeout <secs>          Override profile.timeout_secs
  --no-stream               Disable streaming (ignore {"type":"partial"} events)

Output:
  --verbose                 Forward protocol lines to stderr (default)
  --quiet                   Suppress protocol lines on stderr
  --raw                     Quiet mode: only print the final reply body (no live events)
  --stdin                   Read prompt from stdin instead of --prompt

Other:
  --version                 Print version and exit
  --help, -h                Show this help

Output semantics:
  stderr  → NDJSON events ({"type":"partial"/"session"/"error"})
  stdout  → final reply body (assembled from {"type":"text"} events)
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

    # Any subcommand with --help/-h shows the hub help uniformly.
    if "--help" in rest or "-h" in rest:
        _show_hub_help()
        return 0

    # Common flag
    refresh = "--refresh" in rest

    # Separate hub-level flags from runner-level flags and positional args.
    # In the `hub run` context, `-p` means `--prompt` (the profile name is
    # positional, not a path), so normalize it before handing off to argparse.
    positional: List[str] = []
    runner_args: List[str] = []
    takes_value = {"--prompt", "-p", "--session", "--session-name", "--from",
                   "--image-url", "--file-url", "--cwd", "--env", "--timeout"}
    boolean_flags = {"--no-stream", "--verbose", "--quiet", "--raw", "--stdin"}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--refresh", "-h", "--help"):
            i += 1
            continue
        if a in takes_value:
            runner_args.append("--prompt" if a == "-p" else a)
            if i + 1 < len(rest):
                runner_args.append(rest[i + 1])
                i += 2
            else:
                i += 1
            continue
        if a in boolean_flags:
            runner_args.append(a)
            i += 1
            continue
        if a.startswith("-"):
            sys.stderr.write(f"error: unknown option: {a}\n\n")
            _show_hub_help()
            return 2
        positional.append(a)
        i += 1

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
        dest = hub_mod.install_profile(positional[0], Path.cwd(), refresh=refresh, on_log=_log)
        # Tell the user exactly what they got and how to run it next.
        rel_profile = os.path.relpath(str(dest / "profile.yaml"), str(Path.cwd()))
        sys.stderr.write("\n")
        sys.stderr.write(f"Next: edit {rel_profile} if you want, then run:\n")
        sys.stderr.write(
            f'  agentproc --profile {rel_profile} --prompt "hi" --cwd <your-project>\n'
        )
        return 0

    if sub == "run":
        if not positional:
            sys.stderr.write("error: hub run requires a profile name\n")
            return 2
        profile_name = positional[0]
        cache_dir = hub_mod.fetch_profile(profile_name, refresh=refresh, on_log=_log)
        profile_path = str(cache_dir / "profile.yaml")

        # Parse the runner-level flags we separated out above.
        parser = build_parser()
        opts = parser.parse_args(runner_args)

        if not opts.prompt and not opts.stdin:
            sys.stderr.write("error: hub run requires --prompt <text> or --stdin\n")
            return 2

        # hub run uses the user's current directory as the agent's cwd when
        # --cwd is not given. Matches the hub docs and the right default for
        # AI-CLI profiles where the agent should operate on the user's project.
        if not opts.cwd:
            opts.cwd = str(Path.cwd())

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
  --from <user>                Sender identifier
  --image-url <url>            Image attachment URL (carried in the turn's attachments)
  --file-url <url>             File attachment URL (carried in the turn's attachments)
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
    profile_dir = str(Path(profile_path).resolve().parent)
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
                profile_dir=profile_dir,
                extra_env=extra_env,
                attachments=_build_attachments(opts),
                timeout_secs=opts.timeout,
            ),
        )
        sys.stdout.write(r.reply)
        if r.reply and not r.reply.endswith("\n"):
            sys.stdout.write("\n")
        return 0 if r.exit_code == 0 else 1

    verbose = opts.verbose or not opts.quiet

    # Interactive tool authorization when the profile opts in.
    profile_block = profile_raw.get("agentproc") if isinstance(
        profile_raw.get("agentproc"), dict) else profile_raw
    permission_on = bool(profile_block and profile_block.get("permission") is True)

    def _emit(obj: Dict[str, object]) -> None:
        if verbose:
            sys.stderr.write(json.dumps(obj, ensure_ascii=False) + "\n")

    r = run(
        profile_raw,
        RunOptions(
            message=prompt,
            session_id=opts.session,
            session_name=opts.session_name,
            from_user=opts.from_user,
            streaming=streaming,
            cwd=opts.cwd,
            profile_dir=profile_dir,
            extra_env=extra_env,
            attachments=_build_attachments(opts),
            timeout_secs=opts.timeout,
            on_partial=lambda t: _emit({"type": "partial", "text": t}),
            on_session=lambda sid: _emit({"type": "session", "id": sid}),
            on_error=lambda msg: _emit({"type": "error", "message": msg}),
            on_stderr=lambda line: verbose and sys.stderr.write(f"[agent stderr] {line}\n"),
            on_permission=_make_permission_handler() if permission_on else None,
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

    try:
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
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # Friendly handling for known hub errors: print the message + hint,
        # never a raw stack trace.
        from .hub import HubError
        if isinstance(e, HubError):
            sys.stderr.write(f"error: {e}\n")
            if e.hint:
                sys.stderr.write(f"\n{e.hint}\n")
            return 1
        # Network errors that slipped through unwrapped.
        msg = str(e)
        if any(s in msg for s in ("fetch failed", "ENOTFOUND", "ECONNREFUSED", "ECONNRESET", "urlopen error")):
            sys.stderr.write(f"error: network error talking to GitHub: {msg}\n")
            sys.stderr.write("\nThis is usually transient. Re-run the command, or run against a local checkout:\n")
            sys.stderr.write('  agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"\n')
            return 1
        # Everything else: surface the message without dumping a traceback.
        sys.stderr.write(f"error: {msg}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
