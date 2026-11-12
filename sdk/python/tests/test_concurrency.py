"""Issue #17 — concurrency gate, per-session serializer, run-lock generation
token, session-file append atomicity.

The gate tests patch ``agentproc.concurrency._gate`` with fresh instances
(the runner resolves ``_concurrency._gate`` at call time, so the patch
takes effect); the public-API test exercises ``configure_concurrency()``
against the real module gate and restores the default (unbounded) after.
"""
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agentproc import (
    ConcurrencyRejected,
    HistoryEntry,
    append_history,
    configure_concurrency,
    concurrency_snapshot,
    load_history,
)
from agentproc import concurrency as concurrency_mod
from agentproc import run_lock as run_lock_mod
from agentproc.runner import RunOptions, run


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def gate():
    """Fresh module-level gate per test (runner picks it up at call time)."""
    fresh = concurrency_mod._RunGate()
    old = concurrency_mod._gate
    concurrency_mod._gate = fresh
    yield fresh
    concurrency_mod._gate = old


@pytest.fixture
def agent_factory(tmp_path):
    def _make(sleep: float = 0.0, text: str = "done") -> str:
        f = tmp_path / f"agent-{time.monotonic_ns()}.sh"
        f.write_text(
            "#!/usr/bin/env bash\n"
            + (f"sleep {sleep}\n" if sleep else "")
            + "echo '" + json.dumps({"type": "result", "text": text}) + "'\n"
        )
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(f)
    return _make


def _run_agent(agent: str, session_id: str = ""):
    return run({"command": agent}, RunOptions(message="hi", session_id=session_id))


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _self_timing_agent(tmp_path, wrap: str = "") -> str:
    """A bash agent that logs its own flight window (`APTAG-start`/`-end`
    with wall timestamps) to $ORDER_LOG before/after emitting the result.
    ``wrap`` is extra body inserted between the markers (e.g. a sleep)."""
    f = tmp_path / f"self-timing-{time.monotonic_ns()}.sh"
    f.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$APTAG-start $(date +%s.%N)" >> "$ORDER_LOG"\n'
        + wrap
        + "\n"
        + "echo '" + json.dumps({"type": "result", "text": "done"}) + "'\n"
        'echo "$APTAG-end $(date +%s.%N)" >> "$ORDER_LOG"\n'
    )
    f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(f)


def _assert_windows_do_not_overlap(log: Path, timeout: float = 5.0) -> None:
    """All agents logged `<tag>-start <ts>` / `<tag>-end <ts>`; assert the
    two flight windows are disjoint (strict serialization happened)."""
    deadline = time.monotonic() + timeout
    marks = {}
    while time.monotonic() < deadline:
        marks = {}
        for ln in log.read_text(encoding="utf-8").splitlines():
            parts = ln.split()
            if len(parts) == 2 and parts[0].endswith(("-start", "-end")):
                tag, _, kind = parts[0].rpartition("-")
                marks.setdefault(tag, {})[kind] = float(parts[1])
        if len(marks) == 2 and all(len(v) == 2 for v in marks.values()):
            break
        time.sleep(0.02)
    assert len(marks) == 2 and all(len(v) == 2 for v in marks.values()), (
        f"both agents must have logged start+end, got {marks}")
    (s1, e1), (s2, e2) = (
        (m["start"], m["end"]) for m in marks.values()
    )
    first_end, second_start = (e1, s2) if s1 < s2 else (e2, s1)
    assert second_start >= first_end, (
        f"flight windows overlapped: {marks}"
    )


# ---------------------------------------------------------------------------
# global gate
# ---------------------------------------------------------------------------

