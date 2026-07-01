"""Tests for agentproc.hub.

Mock-based — no real network access. Covers:
  - Tree API response parsing
  - Local cache TTL logic
  - list/show/install/run operations
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from agentproc import hub as hub_mod
from agentproc.hub import (
    HUB_CACHE_TTL_SECS,
    HubError,
    cache_dir,
    _cache_root,
    _cache_age_secs,
    _write_cache_meta,
)


# ---------------------------------------------------------------------------
# Test fixtures: synthetic GitHub tree + file contents
# ---------------------------------------------------------------------------

FAKE_TREE = [
    {"path": "hub", "type": "tree"},
    {"path": "hub/_shared", "type": "tree"},
    {"path": "hub/_shared/stream_utils.py", "type": "blob"},
    {"path": "hub/_shared/stream_utils.js", "type": "blob"},
    {"path": "hub/_shared/README.md", "type": "blob"},
    {"path": "hub/echo-agent", "type": "tree"},
    {"path": "hub/echo-agent/profile.yaml", "type": "blob"},
    {"path": "hub/echo-agent/bridge.py", "type": "blob"},
    {"path": "hub/echo-agent/bridge.js", "type": "blob"},
    {"path": "hub/echo-agent/bridge.sh", "type": "blob"},
    {"path": "hub/echo-agent/README.md", "type": "blob"},
    {"path": "hub/claude-code", "type": "tree"},
    {"path": "hub/claude-code/profile.yaml", "type": "blob"},
    {"path": "hub/claude-code/bridge.py", "type": "blob"},
    {"path": "hub/claude-code/bridge.js", "type": "blob"},
    {"path": "hub/claude-code/README.md", "type": "blob"},
    {"path": "README.md", "type": "blob"},
    {"path": "spec/protocol.md", "type": "blob"},
]

FAKE_FILE_CONTENTS = {
    "hub/echo-agent/profile.yaml": (
        "name: echo-agent\n"
        "description: Minimal hello-world agent\n"
        "cli: none\n"
        "agentproc:\n"
        "  command: python3 ./bridge.py\n"
        "  cwd: .\n"
        "tested: official\n"
        "maintainer: jeffkit\n"
    ),
    "hub/echo-agent/bridge.py": "#!/usr/bin/env python3\nprint('echo')\n",
    "hub/echo-agent/bridge.js": "'use strict';\nconsole.log('echo');\n",
    "hub/echo-agent/bridge.sh": "#!/usr/bin/env bash\necho echo\n",
    "hub/echo-agent/README.md": "# echo-agent\n\nHello world.\n",
    "hub/claude-code/profile.yaml": (
        "name: claude-code\n"
        "description: Claude Code wrapper\n"
        "cli: claude\n"
        "agentproc:\n"
        "  command: python3 ./bridge.py\n"
        "tested: official\n"
        "maintainer: jeffkit\n"
    ),
    "hub/claude-code/bridge.py": "#!/usr/bin/env python3\nprint('claude')\n",
    "hub/claude-code/bridge.js": "'use strict';\nconsole.log('claude');\n",
    "hub/claude-code/README.md": "# claude-code\n\nReal wrapper.\n",
}


def _make_fake_http_get_json(tree=None):
    """Return a callable that emulates _http_get_json for the tree API."""
    tree = tree if tree is not None else FAKE_TREE
    def fake(url, timeout=30):
        if "git/trees" in url:
            return {"tree": tree}
        raise AssertionError(f"unexpected JSON URL: {url}")
    return fake


def _make_fake_http_get_text(contents=None):
    """Return a callable that emulates _http_get_text for raw file fetches.

    Unmatched URLs raise a HubError(404) so that _http_get_text_optional
    (which wraps _http_get_text and swallows 404) returns None — modelling
    an optional profile file that doesn't exist (e.g. bridge.sh on a
    non-echo profile) or a wrong profile name.
    """
    contents = contents or FAKE_FILE_CONTENTS
    def fake(url, timeout=30):
        # Extract path from raw URL: .../agentproc/main/hub/echo-agent/profile.yaml
        for path, content in contents.items():
            if url.endswith(path):
                return content
        raise HubError(f"fetch failed (HTTP 404) for {url}", status=404)
    return fake


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Redirect cache to a tmp dir and reset the module-level tree cache."""
    cache_root = tmp_path / "cache" / "hub"
    monkeypatch.setattr(hub_mod, "_cache_root", lambda: cache_root)
    monkeypatch.setattr(hub_mod, "cache_dir", lambda name: cache_root / name)
    # _tree_cache is module-global and would otherwise leak across tests
    # (each test uses a different fake tree). Reset it so every test starts
    # cold and _get_tree re-reads from the (fresh) fake _http_get_json.
    hub_mod._clear_tree_cache()
    return tmp_path


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

