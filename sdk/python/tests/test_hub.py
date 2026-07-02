"""Tests for agentproc.hub.

Mock-based — no real network access. Covers:
  - jsDelivr data API response parsing (nested tree)
  - Local cache TTL logic
  - bundled-copy fast path (zero network) + _shared population
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
    "hub/_shared/stream_utils.py": "def main_entry():\n    pass\n",
    "hub/_shared/stream_utils.js": "'use strict';\nmodule.exports = {};\n",
    "hub/_shared/README.md": "# shared\n",
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


def _flat_to_nested(flat):
    """Convert flat [{path, type}] → jsDelivr nested {files:[{type,name,files}]}."""
    root = {"files": []}
    dir_nodes: Dict[str, Any] = {"": root}
    for e in sorted(flat, key=lambda x: x["path"]):
        segs = e["path"].split("/")
        name = segs.pop()
        parent_path = "/".join(segs)
        parent = dir_nodes.get(parent_path, root)
        if e["type"] == "tree":
            node = {"type": "directory", "name": name, "files": []}
            parent["files"].append(node)
            dir_nodes[e["path"]] = node
        else:
            parent["files"].append({"type": "file", "name": name})
    return root["files"]


def _make_fake_http_get_json(tree=None):
    """Return a callable emulating _http_get_json for jsDelivr's data API."""
    tree = tree if tree is not None else FAKE_TREE
    nested = _flat_to_nested(tree)

    def fake(url, timeout=30):
        if "data.jsdelivr.com" in url:
            return {"files": nested}
        raise AssertionError(f"unexpected JSON URL: {url}")
    return fake


