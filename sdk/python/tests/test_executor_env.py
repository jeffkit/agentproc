"""Executor-path env composition tests (issue #5).

run_via_executor must apply the spawn path's env composition (infra set +
profile env + env_allowlist via _compose_env) — not inherit the whole host
environment.

Guards the three acceptance clauses directly:
  1. a host sentinel outside the infra set (AWS_SECRET_ACCESS_KEY/DATABASE_URL)
     is absent from BOTH the env handed to build_args and the child process env;
  2. a ${VAR} blocked by env_allowlist expands to empty AND emits the
     on_stderr hint;
  3. a profile timeout kills the whole process group (killpg), not just the
     direct child.
"""
from __future__ import annotations

import os
import signal
import stat
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentproc.runner import RunOptions, run, run_via_executor

CLI_NAME = "agentproc-test-exec-env"


def _script(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _dump_env_script(tmp_path):
    agent = tmp_path / "dump-agent"
    agent.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"{\\\"type\\\":\\\"result\\\",\\\"text\\\":\\\""
        "SECRET=${HOST_SECRET:-unset}\\\"}\"\n"
    )
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)
    return agent


def _fake_executor(agent_path):
    return {
        "cli_name": "test-cli",
        "plain": False,
        "build_args": lambda message, session_id, env, ctx: [str(agent_path)],
        "parse_event": lambda event: {"final_text": event.get("text", "")},
    }


def _register(monkeypatch, name, executor):
    import agentproc.runner as runner_mod
    monkeypatch.setitem(runner_mod.EXECUTORS, name, executor)


def _result_executor(agent, capture=None):
    def _build_args(message, session_id, env, ctx):
        if capture is not None:
            capture.update(env)
        return [str(agent)]

    return {
        "cli_name": CLI_NAME,
        "plain": False,
        "build_args": _build_args,
        "parse_event": lambda event: {"final_text": event.get("text", "")},
    }


def test_run_via_executor_does_not_inherit_host_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SECRET", "top-secret")
    agent = _dump_env_script(tmp_path)
    r = run_via_executor(
        _fake_executor(agent),
        RunOptions(message="hi"),
    )
    assert r.exit_code == 0, r.error
    assert "top-secret" not in r.reply, (
        "run_via_executor leaked HOST_SECRET from the host environment"
    )


def test_run_executor_path_applies_profile_env_and_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SECRET", "top-secret")
    monkeypatch.setenv("ALLOWED_KEY", "ok-val")
    agent = tmp_path / "dump-agent2"
    agent.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"{\\\"type\\\":\\\"result\\\",\\\"text\\\":\\\""
        "A=${ALLOWED_KEY:-unset} S=${HOST_SECRET:-unset}\\\"}\"\n"
    )
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)

    import agentproc.runner as runner_mod
    fake = _fake_executor(agent)
    monkeypatch.setitem(runner_mod.EXECUTORS, "test-cli", fake)
    try:
        r = run(
            {
                "executor": "test-cli",
                "env": {
                    "ALLOWED_KEY": "${ALLOWED_KEY}",
                    "HOST_SECRET": "${HOST_SECRET}",
                },
                "env_allowlist": ["ALLOWED_KEY"],
            },
            RunOptions(message="hi"),
        )
    finally:
        runner_mod.EXECUTORS.pop("test-cli", None)
    assert r.exit_code == 0, r.error
    assert "A=ok-val" in r.reply
    assert "S=unset" in r.reply, "allowlist should have blocked HOST_SECRET"


def test_host_sentinels_absent_from_build_args_env_and_child_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIA-not-for-the-agent")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@internal/db")
    agent = _script(
        tmp_path, "dump.sh",
        "echo \"{\\\"type\\\":\\\"result\\\",\\\"text\\\":\\\""
        "aws=${AWS_SECRET_ACCESS_KEY:-unset} db=${DATABASE_URL:-unset}\\\"}\"\n",
    )
    seen: dict = {}
    _register(monkeypatch, CLI_NAME, _result_executor(agent, seen))

    r = run({"executor": CLI_NAME}, RunOptions(message="hi"))

    assert r.exit_code == 0, r.error
    assert "AWS_SECRET_ACCESS_KEY" not in seen, "sentinel reached build_args env"
    assert "DATABASE_URL" not in seen, "sentinel reached build_args env"
    assert "aws=unset" in r.reply and "db=unset" in r.reply, r.reply


def test_blocked_env_ref_expands_empty_and_warns_on_executor_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SECRET", "top-secret")
    agent = _script(
        tmp_path, "blocked.sh",
        "echo \"{\\\"type\\\":\\\"result\\\",\\\"text\\\":\\\""
        "tok=[${TOKEN}] hid=${HOST_SECRET:-unset}\\\"}\"\n",
    )
    _register(monkeypatch, CLI_NAME, _result_executor(agent))
    stderr: list = []

    r = run(
        {
            "executor": CLI_NAME,
            "env": {"TOKEN": "${HOST_SECRET}"},
            "env_allowlist": [],
        },
        RunOptions(message="hi", on_stderr=stderr.append),
    )

    assert r.exit_code == 0, r.error
    assert "tok=[]" in r.reply, r.reply
    assert "hid=unset" in r.reply, r.reply
    assert any("env_allowlist blocked ${HOST_SECRET}" in line for line in stderr), stderr


def test_executor_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    # The shell ignores SIGTERM and holds a sleep child: only a killpg can
    # clear the group, so a surviving group proves the escalation is missing.
    agent = _script(
        tmp_path, "stubborn.sh",
        f"echo $$ > {pid_file}\ntrap '' TERM\nsleep 30 &\nwait\n",
    )
    _register(monkeypatch, CLI_NAME, _result_executor(agent))
    start = time.monotonic()

    r = run(
        {"executor": CLI_NAME, "timeout_secs": 1, "kill_grace_secs": 1},
        RunOptions(message="hi"),
    )
    elapsed = time.monotonic() - start

    assert r.exit_code != 0 and r.timed_out, r
    assert elapsed < 15, f"escalation hung for {elapsed:.1f}s"
    pid = int(pid_file.read_text().strip())
    for _ in range(50):
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return
    os.killpg(pid, signal.SIGKILL)
    raise AssertionError(f"process group {pid} survived the executor timeout")
