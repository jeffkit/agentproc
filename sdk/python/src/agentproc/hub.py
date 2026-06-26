"""Hub client — fetch and manage profile directories from the official Hub.

The Hub lives at https://github.com/jeffkit/agentproc/tree/main/hub/
Profiles are cached locally at ~/.agentproc/cache/hub/<name>/ with a
24-hour TTL. Pass refresh=True to force re-fetch.

Public API:
    HUB_REPO            — the github repo id ("jeffkit/agentproc")
    HUB_REF             — the git ref to fetch from ("main")
    HUB_CACHE_TTL_SECS  — default 24 hours
    cache_dir(name)     — Path to the local cache directory for a profile
    fetch_profile(name, refresh=False, on_log=None) -> Path
    list_profiles(refresh=False, on_log=None) -> List[Dict[str,str]]
    show_readme(name, refresh=False, on_log=None) -> str
    install_profile(name, target_dir, refresh=False, on_log=None) -> Path

All network access is via urllib (stdlib). Zero dependencies.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HUB_REPO = "jeffkit/agentproc"
HUB_REF = "main"
HUB_CACHE_TTL_SECS = 24 * 60 * 60  # 24 hours

GITHUB_API = "https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
GITHUB_TREES = "https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
GITHUB_RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"


class HubError(RuntimeError):
    """Hub fetch failure with a human-readable remediation hint.

    The CLI prints `message` + `hint` to stderr instead of dumping a stack
    trace. `status` is the HTTP status (0 for network errors).

    Inherits from RuntimeError so existing `except RuntimeError` callers
    continue to work.
    """

    def __init__(self, message: str, *, hint: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.hint = hint
        self.status = status


def _auth_headers(*, json: bool = False) -> Dict[str, str]:
    """Build HTTP headers, adding Authorization if a token is in env.

    GITHUB_TOKEN (the env var GitHub Actions injects) or GH_TOKEN (what
    the `gh` CLI sets) raises GitHub's anonymous rate limit from ~60/hr
    to ~5000/hr.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    h: Dict[str, str] = {"User-Agent": "agentproc-cli"}
    if json:
        h["Accept"] = "application/vnd.github+json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _cache_root() -> Path:
    """Root cache directory: ~/.agentproc/cache/hub/"""
    return Path.home() / ".agentproc" / "cache" / "hub"


def cache_dir(name: str) -> Path:
    """Local cache path for a profile."""
    return _cache_root() / name


def _cache_age_secs(name: str) -> Optional[float]:
    """Seconds since the cache was last written. None if not cached."""
    p = cache_dir(name)
    marker = p / ".cache-meta.json"
    if not marker.exists():
        return None
    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
        ts = meta.get("fetched_at", 0)
        return max(0, time.time() - ts)
    except Exception:
        return None


def _write_cache_meta(name: str) -> None:
    """Write a small metadata file recording when we fetched."""
    p = cache_dir(name)
    p.mkdir(parents=True, exist_ok=True)
    (p / ".cache-meta.json").write_text(
        json.dumps({"fetched_at": time.time(), "ref": HUB_REF}),
        encoding="utf-8",
    )


def _http_get_json(url: str, timeout: int = 30) -> Any:
    """GET a URL, return parsed JSON. Raises HubError on failure."""
    req = urllib.request.Request(url, headers=_auth_headers(json=True))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise _http_error_to_hub(e.code, body[:200], url, is_json=True) from e
    except urllib.error.URLError as e:
        # Network/DNS/connection-level errors (no HTTP status).
        raise HubError(
            "could not reach GitHub while fetching hub profile",
            status=0,
            hint=(
                "This is usually a transient network issue. Try:\n"
                "  1. Re-run the command (often succeeds on retry).\n"
                "  2. If your network requires a proxy, set HTTPS_PROXY.\n"
                "  3. To avoid the network entirely, run against a local checkout:\n"
                "       agentproc --profile ./hub/<name>/profile.yaml --prompt \"hi\""
            ),
        ) from e


def _http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=_auth_headers(json=False))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise _http_error_to_hub(e.code, body[:200], url, is_json=False) from e
    except urllib.error.URLError as e:
        raise HubError(
            "could not reach GitHub while fetching hub profile",
            status=0,
            hint=(
                "This is usually a transient network issue. Try:\n"
                "  1. Re-run the command (often succeeds on retry).\n"
                "  2. If your network requires a proxy, set HTTPS_PROXY.\n"
                "  3. To avoid the network entirely, run against a local checkout:\n"
                "       agentproc --profile ./hub/<name>/profile.yaml --prompt \"hi\""
            ),
        ) from e