class TestGlobalGate:
    def test_unbounded_by_default(self, gate, agent_factory):
        gate.configure()  # explicit default: max=None → unbounded
        agent = agent_factory(sleep=0.35)
        seen = {}
        t1 = threading.Thread(target=lambda: seen.setdefault("r1", _run_agent(agent)))
        t1.start()
        assert _wait_until(lambda: gate.snapshot()["active_runs"] == 1)
        t2 = threading.Thread(target=lambda: seen.setdefault("r2", _run_agent(agent)))
        t2.start()
        # both in flight at once — unbounded means no gate, no queue
        assert _wait_until(lambda: gate.snapshot()["active_runs"] == 2)
        t1.join(10); t2.join(10)
        assert seen["r1"].reply == "done" and seen["r2"].reply == "done"
        assert gate.snapshot()["active_runs"] == 0

    def test_reject_when_full(self, gate, agent_factory):
        gate.configure(max_concurrent_runs=1, overflow="reject")
        agent = agent_factory(sleep=0.35)
        box = {}
        t1 = threading.Thread(target=lambda: box.setdefault("r1", _run_agent(agent)))
        t1.start()
        assert _wait_until(lambda: gate.snapshot()["active_runs"] == 1)
        with pytest.raises(ConcurrencyRejected) as excinfo:
            _run_agent(agent)
        assert excinfo.value.max_concurrent_runs == 1
        t1.join(10)
        assert box["r1"].reply == "done"
        # the rejected run must not have leaked a slot
        assert gate.snapshot()["active_runs"] == 0

    def test_wait_serializes_when_full(self, gate, agent_factory, tmp_path):
        gate.configure(max_concurrent_runs=1, overflow="wait")
        log = tmp_path / "order.log"
        # the agent processes themselves report their real flight windows —
        # a thread entering the worker is NOT the same as its run() acquiring
        # the gate slot, so gate-level timing must come from the children
        agent = _self_timing_agent(tmp_path, "sleep 0.3")

        boxes = {}

        def worker(tag):
            boxes[tag] = run(
                {"command": agent},
                RunOptions(message="hi", extra_env={"APTAG": tag, "ORDER_LOG": str(log)}),
            )

        t1 = threading.Thread(target=worker, args=("a",))
        t1.start()
        assert _wait_until(lambda: gate.snapshot()["active_runs"] == 1)
        t2 = threading.Thread(target=worker, args=("b",))
        t2.start()
        t1.join(15); t2.join(15)
        assert boxes["a"].reply == "done" and boxes["b"].reply == "done"
        _assert_windows_do_not_overlap(log)

    def test_wait_frees_slot_after_release(self, gate, agent_factory):
        gate.configure(max_concurrent_runs=1, overflow="wait")
        agent = agent_factory(sleep=0.0)
        # three sequential runs through a one-slot gate all succeed
        for _ in range(3):
            assert _run_agent(agent).reply == "done"
        assert gate.snapshot()["active_runs"] == 0


# ---------------------------------------------------------------------------
# per-session serializer
# ---------------------------------------------------------------------------

class TestSessionSerializer:
    def test_same_session_serializes(self, gate, agent_factory, tmp_path):
        gate.configure(serialize_same_session=True, max_concurrent_runs=4)
        log = tmp_path / "order.log"
        agent = _self_timing_agent(tmp_path, "sleep 0.3")

        boxes = {}

        def worker(tag):
            boxes[tag] = run(
                {"command": agent},
                RunOptions(
                    message="hi",
                    session_id="chat-1",
                    extra_env={"APTAG": tag, "ORDER_LOG": str(log)},
                ),
            )

        t1 = threading.Thread(target=worker, args=("a",))
        t1.start()
        assert _wait_until(lambda: gate.snapshot()["active_runs"] == 1)
        t2 = threading.Thread(target=worker, args=("b",))
        t2.start()
        t1.join(15); t2.join(15)
        assert boxes["a"].reply == "done" and boxes["b"].reply == "done"
        # same session id → the two turns must not overlap on the wire
        _assert_windows_do_not_overlap(log)

    def test_different_sessions_parallel(self, gate, agent_factory):
        gate.configure(serialize_same_session=True, max_concurrent_runs=4)
        agent = agent_factory(sleep=0.4)
        boxes = {}

        def mk(sid):
            def go():
                boxes[sid] = _run_agent(agent, session_id=sid)
            return go

        threads = [threading.Thread(target=mk(sid)) for sid in ("chat-1", "chat-2")]
        for t in threads:
            t.start()
        assert _wait_until(
            lambda: concurrency_snapshot()["active_runs"] == 2
        ), "different sessions must run in parallel even with serialization on"
        for t in threads:
            t.join(10)
        assert boxes["chat-1"].reply == "done" and boxes["chat-2"].reply == "done"

    def test_reject_releases_session_guard(self, gate, agent_factory):
        gate.configure(max_concurrent_runs=1, overflow="reject", serialize_same_session=True)
        agent = agent_factory(sleep=0.35)
        box = {}
        t1 = threading.Thread(
            target=lambda: box.setdefault("r1", _run_agent(agent, session_id="s1"))
        )
        t1.start()
        assert _wait_until(lambda: gate.snapshot()["active_runs"] == 1)
        # s2 is a different session: it acquires its guard, then hits the full
        # global gate → rejected; the guard must be released on the way out
        with pytest.raises(ConcurrencyRejected):
            _run_agent(agent, session_id="s2")
        t1.join(10)
        # the released guard did not wedge the session table
        assert gate.snapshot()["sessions_in_flight"] == 0
        box.setdefault("r2", _run_agent(agent, session_id="s2"))
        assert box["r2"].reply == "done"