class TestCacheHelpers:
    def test_cache_age_none_when_not_cached(self, isolated_cache):
        assert _cache_age_secs("never-cached") is None

    def test_cache_age_after_write(self, isolated_cache):
        _write_cache_meta("foo")
        age = _cache_age_secs("foo")
        assert age is not None
        assert age < 5  # just written

    def test_cache_age_old(self, isolated_cache, tmp_path):
        # Manually write an old marker.
        marker = isolated_cache / "cache" / "hub" / "old" / ".cache-meta.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"fetched_at": time.time() - 100000, "ref": "main"}))
        age = _cache_age_secs("old")
        assert age is not None
        assert age > 100000

    def test_cache_age_invalid_meta(self, isolated_cache):
        marker = isolated_cache / "cache" / "hub" / "bad" / ".cache-meta.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("not json at all")
        assert _cache_age_secs("bad") is None


# ---------------------------------------------------------------------------
# fetch_profile
# ---------------------------------------------------------------------------

class TestFetchProfile:
    def test_fetch_downloads_all_files(self, isolated_cache):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            p = hub_mod.fetch_profile("echo-agent", on_log=lambda m: None)
        assert p.exists()
        names = sorted(x.name for x in p.iterdir())
        assert "profile.yaml" in names
        assert "bridge.py" in names
        assert "bridge.js" in names
        assert "README.md" in names
        assert ".cache-meta.json" in names

    def test_fetch_happy_path_does_not_call_trees_api(self, isolated_cache):
        # `hub run` fetches profile files via raw.githubusercontent.com (CDN,
        # not rate-limited). A known profile must not trigger any
        # api.github.com call — that's the whole point of the rate-limit fix.
        json_calls = {"n": 0}

        def assert_no_json(url, timeout=30):
            json_calls["n"] += 1
            raise AssertionError(f"unexpected Trees API call: {url}")

        with patch("agentproc.hub._http_get_json", side_effect=assert_no_json), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.fetch_profile("echo-agent")
        assert json_calls["n"] == 0

    def test_fetch_skips_optional_files_that_404(self, isolated_cache):
        # claude-code has no bridge.sh in the fixtures → the 404 is swallowed
        # and bridge.sh is not stored, while the four standard files are.
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            p = hub_mod.fetch_profile("claude-code")
        names = [x.name for x in p.iterdir()]
        assert "profile.yaml" in names
        assert "bridge.py" in names
        assert "bridge.js" in names
        assert "README.md" in names
        assert "bridge.sh" not in names

    def test_fetch_unknown_profile_raises(self, isolated_cache):
        empty_tree = [{"path": "hub", "type": "tree"}]
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json(empty_tree)), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            with pytest.raises(RuntimeError, match="not found in hub"):
                hub_mod.fetch_profile("nope")

    def test_fetch_uses_cache_on_second_call(self, isolated_cache):
        call_count = {"json": 0, "text": 0}

        def counting_json(url, timeout=30):
            call_count["json"] += 1
            return _make_fake_http_get_json()(url, timeout)

        def counting_text(url, timeout=30):
            call_count["text"] += 1
            return _make_fake_http_get_text()(url, timeout)

        with patch("agentproc.hub._http_get_json", side_effect=counting_json), \
             patch("agentproc.hub._http_get_text", side_effect=counting_text):
            hub_mod.fetch_profile("echo-agent")
            first_json = call_count["json"]
            first_text = call_count["text"]
            hub_mod.fetch_profile("echo-agent")  # should hit cache
            assert call_count["json"] == first_json  # no new API calls
            assert call_count["text"] == first_text  # no new file downloads

    def test_refresh_forces_refetch(self, isolated_cache):
        call_count = {"text": 0}

        def counting_text(url, timeout=30):
            call_count["text"] += 1
            return _make_fake_http_get_text()(url, timeout)

        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=counting_text):
            hub_mod.fetch_profile("echo-agent")
            first_text = call_count["text"]
            hub_mod.fetch_profile("echo-agent", refresh=True)
            # hub run fetches files via raw URLs (CDN), not the rate-limited
            # Trees API — so a refresh re-fetches the file set, not the tree.
            assert call_count["text"] > first_text

    def test_fetch_overwrites_old_files(self, isolated_cache):
        # First fetch.
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.fetch_profile("echo-agent")
        # Modify cache.
        cached_file = cache_dir("echo-agent") / "bridge.py"
        original = cached_file.read_text()
        cached_file.write_text("# tampered\n")
        # Refresh — should restore original.
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.fetch_profile("echo-agent", refresh=True)
        assert cached_file.read_text() == original


