"""Hub client — fetch and manage profile directories from the official Hub.

The Hub lives at https://github.com/jeffkit/agentproc/tree/main/hub/

Resolution order (so `hub run` / `hub list` work with zero network in the
common case, and stay usable where GitHub itself is unreachable, e.g. China):

  1. Bundled copy — the entire hub/ directory is shipped inside this package
     (at ``<pkg>/hub_data/``). ``hub run`` and ``hub list`` read from it
     directly. No network. This is the default and what most users hit.
  2. jsDelivr CDN — for ``--refresh`` or a profile newer than the installed
     CLI: files come from cdn.jsdelivr.net (Fastly CDN, not GitHub's
     rate-limited API), and the directory listing from jsDelivr's data API.
     jsDelivr is reachable in regions where raw.githubusercontent.com is not.

Remote-fetched profiles are cached at ~/.agentproc/cache/hub/<name>/ with a
24-hour TTL; the shared ``_shared/`` bridge helpers are cached alongside at
~/.agentproc/cache/hub/_shared/ (the bridge scripts import them via a
sibling path).

Public API:
    HUB_REPO            — the github repo id ("jeffkit/agentproc")
    HUB_REF             — the git ref to fetch from ("main")
    HUB_CACHE_TTL_SECS  — default 24 hours
    cache_dir(name)     — Path to the local cache directory for a profile
    fetch_profile(name, refresh=False, on_log=None) -> Path
    list_profiles(refresh=False, on_log=None) -> List[Dict[str,str]]
    show_readme(name, refresh=False, on_log=None) -> str
    install_profile(name, target_dir, refresh=False, on_log=None) -> Path

All network access is via urllib (stdlib). Profile YAML parsing goes through
``agentproc.yaml.parse_yaml`` (PyYAML), shared with the CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .yaml import parse_yaml

HUB_REPO = "jeffkit/agentproc"
HUB_REF = "main"
HUB_CACHE_TTL_SECS = 24 * 60 * 60  # 24 hours

# jsDelivr mirrors the GitHub repo on a global CDN (Fastly), reachable where
# raw.githubusercontent.com / api.github.com are not. No token, no 60/hr limit.
JSDELIVR_RAW = "https://cdn.jsdelivr.net/gh/{repo}@{ref}/{path}"
JSDELIVR_DATA = "https://data.jsdelivr.com/v1/packages/gh/{repo}@{ref}"

# The hub directory shipped inside this package. Defaults to <pkg>/hub_data/
# (this file is at <pkg>/hub.py). Named ``hub_data`` rather than ``hub`` so it
# does not shadow this module. Overridable via set_bundled_hub_dir() for tests.
_bundled_hub_dir: Path = Path(__file__).parent / "hub_data"


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


def set_bundled_hub_dir(p: Path) -> Path:
    """Override the bundled hub directory (used by tests). Returns the prior."""
    global _bundled_hub_dir
    prev = _bundled_hub_dir
    _bundled_hub_dir = Path(p)
    return prev


def _bundled_has(name: str) -> bool:
    return (_bundled_hub_dir / name / "profile.yaml").exists()


def _auth_headers() -> Dict[str, str]:
    return {"User-Agent": "agentproc-cli"}


_NETWORK_HINT = (
    "Could not reach the hub CDN (jsDelivr). Try:\n"
    "  1. Re-run the command (often succeeds on retry).\n"
    "  2. If your network requires a proxy, set HTTPS_PROXY.\n"
    "  3. Profiles ship bundled with this CLI, so the common case needs no\n"
    '     network. To use a local checkout instead:\n'
    '       agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"'
)


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
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise HubError(
            f"hub CDN returned HTTP {e.code}",
            status=e.code,
            hint=body[:200] or _NETWORK_HINT,
        ) from e
    except urllib.error.URLError as e:
        raise HubError("could not reach the hub CDN", status=0, hint=_NETWORK_HINT) from e


def _http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code == 404:
            raise HubError(
                f"fetch failed (HTTP 404) for {url}",
                status=404,
                hint="Profile files should exist in the hub repo. Try `agentproc hub list`.",
            ) from e
        raise HubError(
            f"fetch failed (HTTP {e.code}) for {url}",
            status=e.code,
            hint=body[:200] or _NETWORK_HINT,
        ) from e
    except urllib.error.URLError as e:
        raise HubError("could not reach the hub CDN", status=0, hint=_NETWORK_HINT) from e


def _http_get_text_optional(url: str, timeout: int = 30) -> Optional[str]:
    """Like _http_get_text, but returns None on 404 instead of raising.

    Used for probing optional profile files (e.g. bridge.sh only exists for
    echo-agent) and for detecting "profile does not exist" without a separate
    API call. Delegates to _http_get_text so callers that patch
    _http_get_text in tests automatically cover this path.
    """
    try:
        return _http_get_text(url, timeout=timeout)
    except HubError as e:
        if e.status == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# Repo tree (jsDelivr data API) — cached in-memory and on disk (24h TTL)
# ---------------------------------------------------------------------------

_tree_cache: Optional[List[Dict[str, str]]] = None


def _tree_cache_path() -> Path:
    return _cache_root() / "tree.json"


def _clear_tree_cache() -> None:
    """Drop the in-memory tree cache and delete the disk cache file."""
    global _tree_cache
    _tree_cache = None
    p = _tree_cache_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def _flatten_jsdelivr_tree(files: list, prefix: str = "") -> List[Dict[str, str]]:
    """Flatten jsDelivr's nested {files:[{type:'directory', files:[...]}]} tree
    into the flat [{path, type:'blob'|'tree'}] shape the rest of this module
    expects (same shape GitHub's Trees API returned).
    """
    out: List[Dict[str, str]] = []
    for e in files:
        if not isinstance(e, dict):
            continue
        p = prefix + str(e.get("name", ""))
        if e.get("type") == "directory":
            out.append({"path": p, "type": "tree"})
            if isinstance(e.get("files"), list):
                out.extend(_flatten_jsdelivr_tree(e["files"], p + "/"))
        else:
            out.append({"path": p, "type": "blob"})
    return out


def _get_tree() -> List[Dict[str, str]]:
    """Return the full repo tree as a list of {path, type('blob'|'tree')}.

    Serves from in-memory cache → disk cache (24h TTL) → jsDelivr data API,
    writing each layer as it misses. The API is not rate-limited like
    GitHub's Trees API.
    """
    global _tree_cache
    if _tree_cache is not None:
        return _tree_cache

    tp = _tree_cache_path()
    if tp.exists():
        try:
            meta = json.loads(tp.read_text(encoding="utf-8"))
            age = max(0.0, time.time() - float(meta.get("fetched_at", 0)))
            if age < HUB_CACHE_TTL_SECS and isinstance(meta.get("tree"), list):
                _tree_cache = [
                    {"path": str(e.get("path", "")), "type": str(e.get("type", ""))}
                    for e in meta["tree"] if isinstance(e, dict)
                ]
                return _tree_cache
        except (ValueError, OSError):
            pass  # corrupt cache file — refetch

    url = JSDELIVR_DATA.format(repo=HUB_REPO, ref=HUB_REF)
    data = _http_get_json(url)
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise RuntimeError(f"unexpected jsDelivr data API response: {type(data).__name__}")
    _tree_cache = _flatten_jsdelivr_tree(data["files"])

    try:
        _cache_root().mkdir(parents=True, exist_ok=True)
        tp.write_text(
            json.dumps(
                {"fetched_at": time.time(), "ref": HUB_REF, "tree": _tree_cache},
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # disk cache is best-effort

    return _tree_cache


def _list_remote_files(subpath: str) -> List[Dict[str, str]]:
    """List top-level entries in a hub subpath (e.g. 'hub' → all profile dirs)."""
    if not subpath.endswith("/"):
        subpath = subpath + "/"
    tree = _get_tree()
    out: List[Dict[str, str]] = []
    seen = set()
    for entry in tree:
        p = entry["path"]
        if not p.startswith(subpath):
            continue
        name = p[len(subpath):].split("/")[0]
        if not name or name in seen:
            continue
        seen.add(name)
        is_dir = any(t["path"] == subpath + name and t["type"] == "tree" for t in tree)
        out.append({
            "name": name,
            "path": p,
            "type": "dir" if is_dir else "file",
            "download_url": "",
        })
    return out


def _list_profile_names() -> List[str]:
    """List top-level profile names (directories directly under hub/).

    Uses the bundled copy if present (no network), else the disk-cached
    remote tree. `_`-prefixed utility dirs (e.g. `_shared`) are excluded.
    """
    if _bundled_hub_dir.exists():
        names = []
        for entry in _bundled_hub_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("_") \
                    and (entry / "profile.yaml").exists():
                names.append(entry.name)
        return sorted(names)
    tree = _get_tree()
    names: List[str] = []
    seen = set()
    for entry in tree:
        p = entry["path"]
        if not p.startswith("hub/"):
            continue
        seg = p[len("hub/"):].split("/")[0]
        if seg and not seg.startswith("_") and seg not in seen:
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
    prefix_matches = [c for c in candidates if c.lower().startswith(n)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
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


# Every hub profile is this fixed set of files (see hub/README.md):
#   profile.yaml (required) + bridge.py + bridge.js + README.md,
# with echo-agent additionally shipping bridge.sh. `_shared/` ships
# stream_utils.{py,js} + README.md. If a future profile adds a new file
# type, extend these tuples.
_PROFILE_FILE_CANDIDATES = (
    "profile.yaml",
    "bridge.py",
    "bridge.js",
    "bridge.sh",
    "README.md",
)
_SHARED_FILE_CANDIDATES = ("stream_utils.py", "stream_utils.js", "README.md")

# Exclude Python bytecode / editor cruft when copying bundled dirs to cache.
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _clear_dir(d: Path) -> None:
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)


def _copy_bundled(subname: str, dest: Path) -> bool:
    src = _bundled_hub_dir / subname
    if not src.exists():
        return False
    # copytree creates dest itself; it errors if dest already exists, so wipe
    # first without recreating.
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=_COPY_IGNORE)
    return True


def _ensure_shared_cached(*, refresh: bool, on_log) -> None:
    """Ensure `_shared/` is in the cache root, from the bundle or jsDelivr.

    Bridges do `from _shared.stream_utils import ...` with the cache root on
    sys.path, so this must be populated whenever a profile is fetched.
    Skipped if a fresh _shared cache already exists.
    """
    age = _cache_age_secs("_shared")
    sdir = cache_dir("_shared")
    if not refresh and age is not None and age < HUB_CACHE_TTL_SECS \
            and (sdir / "stream_utils.py").exists():
        return
    if _bundled_hub_dir.exists():
        if _copy_bundled("_shared", sdir):
            _write_cache_meta("_shared")
            return
    # Remote: fetch the candidate file set via jsDelivr raw URLs.
    _clear_dir(sdir)
    for fname in _SHARED_FILE_CANDIDATES:
        url = JSDELIVR_RAW.format(repo=HUB_REPO, ref=HUB_REF, path=f"hub/_shared/{fname}")
        text = _http_get_text_optional(url)
        if text is None:
            continue
        (sdir / fname).write_text(text, encoding="utf-8")
        if on_log:
            on_log(f"  - _shared/{fname}")
    _write_cache_meta("_shared")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_profile(
    name: str,
    refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
) -> Path:
    """Fetch a profile directory to local cache. Returns the cache path.

    Resolution: fresh cache → bundled copy (default, zero network) → jsDelivr
    CDN (for refresh or a profile not in the bundle). ``_shared/`` is
    populated alongside so the bridge scripts can import it.

    If a fresh cache exists (younger than HUB_CACHE_TTL_SECS) and refresh
    is False, returns immediately without network access.
    """
    if refresh:
        _clear_tree_cache()

    age = _cache_age_secs(name)
    cached = cache_dir(name)
    profile_yaml = cached / "profile.yaml"

    if not refresh and age is not None and age < HUB_CACHE_TTL_SECS and profile_yaml.exists():
        if on_log:
            on_log(f"using cached profile: {cached} (age {int(age)}s)")
        return cached

    # 1) Bundled fast path — zero network, the default for most users.
    if not refresh and _bundled_has(name):
        if on_log:
            on_log(f"using bundled profile: {name}")
        _copy_bundled(name, cached)
        _write_cache_meta(name)
        _ensure_shared_cached(refresh=refresh, on_log=on_log)
        return cached

    if on_log:
        if refresh:
            on_log(f"refreshing profile '{name}' from jsDelivr CDN...")
        else:
            on_log(f"fetching profile '{name}' from jsDelivr CDN...")

    # 2) Remote via jsDelivr. Probe profile.yaml first.
    probe_url = JSDELIVR_RAW.format(repo=HUB_REPO, ref=HUB_REF, path=f"hub/{name}/profile.yaml")
    probe = _http_get_text_optional(probe_url)
    if probe is None:
        # profile.yaml 404 → wrong name. Produce a "did you mean" hint from
        # the bundled listing (no network) or the disk-cached remote tree.
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

    # Clear cache, then download the candidate file set via raw URLs.
    _clear_dir(cached)
    (cached / "profile.yaml").write_text(probe, encoding="utf-8")
    if on_log:
        on_log("  - profile.yaml")

    for fname in _PROFILE_FILE_CANDIDATES:
        if fname == "profile.yaml":
            continue
        url = JSDELIVR_RAW.format(repo=HUB_REPO, ref=HUB_REF, path=f"hub/{name}/{fname}")
        text = _http_get_text_optional(url)
        if text is None:
            continue  # optional file not present for this profile
        (cached / fname).write_text(text, encoding="utf-8")
        if on_log:
            on_log(f"  - {fname}")

    _write_cache_meta(name)
    _ensure_shared_cached(refresh=refresh, on_log=on_log)
    return cached


def list_profiles(
    refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, str]]:
    """List profiles in the official hub.

    Reads from the bundled copy by default (zero network). With refresh=True
    (or no bundle) it queries jsDelivr's data API and fetches each
    profile.yaml for metadata.

    Returns list of dicts: {name, description, cli, tested}.
    """
    if not refresh and _bundled_hub_dir.exists():
        profiles: List[Dict[str, str]] = []
        for entry in _bundled_hub_dir.iterdir():
            if not (entry.is_dir() and not entry.name.startswith("_")):
                continue
            yaml_path = entry / "profile.yaml"
            if not yaml_path.exists():
                continue
            try:
                data = parse_yaml(yaml_path.read_text(encoding="utf-8"))
                profiles.append({
                    "name": str(data.get("name", entry.name)),
                    "description": str(data.get("description", "")),
                    "cli": str(data.get("cli", "")),
                    "tested": str(data.get("tested", "unverified")),
                })
            except Exception as e:
                if on_log:
                    on_log(f"warning: could not read metadata for {entry.name}: {e}")
                profiles.append({
                    "name": entry.name,
                    "description": "(failed to read metadata)",
                    "cli": "",
                    "tested": "unverified",
                })
        return profiles

    entries = _list_remote_files("hub")
    profiles = []
    for entry in entries:
        if entry["type"] != "dir":
            continue
        name = entry["name"]
        if name.startswith("_"):
            continue
        try:
            yaml_url = JSDELIVR_RAW.format(
                repo=HUB_REPO, ref=HUB_REF, path=f"hub/{name}/profile.yaml"
            )
            yaml_text = _http_get_text(yaml_url)
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
    """Copy a profile into target_dir/<name>/, along with `_shared/`.

    The bridge scripts import from `_shared` via a sibling path, so it must
    be installed alongside for the installed profile to run.
    """
    cached = fetch_profile(name, refresh=refresh, on_log=on_log)
    dest = Path(target_dir) / name
    if dest.exists():
        raise RuntimeError(f"target already exists: {dest}")
    shutil.copytree(cached, dest, ignore=_COPY_IGNORE)
    meta = dest / ".cache-meta.json"
    if meta.exists():
        meta.unlink()
    shared_src = cache_dir("_shared")
    shared_dest = Path(target_dir) / "_shared"
    if shared_src.exists() and not shared_dest.exists():
        shutil.copytree(shared_src, shared_dest, ignore=_COPY_IGNORE)
    if on_log:
        on_log(f"installed to: {dest}")
    return dest