def _http_error_to_hub(status: int, body: str, url: str, *, is_json: bool) -> HubError:
    """Translate a GitHub HTTP error into a HubError with a remediation hint."""
    authed = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    if status in (403, 429):
        if authed:
            hint = (
                "Your GITHUB_TOKEN is set but still rate-limited. Wait a few minutes and retry,\n"
                "or run against a local checkout instead:\n"
                "  agentproc --profile ./hub/<name>/profile.yaml --prompt \"hi\"\n"
                "\n"
                "Not sure the profile name is right? Check with: agentproc hub list"
            )
        else:
            hint = (
                "GitHub limits anonymous hub fetches to ~60/hour. To raise this to 5,000/hour:\n"
                "  export GITHUB_TOKEN=$(gh auth token)   # if you have the GitHub CLI\n"
                "  # or set GITHUB_TOKEN to any personal access token\n"
                "\n"
                "To skip the network entirely, run against a local checkout:\n"
                "  git clone https://github.com/jeffkit/agentproc && cd agentproc\n"
                "  agentproc --profile ./hub/<name>/profile.yaml --prompt \"hi\"\n"
                "\n"
                "Not sure the profile name is right? Check with: agentproc hub list"
            )
        return HubError(f"GitHub rate-limited the hub fetch (HTTP {status})", status=status, hint=hint)
    if status == 404:
        return HubError(
            "profile not found on GitHub (HTTP 404)",
            status=404,
            hint="Check the profile name with `agentproc hub list`. (Typos are case-sensitive.)",
        )
    return HubError(
        f"GitHub returned HTTP {status} for hub fetch",
        status=status,
        hint=body or "No additional detail from GitHub.",
    )


def _list_remote_files(subpath: str) -> List[Dict[str, str]]:
    """List files in a hub subpath via the git tree API (1 API call for all).

    Returns list of {name, path, type, download_url}. We avoid the Contents
    API because its rate limit is 60/hr unauthenticated; the tree API gives
    us the entire hub/ tree in one call.
    """
    if not subpath.endswith("/"):
        subpath = subpath + "/"
    url = GITHUB_TREES.format(repo=HUB_REPO, ref=HUB_REF)
    data = _http_get_json(url)
    if not isinstance(data, dict) or "tree" not in data:
        raise RuntimeError(f"unexpected tree API response: {type(data).__name__}")
    out: List[Dict[str, str]] = []
    for entry in data["tree"]:
        if not isinstance(entry, dict):
            continue
        p = str(entry.get("path", ""))
        if not p.startswith(subpath):
            continue
        # type is "blob" (file) or "tree" (directory)
        etype = "file" if entry.get("type") == "blob" else (
            "dir" if entry.get("type") == "tree" else ""
        )
        # Name is the part after subpath.
        name = p[len(subpath):].split("/")[0]
        out.append({
            "name": name,
            "path": p,
            "type": etype,
            # We don't use download_url from the API; we use raw URLs.
            "download_url": "",
        })
    # Deduplicate: for directory listing we only want the top-level entries.
    seen = set()
    unique: List[Dict[str, str]] = []
    for e in out:
        if e["name"] in seen:
            continue
        seen.add(e["name"])
        unique.append(e)
    return unique


def _list_remote_profile_files(name: str) -> List[Dict[str, str]]:
    """List the actual files inside hub/<name>/ (not just top-level entries).

    Returns only file entries (type=file), with their full remote paths.
    """
    prefix = f"hub/{name}/"
    url = GITHUB_TREES.format(repo=HUB_REPO, ref=HUB_REF)
    data = _http_get_json(url)
    if not isinstance(data, dict) or "tree" not in data:
        raise RuntimeError(f"unexpected tree API response: {type(data).__name__}")
    out: List[Dict[str, str]] = []
    for entry in data["tree"]:
        if not isinstance(entry, dict):
            continue
        p = str(entry.get("path", ""))
        if not p.startswith(prefix):
            continue
        if entry.get("type") != "blob":
            continue
        # Filename is the last segment.
        fname = p[len(prefix):].split("/")[-1]
        out.append({"name": fname, "path": p})
    return out


def _list_profile_names() -> List[str]:
    """List top-level profile names (directories directly under hub/).

    Cheap: uses the same tree fetch as everything else, so this is one
    network call at most (cached in-memory by urllib).
    """
    url = GITHUB_TREES.format(repo=HUB_REPO, ref=HUB_REF)
    data = _http_get_json(url)
    if not isinstance(data, dict) or "tree" not in data:
        return []
    names: List[str] = []
    seen = set()
    for entry in data["tree"]:
        if not isinstance(entry, dict):
            continue
        p = str(entry.get("path", ""))
        if not p.startswith("hub/"):
            continue
        seg = p[len("hub/"):].split("/")[0]
        if seg and seg not in seen:
            seen.add(seg)
            names.append(seg)
    return sorted(names)