def _make_fake_http_get_text(contents=None):
    """Return a callable emulating _http_get_text for jsDelivr raw fetches.

    Unmatched URLs raise HubError(404) so _http_get_text_optional returns
    None — modelling an optional file that doesn't exist or a wrong name.
    """
    contents = contents or FAKE_FILE_CONTENTS

    def fake(url, timeout=30):
        for path, content in contents.items():
            if url.endswith(path):
                return content
        raise HubError(f"fetch failed (HTTP 404) for {url}", status=404)
    return fake


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Redirect cache to a tmp dir, disable the bundled copy, reset tree cache."""
    cache_root = tmp_path / "cache" / "hub"
    monkeypatch.setattr(hub_mod, "_cache_root", lambda: cache_root)
    monkeypatch.setattr(hub_mod, "cache_dir", lambda name: cache_root / name)
    # Disable the bundled copy by default so tests exercise the jsDelivr
    # remote path. Bundled-path tests override this via use_bundled_dir().
    monkeypatch.setattr(hub_mod, "_bundled_hub_dir", tmp_path / "no-such-bundle")
    # _tree_cache is module-global; reset so every test starts cold.
    hub_mod._clear_tree_cache()
    return tmp_path


def _use_bundled_dir(monkeypatch, tmp_path, profile_names):
    """Materialize a tmp bundled-hub dir with the given profiles + _shared."""
    bdir = tmp_path / "bundled-hub"
    bdir.mkdir(parents=True, exist_ok=True)
    for name in profile_names:
        prefix = f"hub/{name}/"
        dest = bdir / name
        dest.mkdir(parents=True, exist_ok=True)
        for p, content in FAKE_FILE_CONTENTS.items():
            if p.startswith(prefix):
                (dest / Path(p).name).write_text(content, encoding="utf-8")
    shared = bdir / "_shared"
    shared.mkdir(parents=True, exist_ok=True)
    for p, content in FAKE_FILE_CONTENTS.items():
        if p.startswith("hub/_shared/"):
            (shared / Path(p).name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(hub_mod, "_bundled_hub_dir", bdir)
    return bdir


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
# fetch_profile (remote / jsDelivr)
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

    def test_fetch_happy_path_does_not_call_data_api(self, isolated_cache):
        json_calls = {"n": 0}

        def assert_no_json(url, timeout=30):
            json_calls["n"] += 1
            raise AssertionError(f"unexpected jsDelivr data API call: {url}")

        with patch("agentproc.hub._http_get_json", side_effect=assert_no_json), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.fetch_profile("echo-agent")
        assert json_calls["n"] == 0

    def test_fetch_skips_optional_files_that_404(self, isolated_cache):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            p = hub_mod.fetch_profile("claude-code")
        names = [x.name for x in p.iterdir()]
        assert "profile.yaml" in names
        assert "bridge.py" in names
        assert "bridge.js" in names
        assert "README.md" in names
        assert "bridge.sh" not in names

    def test_fetch_populates_shared_in_cache_root(self, isolated_cache):
        # Bridges do `from _shared.stream_utils import ...` against the cache
        # root, so _shared must be populated whenever a profile is fetched.
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.fetch_profile("claude-code")
        shared = hub_mod._cache_root() / "_shared"
        assert (shared / "stream_utils.py").exists(), \
            "_shared/stream_utils.py not cached — bridge.py import would fail"
        assert (shared / "stream_utils.js").exists()

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
            assert call_count["json"] == first_json
            assert call_count["text"] == first_text

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
            assert call_count["text"] > first_text

    def test_fetch_overwrites_old_files(self, isolated_cache):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.fetch_profile("echo-agent")
        cached_file = cache_dir("echo-agent") / "bridge.py"
        original = cached_file.read_text()
        cached_file.write_text("# tampered\n")
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.fetch_profile("echo-agent", refresh=True)
        assert cached_file.read_text() == original


# ---------------------------------------------------------------------------
# fetch_profile (bundled)
# ---------------------------------------------------------------------------

class TestFetchProfileBundled:
    def test_uses_bundled_copy_with_zero_network(self, isolated_cache, monkeypatch, tmp_path):
        _use_bundled_dir(monkeypatch, tmp_path, ["echo-agent"])
        with patch("agentproc.hub._http_get_json", side_effect=AssertionError("no network")), \
             patch("agentproc.hub._http_get_text", side_effect=AssertionError("no network")):
            p = hub_mod.fetch_profile("echo-agent")
        assert (p / "profile.yaml").exists()
        assert (p / "bridge.py").exists()

    def test_bundled_ndjson_profile_also_caches_shared(self, isolated_cache, monkeypatch, tmp_path):
        # The pre-bundle bug: fetching claude-code did not bring _shared, so
        # bridge.py's `from _shared.stream_utils import ...` failed at runtime.
        _use_bundled_dir(monkeypatch, tmp_path, ["claude-code"])
        with patch("agentproc.hub._http_get_json", side_effect=AssertionError("no network")), \
             patch("agentproc.hub._http_get_text", side_effect=AssertionError("no network")):
            hub_mod.fetch_profile("claude-code")
        assert (hub_mod._cache_root() / "_shared" / "stream_utils.py").exists()

    def test_falls_back_to_remote_for_profile_not_in_bundle(self, isolated_cache, monkeypatch, tmp_path):
        _use_bundled_dir(monkeypatch, tmp_path, ["echo-agent"])
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            p = hub_mod.fetch_profile("claude-code")
        assert (p / "profile.yaml").exists()


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

    def test_list_skips_underscore_utility_dirs(self, isolated_cache):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            profiles = hub_mod.list_profiles()
        names = [p["name"] for p in profiles]
        assert not any(n.startswith("_") for n in names), names
        assert "_shared" not in names

    def test_tree_disk_cached_across_calls(self, isolated_cache):
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
            assert json_calls["n"] == 1, "second call hit the data API again"

    def test_bundled_reads_metadata_locally_with_zero_network(self, isolated_cache, monkeypatch, tmp_path):
        _use_bundled_dir(monkeypatch, tmp_path, ["echo-agent", "claude-code"])
        with patch("agentproc.hub._http_get_json", side_effect=AssertionError("no network")), \
             patch("agentproc.hub._http_get_text", side_effect=AssertionError("no network")):
            profiles = hub_mod.list_profiles()
        names = sorted(p["name"] for p in profiles)
        assert names == ["claude-code", "echo-agent"]


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
        assert not (dest / ".cache-meta.json").exists()

    def test_install_also_installs_shared(self, isolated_cache, tmp_path):
        with patch("agentproc.hub._http_get_json", side_effect=_make_fake_http_get_json()), \
             patch("agentproc.hub._http_get_text", side_effect=_make_fake_http_get_text()):
            hub_mod.install_profile("claude-code", tmp_path)
        assert (tmp_path / "_shared" / "stream_utils.py").exists(), \
            "_shared not installed — bridge.py import would fail"

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