# ---------------------------------------------------------------------------
# configuration surface
# ---------------------------------------------------------------------------

class TestConfig:
    def test_env_bootstrap(self, monkeypatch):
        fresh = concurrency_mod._RunGate()
        monkeypatch.setenv(concurrency_mod.ENV_MAX_CONCURRENT, "1")
        monkeypatch.setenv(concurrency_mod.ENV_OVERFLOW, "reject")
        monkeypatch.setenv(concurrency_mod.ENV_SERIALIZE_SESSION, "1")
        t = fresh.acquire("s1")
        with pytest.raises(ConcurrencyRejected):
            fresh.acquire("s2")
        t.release()
        snap = fresh.snapshot()
        assert snap["max_concurrent_runs"] == 1
        assert snap["overflow"] == "reject"
        assert snap["serialize_same_session"] is True

    def test_env_garbage_stays_unbounded(self, monkeypatch):
        fresh = concurrency_mod._RunGate()
        monkeypatch.setenv(concurrency_mod.ENV_MAX_CONCURRENT, "not-a-number")
        tok = fresh.acquire("s1")
        tok2 = fresh.acquire("s2")  # unbounded: second acquire is fine
        tok.release(); tok2.release()
        assert fresh.snapshot()["max_concurrent_runs"] is None

    def test_configure_validation(self):
        fresh = concurrency_mod._RunGate()
        with pytest.raises(ValueError):
            fresh.configure(overflow="explode")
        with pytest.raises(ValueError):
            fresh.configure(max_concurrent_runs=0)
        with pytest.raises(ValueError):
            fresh.configure(max_concurrent_runs=-3)

    def test_public_api_end_to_end(self, monkeypatch, agent_factory):
        """The documented surface: configure_concurrency() → run() respects it."""
        agent = agent_factory(sleep=0.3)
        configure_concurrency(max_concurrent_runs=1, overflow="reject")
        try:
            box = {}
            t1 = threading.Thread(
                target=lambda: box.setdefault("r1", _run_agent(agent))
            )
            t1.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and concurrency_snapshot()["active_runs"] < 1:
                time.sleep(0.02)
            with pytest.raises(ConcurrencyRejected):
                _run_agent(agent)
            t1.join(10)
            assert box["r1"].reply == "done"
        finally:
            configure_concurrency(max_concurrent_runs=None, overflow="wait", serialize_same_session=False)
        assert concurrency_snapshot()["max_concurrent_runs"] is None

    def test_runner_wiring_runs_through_gate(self, gate, agent_factory):
        """run() must pass through the gate even for plain spawn profiles."""
        agent = agent_factory()
        gate.configure(max_concurrent_runs=1, overflow="reject")
        assert _run_agent(agent).reply == "done"
        # gate still busy-guarding: a *held* token (not a run) blocks the next
        tok = gate.acquire("x")
        with pytest.raises(ConcurrencyRejected):
            _run_agent(agent)
        tok.release()
        assert _run_agent(agent).reply == "done"


