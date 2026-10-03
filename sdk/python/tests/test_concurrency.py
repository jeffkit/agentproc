"""issue #17：并发语义——run lock 竞态 / per-session 串行化 / 全局并发闸。

吸收原 `test_wip_concurrent_run_lock.py` 的两条失败复现用例（世代化后通过）。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from agentproc import run_lock
from agentproc.concurrency import (
    CONCURRENCY_LIMIT_MARKER,
    ConcurrencyGate,
    ConcurrencyLimitError,
    SessionSerializer,
)


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(run_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    return tmp_path / "locks"


# ---------------------------------------------------------------------------
# run lock 竞态（原 wip 复现用例修正断言后并入）
# ---------------------------------------------------------------------------


class TestRunLockGeneration:
    def test_second_write_gets_new_generation(self):
        _p1, gen1 = run_lock.write_run_lock("/ws/a", 111, ["/usr/bin/env", "recursive"])
        _p2, gen2 = run_lock.write_run_lock("/ws/a", 222, ["/usr/bin/env", "recursive"])
        assert gen1 == 1 and gen2 == 2
        record = json.loads(run_lock.lock_path_for("/ws/a").read_text())
        assert record["generation"] == 2  # 当前持有者是后来者
        assert record["pid"] == 222

    def test_clear_by_first_finisher_does_not_clear_running_second(self):
        _pa, gen_a = run_lock.write_run_lock("/ws/a", 111, ["/usr/bin/env", "recursive"])
        _pb, gen_b = run_lock.write_run_lock("/ws/a", 222, ["/usr/bin/env", "recursive"])
        assert gen_a != gen_b
        run_lock.clear_run_lock("/ws/a", gen_a)  # A 的收尾：不得清 B 的锁
        record_path = run_lock.lock_path_for("/ws/a")
        assert record_path.exists()
        record = json.loads(record_path.read_text())
        assert record["pid"] == 222
        # B 正常收尾后清掉自己世代
        run_lock.clear_run_lock("/ws/a", gen_b)
        assert not record_path.exists()

    def test_inprocess_mutex_exists(self):
        attrs = [a for a in dir(run_lock) if "lock" in a.lower() or "mutex" in a.lower()]
        assert any(
            a for a in attrs
            if a not in ("write_run_lock", "clear_run_lock", "RunLockBusy", "LOCK_DIR_ENV")
        ), f"no in-process mutex primitive found in agentproc.run_lock (attrs: {attrs})"

    def test_generation_monotonic_under_threads(self):
        gens = []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            _p, gen = run_lock.write_run_lock("/ws/t", 1000 + i, ["/usr/bin/env", "x"])
            gens.append(gen)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(gens) == list(range(1, 9))  # 无重复、单调

    def test_interleaved_write_clear_no_misfire(self):
        """双线程同 key 交错 write/clear：每个 clear 只清自己世代。"""
        _p, gen1 = run_lock.write_run_lock("/ws/i", 111, ["/usr/bin/env", "x"])
        _p, gen2 = run_lock.write_run_lock("/ws/i", 222, ["/usr/bin/env", "x"])
        # 线程 A（gen1，先结束）clear；线程 B（gen2，仍在跑）后 clear
        run_lock.clear_run_lock("/ws/i", gen1)
        assert run_lock.lock_path_for("/ws/i").exists()
        run_lock.clear_run_lock("/ws/i", gen2)
        assert not run_lock.lock_path_for("/ws/i").exists()


# ---------------------------------------------------------------------------
# SessionSerializer / ConcurrencyGate 单元
# ---------------------------------------------------------------------------


class TestPrimitives:
    def test_serializer_same_key_serializes(self):
        ser = SessionSerializer()
        order = []
        lock = threading.Lock()

        def work(name):
            with ser.serialize("s1"):
                with lock:
                    order.append(("start", name))
                time.sleep(0.05)
                with lock:
                    order.append(("end", name))

        threads = [threading.Thread(target=work, args=(n,)) for n in "AB"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 区间不相交：每个 start 后紧跟同名 end
        assert order in (
            [("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")],
            [("start", "B"), ("end", "B"), ("start", "A"), ("end", "A")],
        )

    def test_serializer_none_key_is_noop(self):
        ser = SessionSerializer()
        with ser.serialize(None):
            pass  # 不阻塞

    def test_gate_reject(self):
        gate = ConcurrencyGate(1, "reject")
        gate.acquire()
        with pytest.raises(ConcurrencyLimitError) as exc:
            gate.acquire()
        assert CONCURRENCY_LIMIT_MARKER in str(exc.value)
        gate.release()
        gate.acquire()  # 空位释放后可再入

    def test_gate_queue_serializes(self):
        gate = ConcurrencyGate(1, "queue")
        active = []
        peak = []
        lock = threading.Lock()

        def work():
            with gate.slot():
                with lock:
                    active.append(1)
                    peak.append(len(active))
                time.sleep(0.05)
                with lock:
                    active.pop()

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert max(peak) == 1

    def test_gate_unlimited(self):
        gate = ConcurrencyGate(None)
        for _ in range(10):
            gate.acquire()
        for _ in range(10):
            gate.release()

    def test_gate_validation(self):
        with pytest.raises(ValueError):
            ConcurrencyGate(1, "explode")
        with pytest.raises(ValueError):
            ConcurrencyGate(0)


# ---------------------------------------------------------------------------
# runner 集成：串行化 / 并发闸（executor 路径，无需真实 CLI）
# ---------------------------------------------------------------------------


def _echo_profile():
    return {"command": "python3", "args": ["-c", "print('{\"type\":\"result\",\"text\":\"ok\"}')"]}


class TestRunnerConcurrency:
    def test_same_session_key_serialized(self):
        from agentproc.runner import RunOptions, run

        lock = threading.Lock()
        # 带 sleep 的 agent 以拉开执行区间，便于探测重叠
        profile = {"command": "python3", "args": [
            "-c",
            "import time,json;"
            "print(json.dumps({'type':'partial','text':'x'}),flush=True);"
            "time.sleep(0.3);"
            "print(json.dumps({'type':'result','text':'ok'}),flush=True)",
        ]}
        starts = []
        ends = []

        def w(i):
            with lock:
                starts.append(time.time())
            run(profile, RunOptions(message="hi", session_key="sess-1", timeout_secs=30))
            with lock:
                ends.append(time.time())

        threads = [threading.Thread(target=w, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 串行：两个各睡 0.3s 的 run 总耗时 ≥ 0.55s（并发重叠则 ≈0.35s）
        total = max(e for e in ends) - min(s for s in starts)
        assert total >= 0.55, f"runs overlapped: total={total:.3f}s starts={starts} ends={ends}"
        assert not any(isinstance(e, str) for e in ends)

    def test_reject_returns_error_marker(self):
        from agentproc.runner import RunOptions, run

        slow_profile = {"command": "python3", "args": [
            "-c",
            "import time,json;time.sleep(0.6);"
            "print(json.dumps({'type':'result','text':'done'}),flush=True)",
        ]}

        results = [None, None]

        def w(i, prof):
            results[i] = run(prof, RunOptions(
                message="m", max_concurrent=1, on_saturated="reject", timeout_secs=30))

        t1 = threading.Thread(target=w, args=(0, slow_profile))
        t1.start()
        time.sleep(0.2)  # 让第一个占住闸
        t2 = threading.Thread(target=w, args=(1, _echo_profile()))
        t2.start()
        t1.join()
        t2.join()
        assert not results[0].error
        assert results[1].error and CONCURRENCY_LIMIT_MARKER in results[1].error, (
            repr(results[1]))

    def test_default_no_limits(self):
        from agentproc.runner import RunOptions, run

        results = [None] * 4

        def w(i):
            results[i] = run(_echo_profile(), RunOptions(message="m", timeout_secs=30))

        threads = [threading.Thread(target=w, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(not r.error for r in results)
