"""[wip] issue #20 — runner.py long-lived-consumer leaks (repro tests, expected to FAIL on baseline 840392d).

Covers three defects reported in the issue:
  1. daemon drain threads + pipe read fds survive run() return when a
     grandchild inherits stdout/stderr (no EOF) → per-turn leak.
  2. late on_partial/on_session/on_error can fire AFTER run() returned
     (no turn epoch/generation guard).
  3. _kill_process_group leaves a zombie when communicate(timeout=5)
     expires (no reap / no structured warning).

Fix must land in sdk/python/src/agentproc/runner.py only (internal
behaviour, not a spec change). Remove the [wip] prefix when green.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentproc.runner import (
    RunOptions,
    _kill_process_group,
    run,
)

HERE = Path(__file__).resolve().parent

# Agent whose direct child (grandchild of the runner) inherits the
# stdout/stderr pipe write-ends and keeps them open — drain threads never
# see EOF after the agent itself exits.
_LEAKY_AGENT = r"""
import json, subprocess, sys, time
gc = subprocess.Popen(
    [sys.executable, "-c",
     "import time,sys; time.sleep(0.5);"
     "sys.stdout.write('{\"type\":\"partial\",\"text\":\"LATE\"}\\n');"
     "sys.stdout.flush(); time.sleep(30)"],
)
print(json.dumps({"type": "result", "text": "done"}), flush=True)
sys.exit(0)
"""


def _leaky_profile(tmp_path):
    script = tmp_path / "leaky_agent.py"
    script.write_text(_LEAKY_AGENT)
    return {
        "command": sys.executable,
        "args": [str(script)],
        "timeout_secs": 20,
        "kill_grace_secs": 2,
    }


def _fd_count():
    # Linux only; macOS has no /proc — the test skips there.
    d = "/proc/self/fd"
    if not os.path.isdir(d):
        pytest.skip("no /proc/self/fd on this platform")
    return len(os.listdir(d))


def test_1_no_pipe_fd_or_thread_leak_after_run_returns(tmp_path):
    """run() must not leave the pipe read-ends (and their drain threads)
    open after returning when the drain threads never saw EOF."""
    base = _fd_count()
    threads_before = threading.active_count()

    late_partials = []
    result = run(
        _leaky_profile(tmp_path),
        RunOptions(message="hi", on_partial=late_partials.append),
    )
    assert result.exit_code == 0

    time.sleep(0.3)  # let any post-return event land, if it would
    # The grandchild is still alive holding the write ends; the runner must
    # nonetheless have closed its own read ends before returning.
    assert _fd_count() <= base, (
        f"pipe read fds leaked across run(): {base} -> {_fd_count()}"
    )
    assert threading.active_count() <= threads_before + 1, (
        "drain threads survived run() return"
    )
    # cleanup: kill the grandchild (it is in our process tree via the agent's
    # start_new_session group, so kill our leftover sleepers best-effort)
    subprocess.run(["pkill", "-f", "time.sleep(30)"], check=False)


def test_2_late_callbacks_dropped_after_run_returns(tmp_path):
    """A partial emitted by a grandchild AFTER run() returned must be
    dropped, not forwarded to on_partial (turn epoch guard)."""
    late_partials = []
    result = run(
        _leaky_profile(tmp_path),
        RunOptions(message="hi", on_partial=late_partials.append),
    )
    assert result.exit_code == 0
    assert result.reply == "done"
    time.sleep(1.2)  # the grandchild writes its LATE partial at ~0.5s
    assert late_partials == [], (
        f"late on_partial fired after run() returned: {late_partials!r}"
    )
    subprocess.run(["pkill", "-f", "time.sleep(30)"], check=False)


def test_3_kill_process_group_reaps_zombie(tmp_path, monkeypatch):
    """_kill_process_group must reap the child even when its communicate()
    backstop times out — the child must not be left as a zombie with
    returncode unset."""
    # child that dies immediately; communicate is forced to time out as if
    # a grandchild were still holding the pipes.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os, sys; os.kill(os.getpid(), signal.SIGKILL)"]
        if False else
        [sys.executable, "-c", "pass"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    time.sleep(0.2)  # ensure the child has exited (now a zombie until reaped)

    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(proc.args, 5)

    monkeypatch.setattr(subprocess.Popen, "communicate", _timeout)
    _kill_process_group(proc)

    assert proc.returncode is not None, (
        "_kill_process_group left the child unreaped (zombie) after "
        "communicate timeout"
    )


def test_3b_run_second_wait_timeout_reaps(tmp_path):
    """Spawn-path timeout with an escaped grandchild: killpg SIGKILL cannot
    reach the grandchild (own setsid), the drain pipes stay open — run()
    must still reap/warn instead of leaking a zombie direct child.

    Injected variant: even when every Popen.wait times out (unkillable
    child), run() must report timed_out and emit a structured warning."""
    script = tmp_path / "stubborn_agent.py"
    script.write_text(
        "import json, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        # grandchild escapes the agent's process group and holds the pipes
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'])\n"
        "print(json.dumps({'type': 'partial', 'text': 'x'}), flush=True)\n"
        "time.sleep(30)\n"
    )
    proc_holder = {}
    import agentproc.runner as runner_mod
    orig_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        p = orig_popen(*a, **kw)
        proc_holder["proc"] = p
        return p

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner_mod.subprocess, "Popen", spying_popen)
    try:
        result = run(
            {
                "command": sys.executable,
                "args": [str(script)],
                "timeout_secs": 1,
                "kill_grace_secs": 1,
            },
            RunOptions(message="hi"),
        )
    finally:
        monkeypatch.undo()

    assert result.timed_out is True
    time.sleep(0.3)
    p = proc_holder.get("proc")
    assert p is not None
    assert p.returncode is not None, (
        "run() timeout path left the direct child unreaped (zombie) — "
        "no reap and no structured warning after wait(timeout=2) expired"
    )
    subprocess.run(["pkill", "-f", "time.sleep(15)"], check=False)
    subprocess.run(["pkill", "-f", "time.sleep(30)"], check=False)


def test_3b_injected_wait_always_times_out_warns(tmp_path, monkeypatch):
    """Injected branch: Popen.wait always raises TimeoutExpired — run() must
    still return timed_out=True and emit the unreaped structured warning."""
    script = tmp_path / "stubborn_agent2.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )

    def _always_timeout(self, *a, **kw):
        raise subprocess.TimeoutExpired(self.args, 2)

    monkeypatch.setattr(subprocess.Popen, "wait", _always_timeout)
    warnings = []
    result = run(
        {
            "command": sys.executable,
            "args": [str(script)],
            "timeout_secs": 1,
            "kill_grace_secs": 1,
        },
        RunOptions(message="hi", on_stderr=warnings.append),
    )
    monkeypatch.undo()
    assert result.timed_out is True
    assert any("unreaped" in w for w in warnings), (
        f"no structured unreaped warning; stderr: {warnings!r}"
    )
    subprocess.run(["pkill", "-f", "time.sleep(30)"], check=False)