# ---------------------------------------------------------------------------
# list_profiles
# ---------------------------------------------------------------------------

class TestListProfiles:
    def test_list_returns_all_dirs(self, isolated_cache):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            profiles = hub_mod.list_profiles()
        names = sorted(p["name"] for p in profiles)
        assert names == ["claude-code", "echo-agent"]
        ec = next(p for p in profiles if p["name"] == "echo-agent")
        assert ec["tested"] == "official"
        assert ec["description"] == "Minimal hello-world agent"
        assert ec["cli"] == "none"

    def test_list_skips_non_hub_paths(self, isolated_cache):
        """The repo root README.md and spec/ should not appear in hub listing."""
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            profiles = hub_mod.list_profiles()
        # No root-level files leaked.
        for p in profiles:
            assert p["name"].startswith("")  # just a sanity check
            assert "/" not in p["name"]

    def test_list_skips_underscore_utility_dirs(self, isolated_cache):
        """`_shared/` holds bridge helpers, not a profile — must be excluded."""
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            profiles = hub_mod.list_profiles()
        names = [p["name"] for p in profiles]
        assert not any(n.startswith("_") for n in names), names
        assert "_shared" not in names

    def test_tree_disk_cached_across_calls(self, isolated_cache):
        # First list_profiles hits the Trees API (json 0→1) and writes
        # ~/.agentproc/cache/hub/tree.json. A second call reuses the cached
        # tree and must not make another API call.
        json_calls = {"n": 0}

        def counting_json(url, timeout=30):
            json_calls["n"] += 1
            return _make_fake_http_get_json()(url, timeout)

        with patch("agentproc.hub._http_get_json", side_effect=counting_json), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.list_profiles()
            assert json_calls["n"] == 1
            assert (hub_mod._cache_root() / "tree.json").exists()
            hub_mod.list_profiles()
            assert json_calls["n"] == 1, "second call hit the Trees API again"


# ---------------------------------------------------------------------------
# show_readme
# ---------------------------------------------------------------------------

class TestShowReadme:
    def test_returns_readme_content(self, isolated_cache):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            text = hub_mod.show_readme("echo-agent")
        assert "echo-agent" in text
        assert "Hello world" in text

    def test_missing_readme_returns_placeholder(self, isolated_cache):
        # Fetch a profile with no README.
        tree = [
            {"path": "hub/noreadme", "type": "tree"},
            {"path": "hub/noreadme/profile.yaml", "type": "blob"},
        ]
        contents = {"hub/noreadme/profile.yaml": "name: noreadme\ndescription: x\ntested: unverified\n"}
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json(tree)), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text(contents)):
            text = hub_mod.show_readme("noreadme")
        assert "no README.md" in text


# ---------------------------------------------------------------------------
# install_profile
# ---------------------------------------------------------------------------

class TestInstallProfile:
    def test_install_copies_to_target(self, isolated_cache, tmp_path):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            dest = hub_mod.install_profile("echo-agent", tmp_path)
        assert dest.exists()
        assert (dest / "profile.yaml").exists()
        assert (dest / "bridge.py").exists()
        # Cache meta should NOT be copied to the installed copy.
        assert not (dest / ".cache-meta.json").exists()

    def test_install_refuses_existing_target(self, isolated_cache, tmp_path):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.install_profile("echo-agent", tmp_path)
            with pytest.raises(RuntimeError, match="target already exists"):
                hub_mod.install_profile("echo-agent", tmp_path)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_hub_cache_ttl_is_24h():
    assert HUB_CACHE_TTL_SECS == 24 * 60 * 60


def test_hub_repo_constants():
    assert hub_mod.HUB_REPO == "jeffkit/agentproc"
    assert hub_mod.HUB_REF == "main"
