"""Codex permission-mode temp CODEX_HOME lifecycle (issue #15).

Permission mode copies ~/.codex/auth.json into a temp dir. If the bridge is
killed (SIGKILL) no cleanup handler runs, so the credential copy lingers in
/tmp. The bridge must:
  - name temp dirs agentproc-codex-<pid>-… (owner identifiable),
  - sweep stale leftover dirs at startup (skipping live-owner and young dirs),
  - clean up its own dir on normal exit and on SIGTERM.

Observable parity: hub/codex/bridge.js has the same behaviour.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bridges import _load_bridge  # noqa: E402

HUB_ROOT = Path(__file__).resolve().parents[3] / "hub"
BRIDGE_PY = HUB_ROOT / "codex" / "bridge.py"
BRIDGE_JS = HUB_ROOT / "codex" / "bridge.js"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token": "secret"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return home


def _fake_codex_cli(tmp_path: Path) -> Path:
    """A `codex` stub that prints a session + message and exits 0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    cli = bin_dir / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 't1'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'done'}}))\n"
        "sys.exit(0)\n"
    )
    cli.chmod(0o755)
    return cli


def _run_bridge_py(env_overrides, turn_obj, cwd=None, timeout=30):
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, str(BRIDGE_PY)],
        input=json.dumps(turn_obj) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=timeout,
    )


def _perm_dirs(tmp_root: Path):
    return [p for p in Path(tmp_root).glob("agentproc-codex-*")]


TURN = {
    "type": "turn",
    "message": "hi",
    "session_id": "",
    "protocol_version": "0.4",
    "permission": True,
}


class TestPyPreparePermissionHome:
    def test_dir_name_contains_pid(self, fake_home, tmp_path):
        mod = _load_bridge("codex")
        import tempfile as tf
        real_tmpdir = tf.gettempdir
        tf.gettempdir = lambda: str(tmp_path)
        try:
            tmp, _sock = mod._prepare_permission_home()
        finally:
            tf.gettempdir = real_tmpdir
            mod._release_permission_home(tmp)
        assert os.path.basename(tmp).startswith(f"agentproc-codex-{os.getpid()}-")

    def test_sweep_removes_stale_dir(self, fake_home, tmp_path):
        stale = tmp_path / f"agentproc-codex-999999-deadbeef"
        stale.mkdir()
        (stale / "auth.json").write_text("{}")
        old = time.time() - 7200
        os.utime(stale, (old, old))
        mod = _load_bridge("codex")
        import tempfile as tf
        real_tmpdir = tf.gettempdir
        tf.gettempdir = lambda: str(tmp_path)
        try:
            mod._sweep_stale_permission_homes()
        finally:
            tf.gettempdir = real_tmpdir
        assert not stale.exists(), "stale credential dir must be swept at startup"

    def test_sweep_keeps_live_pid_dir_and_young_dir(self, fake_home, tmp_path):
        live = tmp_path / f"agentproc-codex-{os.getpid()}-live"
        live.mkdir()
        old = time.time() - 7200
        os.utime(live, (old, old))  # old but owner alive → keep
        young_dead = tmp_path / "agentproc-codex-999999-young"
        young_dead.mkdir()  # dead owner but young → keep
        mod = _load_bridge("codex")
        import tempfile as tf
        real_tmpdir = tf.gettempdir
        tf.gettempdir = lambda: str(tmp_path)
        try:
            mod._sweep_stale_permission_homes()
        finally:
            tf.gettempdir = real_tmpdir
        assert live.exists()
        assert young_dead.exists()


