"""run_lock：遗言锁写入/清理 + kill-before-start 孤儿清场（keeper 迁移 G3）。

spawner 维度（issue #9）：锁记录带「派这个 agent 的 worker pid」，接管侧只有在
它已死时才 killpg；它仍存活 → ``RunLockBusy``（不杀不删）。0.18.3 之前的旧记录
没有该字段 → 一律按不可核实处理（``RunLockBusy``），规则见 ``run_lock`` docstring。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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


def _write_raw(key, **fields):
    """直接落盘锁记录：测试要控制 ``spawner`` 字段的有无与取值。"""
    path = run_lock.lock_path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"key": os.path.abspath(key), "started_at": "2026-01-01T00:00:00"}
    record.update(fields)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _reaped_pid():
    """已回收的短命进程 pid —— 确定已死但形状真实（非 999999999 哨兵）。"""
    proc = subprocess.Popen(["sleep", "0.01"])
    proc.wait(timeout=15)
    return proc.pid


def _ppid_of(pid):
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                         capture_output=True, text=True, encoding="utf-8")
    text = (out.stdout or "").strip()
    return int(text) if out.returncode == 0 and text else None


def _spawn_reparented_session_leader(secs=60):
    """派一个 ``start_new_session`` 的 sleep 后立刻退出：sleep 成为会话领袖
    并被 init/launchd 收养（ppid==1）——即「无 spawner 记录时的可判孤儿态」。"""
    launcher = subprocess.run(
        [sys.executable, "-c",
         "import subprocess,sys\n"
         "p = subprocess.Popen(['sleep', sys.argv[1]], start_new_session=True,\n"
         "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
         "                     stderr=subprocess.DEVNULL)\n"
         "print(p.pid, flush=True)",
         str(secs)],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert launcher.returncode == 0, launcher.stderr
    pid = int((launcher.stdout or "").strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _ppid_of(pid) == 1:
            return pid
        time.sleep(0.1)
    os.kill(pid, 9)
    pytest.skip("进程未被 reparent 到 pid 1（平台差异）")


class TestWriteClear:
    def test_roundtrip(self, lock_dir):
        path = run_lock.write_run_lock("/ws/a", 4242, ["/usr/bin/env", "recursive", "run"])
        assert path is not None and path.exists()
        record = json.loads(path.read_text())
        assert record["pid"] == 4242
        assert record["command"] == "env"
        assert record["key"] == os.path.abspath("/ws/a")
        run_lock.clear_run_lock("/ws/a")
        assert not path.exists()

    def test_records_spawner_pid(self, lock_dir):
        """spawner = 调用方进程（它就是 Popen 掉 agent 的那一个）。"""
        path = run_lock.write_run_lock("/ws/a", 4242, ["/usr/bin/env", "recursive", "run"])
        record = json.loads(path.read_text())
        assert record.get("spawner") == os.getpid()
        assert run_lock._pid_alive(record["spawner"])

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
            run_lock.write_run_lock("/ws/a", proc.pid, ["sleep", "60"],
                                    spawner=_reaped_pid())
            info = run_lock.cleanup_stale_run("/ws/a")
            assert info["action"] == "killed"
            assert info["pid"] == proc.pid
            assert not run_lock.lock_path_for("/ws/a").exists()
        finally:
            proc.wait(timeout=15)

    def test_live_spawner_is_not_killed(self, lock_dir):
        """worker 仍活着 → RunLockBusy，且不 killpg、不删锁（issue #9 的核心诉求）。"""
        proc = _spawn_session_leader()
        try:
            path = _write_raw("/ws/a", pid=proc.pid, command="sleep",
                              spawner=os.getpid())
            with pytest.raises(run_lock.RunLockBusy):
                run_lock.cleanup_stale_run("/ws/a")
            assert proc.poll() is None      # 健康 agent 未被误杀
            assert path.exists()            # 存疑不删锁：下次探测还能重试
        finally:
            proc.kill()
            proc.wait(timeout=15)

    def test_dead_spawner_kills_orphan(self, lock_dir):
        """spawner 已死 → 真孤儿，killpg 放行，且 reason 区分该分支。"""
        proc = _spawn_session_leader()
        try:
            path = _write_raw("/ws/a", pid=proc.pid, command="sleep",
                              spawner=_reaped_pid())
            info = run_lock.cleanup_stale_run("/ws/a")
            assert info["action"] == "killed"
            assert info["pid"] == proc.pid
            assert "spawner" in str(info.get("reason", "")).lower()
            assert not path.exists()
        finally:
            proc.kill()
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


class TestLegacyRecordWithoutSpawner:
    """旧格式锁（无 spawner）：不得盲杀。可判孤儿态的实现也须记录其判定规则。"""

    def test_live_pid_without_spawner_is_not_blindly_killed(self, lock_dir):
        proc = _spawn_session_leader()
        try:
            path = _write_raw("/ws/legacy", pid=proc.pid, command="sleep")
            info = None
            try:
                info = run_lock.cleanup_stale_run("/ws/legacy")
            except run_lock.RunLockBusy:
                pass  # 允许：不可核实 → fail-safe
            assert proc.poll() is None
            if info is not None:
                assert info["action"] != "killed"
                assert info.get("reason") in ("corrupt", "pid-reused") or not info.get("reason")
            assert path.exists() or info is not None
        finally:
            proc.kill()
            proc.wait(timeout=15)

    def test_legacy_orphan_state_has_no_third_outcome(self, lock_dir):
        """旧格式 + 进程确已孤儿化（被 init/launchd 收养，ppid==1）：
        实现可判 stale/可核实为孤儿后 killpg，也可按不可核实抛 RunLockBusy——
        两者都可接受，但不允许「未核实就杀」的中间态。"""
        pid = _spawn_reparented_session_leader()
        _write_raw("/ws/legacy-orphan", pid=pid, command="sleep")
        try:
            try:
                info = run_lock.cleanup_stale_run("/ws/legacy-orphan")
            except run_lock.RunLockBusy:
                assert run_lock._pid_alive(pid)   # 存疑必须留活口
            else:
                assert info["action"] in ("killed", "stale")
        finally:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


class TestRunnerIntegration:
    @staticmethod
    def _register_plain_executor(name, argv):
        from agentproc import EXECUTORS

        def _make():
            def build_args(message, session_id, env, ctx):
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
