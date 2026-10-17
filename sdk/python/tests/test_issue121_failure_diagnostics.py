"""#121 — executor 非零退出时的失败上报：提取「有诊断价值的行」。

现场（plaita#121）：recursive 启动时把 ``checkpoint: per-turn snapshots
active`` 打到 stderr 第一行，真正的原因是后面的 LLM 瞬时 HTTP 错误（限流/5xx）。
旧实现取 ``stderr_text[:500]``（头部固定窗口），横幅+正常日志把窗口占满，真因
被丢弃；flow 层、fix-loop 提示词与 ``failure-*.log`` 只看到无关的 checkpoint 行。

契约（本文件锁定）：
- 命中已知错误形态（``transient HTTP error`` / 429 / 5xx / panic /
  ``thread '…' panicked`` / error|exception|fatal|failed）的行进 ``result.error``；
- 一条都不命中时带 stderr **末 N 行**并显式标注「未必是根因」，不静默丢；
- 无 stderr 保持 ``(no stderr)`` 旧措辞；
- 同一文本透传 ``on_error``（fix-loop 的同源）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentproc.runner import (
    FAILURE_TAIL_LINES,
    RunOptions,
    extract_failure_diagnostics,
    run_via_executor,
)

CHECKPOINT = "checkpoint: per-turn snapshots active"
TRANSIENT = (
    "agent.turn:agent.step:llm.complete: transient HTTP error, "
    "retrying attempt=0 backoff_ms=200 status=429"
)


def _padded_stderr(*, cause: str = TRANSIENT, pad_lines: int = 20) -> str:
    """复刻现场：启动横幅 + 一堆正常日志（>500 字符）+ 真因。"""
    body = [CHECKPOINT]
    body += [
        f"profile: glm-52 workspace=/tmp/ws step={i} tool=read_file ok elapsed_ms={i}"
        for i in range(pad_lines)
    ]
    if cause:
        body.append(cause)
    body.append("fatal: giving up after 1 retry")
    return "\n".join(body)


def _stderr_executor(tmp_path, script_body: str):
    agent = tmp_path / "fake-agent.py"
    agent.write_text(script_body)
    return {
        "cli_name": "recursive",
        "plain": False,
        "build_args": lambda message, session_id, env, ctx: [sys.executable, str(agent)],
        "parse_event": lambda event: {},
    }


def _run_script(tmp_path, stderr_text: str):
    script = (
        "import sys\n"
        f"sys.stderr.write({stderr_text!r})\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )
    errors = []
    result = run_via_executor(
        _stderr_executor(tmp_path, script),
        RunOptions(message="x", timeout_secs=60, on_error=errors.append),
    )
    return result, errors


def test_diagnostic_line_beats_startup_banner(tmp_path):
    """真因在 500 字符窗口之外：必须被提取，而非只报 checkpoint 横幅。"""
    result, errors = _run_script(tmp_path, _padded_stderr())
    assert result.exit_code == 1
    assert "transient HTTP error" in result.error, result.error
    assert "status=429" in result.error, result.error
    assert CHECKPOINT not in result.error, (
        "无关的启动横幅不应再占据失败上报"
    )
    assert errors == [result.error], "on_error 必须与节点错误同源（fix-loop 提示词）"


def test_no_diagnostic_falls_back_to_marked_tail(tmp_path):
    """无诊断行：带 stderr 末 N 行并显式标注「未必是根因」，不静默。"""
    benign = "\n".join(f"step {i}: ok" for i in range(FAILURE_TAIL_LINES + 5))
    result, _ = _run_script(tmp_path, benign)
    assert "not necessarily the cause" in result.error, result.error
    # 末行在、最早的行被截掉（只带尾窗口）
    assert f"step {FAILURE_TAIL_LINES + 4}: ok" in result.error
    assert "step 0: ok" not in result.error


def test_empty_stderr_keeps_historical_wording(tmp_path):
    result, _ = _run_script(tmp_path, "")
    assert result.error.endswith("(no stderr)"), result.error


def test_unit_helpers_contract():
    assert extract_failure_diagnostics("") == "(no stderr)"
    # 顺序保持 + 去重 + 上限
    text = f"e1 error\ne1 error\nwarn\n{TRANSIENT}"
    got = extract_failure_diagnostics(text)
    assert got.splitlines() == ["e1 error", TRANSIENT], got
    # 命中时不含尾部标注
    assert "not necessarily" not in got


def test_hits_are_capped():
    text = "\n".join(f"boom error {i}" for i in range(50))
    got = extract_failure_diagnostics(text)
    assert len(got.splitlines()) == 10, got


def test_checkpoint_banner_alone_is_not_a_diagnostic_line():
    """只有启动横幅时不得把它当诊断行，必须走尾部标注路径（不静默丢）。"""
    text = "\n".join([CHECKPOINT] + [f"step {i} ok" for i in range(3)])
    got = extract_failure_diagnostics(text)
    assert "not necessarily the cause" in got
    assert CHECKPOINT in got  # 尾窗口内的原始信息不被抹掉


def test_rust_panic_and_5xx_are_diagnostic():
    text = "thread 'main' panicked at src/llm.rs:42\nHTTP 503 Service Unavailable"
    got = extract_failure_diagnostics(text)
    assert "panicked" in got and "503" in got
