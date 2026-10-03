"""Executor-path env composition tests (issue #5).

run_via_executor must apply the spawn path's env composition (infra set +
profile env + env_allowlist via _compose_env) — not inherit the whole host
environment.
"""
from __future__ import annotations

import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentproc.runner import RunOptions, run, run_via_executor


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
        "build_args": lambda message, session_id, env: [str(agent_path)],
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
