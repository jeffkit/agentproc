"""Process-tree cleanup on the runner's interrupt path (issue #8).

Ctrl-C must clear the agent's whole process group — a surviving grandchild
keeps the agent's credentials and its workspace write access — and it must do
so *before* the per-workspace run lock is cleared, otherwise the orphan becomes
invisible to the next turn's stale-run sweep.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import agentproc.runner as runner_mod
from agentproc.runner import RunOptions, run


def _quick_agent(tmp_path: Path) -> Path:
    script = tmp_path / "quick_agent.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    return script


def _interrupt_on_first_wait(monkeypatch) -> dict:
    """Make the runner's first ``Popen.wait()`` raise ``KeyboardInterrupt``."""
    state = {"raised": False}
    real_wait = subprocess.Popen.wait

    def wait(self, timeout=None):
        if not state["raised"]:
            state["raised"] = True
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    return state


def test_ctrl_c_signals_the_whole_process_group(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner_mod,
        "_signal_process_group",
        lambda proc, sig: calls.append((proc.pid, sig)),
    )
    state = _interrupt_on_first_wait(monkeypatch)

    run(
        {"command": str(_quick_agent(tmp_path)), "timeout_secs": 30},
        RunOptions(message="hi"),
    )

    assert state["raised"], "the injected interrupt never reached the runner"
    assert [sig for _, sig in calls] == [signal.SIGINT], (
        f"interrupt path must signal the group with SIGINT; got {calls!r}"
    )


def test_interrupt_kills_the_group_before_clearing_the_run_lock(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        runner_mod, "_signal_process_group", lambda proc, sig: order.append("kill")
    )
    monkeypatch.setattr(
        runner_mod._run_lock, "write_run_lock", lambda *a, **kw: order.append("write")
    )
    monkeypatch.setattr(
        runner_mod._run_lock, "clear_run_lock", lambda key: order.append("clear")
    )
    _interrupt_on_first_wait(monkeypatch)

    run(
        {"command": str(_quick_agent(tmp_path)), "timeout_secs": 30},
        RunOptions(message="hi", run_lock_key=str(tmp_path / "lock")),
    )

    assert order == ["write", "kill", "clear"], (
        f"the lock must be cleared only after the group kill; got {order!r}"
    )


class _FakeProc:
    pid = 4242

    def __init__(self):
        self.sent = []

    def send_signal(self, sig):
        self.sent.append(sig)


def test_signal_process_group_falls_back_without_killpg(monkeypatch):
    """No ``os.killpg`` (Windows): fall back to the direct child instead of
    raising ``AttributeError`` out of the interrupt path."""
    monkeypatch.delattr(os, "killpg")
    proc = _FakeProc()

    runner_mod._signal_process_group(proc, signal.SIGTERM)

    assert proc.sent == [signal.SIGTERM]