class TestPyBridgeEndToEnd:
    def test_no_residue_after_normal_exit(self, fake_home, tmp_path):
        cli = _fake_codex_cli(tmp_path)
        env = {"PATH": f"{cli.parent}:{os.environ['PATH']}",
               "TMPDIR": str(tmp_path)}
        r = _run_bridge_py(env, TURN)
        assert r.returncode == 0, r.stderr
        residue = [p for p in tmp_path.glob("agentproc-codex-*")]
        assert residue == [], f"credential temp dirs left behind: {residue}"

    def test_startup_sweep_removes_leftover(self, fake_home, tmp_path):
        cli = _fake_codex_cli(tmp_path)
        stale = tmp_path / "agentproc-codex-999999-old"
        stale.mkdir()
        (stale / "auth.json").write_text('{"token":"leaked"}')
        old = time.time() - 7200
        os.utime(stale, (old, old))
        env = {"PATH": f"{cli.parent}:{os.environ['PATH']}",
               "TMPDIR": str(tmp_path)}
        r = _run_bridge_py(env, TURN)
        assert r.returncode == 0, r.stderr
        assert not stale.exists()

    def test_sigterm_cleans_up(self, fake_home, tmp_path):
        """Bridge receives SIGTERM while the (slow) CLI runs → temp home removed."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        cli = bin_dir / "codex"
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, time, sys\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 't1'}), flush=True)\n"
            "time.sleep(30)\n"
        )
        cli.chmod(0o755)
        env = {**os.environ,
               "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "TMPDIR": str(tmp_path)}
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE_PY)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        proc.stdin.write(json.dumps(TURN) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 10
        dirs = []
        while time.time() < deadline:
            dirs = _perm_dirs(tmp_path)
            if dirs:
                break
            time.sleep(0.1)
        assert dirs, "expected a permission temp dir to appear"
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("bridge did not exit on SIGTERM")
        time.sleep(0.5)
        assert _perm_dirs(tmp_path) == [], "SIGTERM should rmtree the permission home"


class TestJsBridgeEndToEnd:
    """Node parity: same observable behaviour as the Python side."""

    def _run_bridge_js(self, tmp_path, turn=TURN, timeout=30):
        return subprocess.run(
            ["node", str(BRIDGE_JS)],
            input=json.dumps(turn) + "\n",
            capture_output=True,
            text=True,
            env={**os.environ, "TMPDIR": str(tmp_path)},
            timeout=timeout,
        )

    def _fake_codex_cli(self, tmp_path: Path) -> Path:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        cli = bin_dir / "codex"
        cli.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"t1\"}'\n"
            "printf '%s\\n' '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"done\"}}'\n"
            "exit 0\n"
        )
        cli.chmod(0o755)
        return cli

    def test_no_residue_after_normal_exit(self, fake_home, tmp_path):
        cli = self._fake_codex_cli(tmp_path)
        env = {**os.environ,
               "PATH": f"{cli.parent}:{os.environ['PATH']}",
               "TMPDIR": str(tmp_path)}
        r = subprocess.run(
            ["node", str(BRIDGE_JS)], input=json.dumps(TURN) + "\n",
            capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stderr
        residue = _perm_dirs(tmp_path)
        assert residue == [], f"credential temp dirs left behind: {residue}"

    def test_startup_sweep_removes_leftover(self, fake_home, tmp_path):
        cli = self._fake_codex_cli(tmp_path)
        stale = tmp_path / "agentproc-codex-999999-old"
        stale.mkdir()
        (stale / "auth.json").write_text('{"token":"leaked"}')
        old = time.time() - 7200
        os.utime(stale, (old, old))
        env = {**os.environ,
               "PATH": f"{cli.parent}:{os.environ['PATH']}",
               "TMPDIR": str(tmp_path)}
        r = subprocess.run(
            ["node", str(BRIDGE_JS)], input=json.dumps(TURN) + "\n",
            capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stderr
        assert not stale.exists()

    def test_sigterm_cleans_up(self, fake_home, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        cli = bin_dir / "codex"
        cli.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"t1\"}'\n"
            "sleep 30\n"
        )
        cli.chmod(0o755)
        env = {**os.environ,
               "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "TMPDIR": str(tmp_path)}
        proc = subprocess.Popen(
            ["node", str(BRIDGE_JS)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        proc.stdin.write(json.dumps(TURN) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 10
        dirs = []
        while time.time() < deadline:
            dirs = _perm_dirs(tmp_path)
            if dirs:
                break
            time.sleep(0.1)
        assert dirs, "expected a permission temp dir to appear"
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("node bridge did not exit on SIGTERM")
        time.sleep(0.5)
        assert _perm_dirs(tmp_path) == [], "SIGTERM should rm the permission home"