def _suggest_close_name(input_name: str, candidates: List[str]) -> str:
    """Lightweight "did you mean" hint.

    Two paths to a match:
      1. Prefix match — `claude` matches `claude-code`, `echo` matches
         `echo-agent`. Common typo pattern (user forgot a suffix).
         Only accepts an unambiguous prefix.
      2. Edit distance with length-scaled threshold (≤6 → 1 edit,
         7-12 → 2, >12 → 3). Catches transpositions and small typos.
    """
    if not input_name or not candidates:
        return ""
    n = input_name.lower()
    # Path 1: unique prefix match.
    prefix_matches = [c for c in candidates if c.lower().startswith(n)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    # Path 2: edit distance.
    threshold = 1 if len(input_name) <= 6 else (2 if len(input_name) <= 12 else 3)
    best = ""
    best_dist = float("inf")
    for c in candidates:
        d = _edit_distance(n, c.lower())
        if d < best_dist:
            best_dist = d
            best = c
    if best and best_dist <= threshold:
        return best
    return ""


def _edit_distance(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        curr[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,        # deletion
                curr[j - 1] + 1,    # insertion
                prev[j - 1] + cost  # substitution
            )
        prev, curr = curr, prev
    return prev[n]


def _download_file(remote_path: str, local_path: Path) -> None:
    """Download a single file from raw.githubusercontent.com."""
    url = GITHUB_RAW.format(repo=HUB_REPO, ref=HUB_REF, path=remote_path)
    text = _http_get_text(url)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_profile(
    name: str,
    refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
) -> Path:
    """Fetch a profile directory to local cache. Returns the cache path.

    If a fresh cache exists (younger than HUB_CACHE_TTL_SECS) and refresh
    is False, returns immediately without network access.
    """
    age = _cache_age_secs(name)
    cached = cache_dir(name)
    profile_yaml = cached / "profile.yaml"

    if not refresh and age is not None and age < HUB_CACHE_TTL_SECS and profile_yaml.exists():
        if on_log:
            on_log(f"using cached profile: {cached} (age {int(age)}s)")
        return cached

    if on_log:
        if refresh:
            on_log(f"refreshing profile '{name}' from {HUB_REPO}:{HUB_REF}...")
        else:
            on_log(f"fetching profile '{name}' from {HUB_REPO}:{HUB_REF}...")

    entries = _list_remote_profile_files(name)
    if not entries:
        # getTree succeeded (otherwise _list_remote_profile_files would have
        # raised HubError already). So the name is genuinely wrong — surface
        # the list of available names so the user can correct the typo.
        known: List[str] = []
        try:
            known = _list_profile_names()
        except Exception:
            pass
        suggestion = _suggest_close_name(name, known)
        lines = []
        if suggestion:
            lines.append(f"Did you mean `{suggestion}`?")
            lines.append("")
        lines.append("Available profiles:")
        for k in known:
            lines.append(f"  - {k}")
        raise HubError(
            f"profile '{name}' not found in hub",
            status=404,
            hint="\n".join(lines),
        )

    # Clear cache, then re-download every file in the profile directory.
    if cached.exists():
        shutil.rmtree(cached)
    cached.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        local = cached / entry["name"]
        _download_file(entry["path"], local)
        if on_log:
            on_log(f"  - {entry['name']}")

    _write_cache_meta(name)
    return cached


def list_profiles(
    refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, str]]:
    """List profiles in the official hub.

    Returns list of dicts: {name, description, cli, tested}.
    """
    entries = _list_remote_files("hub")
    profiles: List[Dict[str, str]] = []
    for entry in entries:
        if entry["type"] != "dir":
            continue
        name = entry["name"]
        # Read profile.yaml from raw URL to get metadata.
        try:
            yaml_url = GITHUB_RAW.format(
                repo=HUB_REPO, ref=HUB_REF, path=f"hub/{name}/profile.yaml"
            )
            yaml_text = _http_get_text(yaml_url)
            from .cli import parse_yaml  # local import to avoid cycle
            data = parse_yaml(yaml_text)
            profiles.append({
                "name": str(data.get("name", name)),
                "description": str(data.get("description", "")),
                "cli": str(data.get("cli", "")),
                "tested": str(data.get("tested", "unverified")),
            })
        except Exception as e:
            if on_log:
                on_log(f"warning: could not read metadata for {name}: {e}")
            profiles.append({
                "name": name,
                "description": "(failed to read metadata)",
                "cli": "",
                "tested": "unverified",
            })
    return profiles


def show_readme(
    name: str,
    refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
) -> str:
    """Return the README.md content for a profile (fetches if needed)."""
    cached = fetch_profile(name, refresh=refresh, on_log=on_log)
    readme = cached / "README.md"
    if not readme.exists():
        return f"(no README.md for profile '{name}')"
    return readme.read_text(encoding="utf-8")


def install_profile(
    name: str,
    target_dir: Path,
    refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
) -> Path:
    """Copy a cached profile into target_dir/<name>/.

    Useful when the user wants to own and edit the profile locally.
    """
    cached = fetch_profile(name, refresh=refresh, on_log=on_log)
    dest = Path(target_dir) / name
    if dest.exists():
        raise RuntimeError(f"target already exists: {dest}")
    shutil.copytree(cached, dest)
    # Don't copy our cache meta file.
    meta = dest / ".cache-meta.json"
    if meta.exists():
        meta.unlink()
    if on_log:
        on_log(f"installed to: {dest}")
    return dest


# Re-export for type checker
from typing import Any  # noqa: E402
