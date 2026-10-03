"""run_lock：遗言锁写入/清理 + kill-before-start 孤儿清场（keeper 迁移 G3）。"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from agentproc import run_lock
from agentproc.runner import RunOptions, run


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(run_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    return tmp_path / "locks"


def _spawn_session_leader(secs=60):
    """真实会话领袖（start_new_session）：pid==pgid，killpg 才有的打。"""
    proc = subprocess.Popen(["sleep", str(secs)], start_new_session=True)
    return proc


class TestWriteClear:
    def test_roundtrip(self, lock_dir):
        path, generation = run_lock.write_run_lock("/ws/a", 4242, ["/usr/bin/env", "recursive", "run"])
        assert path is not None and path.exists()
        record = json.loads(path.read_text())
        assert record["pid"] == 4242
        assert record["command"] == "env"
        assert record["key"] == os.path.abspath("/ws/a")
        run_lock.clear_run_lock("/ws/a")
        assert not path.exists()

    def test_lock_path_keyed_by_abs_path(self):
        assert (run_lock.lock_path_for("/ws/a") == run_lock.lock_path_for("/ws/a/"))
        assert run_lock.lock_path_for("/ws/a") != run_lock.lock_path_for("/ws/b")

    def test_write_failure_is_best_effort(self, monkeypatch):
        monkeypatch.setenv(run_lock.LOCK_DIR_ENV, "/proc/no/such/dir")
        assert run_lock.write_run_lock("/ws/x", 1, ["sleep"]) is None


class TestCleanupStaleRun:
    def test_clean_without_lock(self):
        assert run_lock.cleanup_stale_run("/ws/none")["action"] == "clean"

    def test_stale_when_pid_gone(self, lock_dir):
        run_lock.write_run_lock("/ws/a", 999999999, ["sleep"])
        info = run_lock.cleanup_stale_run("/ws/a")
        assert info["action"] == "stale"
        assert info["reason"] == "pid-gone"
        assert not run_lock.lock_path_for("/ws/a").exists()

    def test_stale_when_corrupt(self, lock_dir):
        path = run_lock.lock_path_for("/ws/a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{")
        assert run_lock.cleanup_stale_run("/ws/a")["action"] == "stale"
        assert not path.exists()

    def test_kills_live_orphan(self, lock_dir):
        proc = _spawn_session_leader()
        try:
            run_lock.write_run_lock("/ws/a", proc.pid, ["sleep", "60"])
            info = run_lock.cleanup_stale_run("/ws/a")
            assert info["action"] == "killed"
            assert info["pid"] == proc.pid
            assert not run_lock.lock_path_for("/ws/a").exists()
        finally:
            proc.wait(timeout=15)

    def test_pid_reuse_guard_does_not_kill(self, lock_dir):
        """存活 pid 但命令与遗言不符（= pid 重用/非本机制的进程）→ stale，不杀。"""
        proc = _spawn_session_leader()
        try:
            run_lock.write_run_lock("/ws/a", proc.pid, ["/opt/definitely-other-bin", "run"])
            info = run_lock.cleanup_stale_run("/ws/a")
            assert info["action"] == "stale"
            assert info["reason"] == "pid-reused"
            assert proc.poll() is None  # 活着未被误杀
        finally:
            proc.kill()
            proc.wait(timeout=15)

    def test_busy_when_identity_unverifiable(self, lock_dir, monkeypatch):
        proc = _spawn_session_leader()
        try:
            run_lock.write_run_lock("/ws/a", proc.pid, ["sleep", "60"])
            monkeypatch.setattr(run_lock, "_ps_fields", lambda pid: None)
            with pytest.raises(run_lock.RunLockBusy):
                run_lock.cleanup_stale_run("/ws/a")
            # 存疑不删锁：下次探测还能重试
            assert run_lock.lock_path_for("/ws/a").exists()
        finally:
            proc.kill()
            proc.wait(timeout=15)

    def test_unsupported_platform_is_noop(self, monkeypatch):
        """无 killpg 的平台（Windows）：探测放行不阻塞主流程。"""
        monkeypatch.delattr(run_lock.os, "killpg")
        assert run_lock.cleanup_stale_run("/ws/a")["action"] == "unsupported"


class TestRunnerIntegration:
    @staticmethod
    def _register_plain_executor(name, argv):
        from agentproc import EXECUTORS

        def _make():
            def build_args(message, session_id, env):
                return list(argv)
            return {"build_args": build_args}

        EXECUTORS[name] = {"cli_name": argv[0], "plain": True, "make_handlers": _make}

    def test_success_clears_lock(self, lock_dir):
        self._register_plain_executor("test-lock-ok", ["sh", "-c", "echo lock-ok"])
        key = "/ws/integ"
        result = run({"executor": "test-lock-ok"},
                     RunOptions(message="hi", run_lock_key=key))
        assert result.error == ""
        assert not run_lock.lock_path_for(key).exists()

    def test_timeout_leak_self_heals(self, lock_dir):
        """超时路径不显式清锁（自愈设计）：残留锁 pid 已死，preflight 判 stale。"""
        self._register_plain_executor("test-lock-slow", ["sleep", "30"])
        key = "/ws/slow"
        result = run({"executor": "test-lock-slow"},
                     RunOptions(message="hi", run_lock_key=key, timeout_secs=1))
        assert result.exit_code == 124  # plain 路径超时以退出码表达
        path = run_lock.lock_path_for(key)
        assert path.exists()  # 泄漏的遗言
        info = run_lock.cleanup_stale_run(key)
        assert info["action"] == "stale"  # pid 已死 → 自愈清理
        assert not path.exists()