# ---------------------------------------------------------------------------
# run lock — generation token (owner) semantics
# ---------------------------------------------------------------------------

class TestRunLockOwner:
    def test_record_carries_owner(self, tmp_path, monkeypatch):
        monkeypatch.setenv(run_lock_mod.LOCK_DIR_ENV, str(tmp_path))
        path = run_lock_mod.write_run_lock(str(tmp_path / "ws"), 999999, ["sleep"])
        assert path is not None
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record.get("owner")
        run_lock_mod.clear_run_lock(str(tmp_path / "ws"))

    def test_clear_refuses_while_recorded_pid_alive(self, tmp_path, monkeypatch):
        monkeypatch.setenv(run_lock_mod.LOCK_DIR_ENV, str(tmp_path))
        proc = subprocess.Popen(["sleep", "5"])
        try:
            key = str(tmp_path / "ws")
            run_lock_mod.write_run_lock(key, proc.pid, ["sleep", "5"])
            # simulated "first finisher" clear while the second run's agent
            # (this live pid) is still going → must NOT remove the lock
            assert run_lock_mod.clear_run_lock(key) is False
            assert run_lock_mod.lock_path_for(key).exists()
        finally:
            proc.kill()
            proc.wait()
        # once the recorded pid is gone the same clear succeeds (self-heal)
        assert run_lock_mod.clear_run_lock(key) is True
        assert not run_lock_mod.lock_path_for(key).exists()

    def test_clear_after_pid_gone(self, tmp_path, monkeypatch):
        monkeypatch.setenv(run_lock_mod.LOCK_DIR_ENV, str(tmp_path))
        key = str(tmp_path / "ws")
        run_lock_mod.write_run_lock(key, 999999999, ["sleep"])  # pid cannot exist
        assert run_lock_mod.clear_run_lock(key) is True

    def test_legacy_record_without_owner_still_cleared(self, tmp_path, monkeypatch):
        monkeypatch.setenv(run_lock_mod.LOCK_DIR_ENV, str(tmp_path))
        key = str(tmp_path / "ws")
        path = run_lock_mod.lock_path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "pid": 999999999, "command": "sleep"}))
        assert run_lock_mod.clear_run_lock(key) is True

    def test_runner_clears_own_lock_after_turn(self, tmp_path, monkeypatch, agent_factory):
        monkeypatch.setenv(run_lock_mod.LOCK_DIR_ENV, str(tmp_path))
        key = str(tmp_path / "ws")
        agent = agent_factory()
        assert run(
            {"command": agent},
            RunOptions(message="hi", run_lock_key=key),
        ).reply == "done"
        assert not run_lock_mod.lock_path_for(key).exists()


# ---------------------------------------------------------------------------
# session history — append atomicity
# ---------------------------------------------------------------------------

class TestAppendHistoryAtomicity:
    def test_roundtrip(self, tmp_path):
        sid = "rt-1"
        append_history(sid, [
            HistoryEntry(role="user", content="你好"),
            HistoryEntry(role="assistant", content="hello"),
        ], base_dir=str(tmp_path))
        entries = load_history(sid, base_dir=str(tmp_path))
        assert [e.content for e in entries] == ["你好", "hello"]

    def test_concurrent_appends_produce_valid_jsonl(self, tmp_path):
        sid = "burst"
        base = str(tmp_path)
        n_threads, n_batches, per_batch = 4, 20, 3
        errors = []

        def worker():
            try:
                for _ in range(n_batches):
                    append_history(sid, [
                        HistoryEntry(role="user", content="x" * 200, timestamp="t"),
                        HistoryEntry(role="assistant", content="y" * 200, timestamp="t"),
                        HistoryEntry(role="user", content="z" * 200, timestamp="t"),
                    ], base_dir=base)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors
        raw = (Path(base) / f"{sid}.jsonl").read_text(encoding="utf-8")
        lines = [ln for ln in raw.splitlines() if ln]
        assert len(lines) == n_threads * n_batches * per_batch
        for ln in lines:
            d = json.loads(ln)  # every line must be intact JSON — no tearing
            assert d["role"] in ("user", "assistant")
