"""Workspace run lock——遗言锁：记录在跑的 agent 进程组，供 kill-before-start 清场。

问题（keeper 迁移设计 G3 / P0 复测）：worker 被 SIGKILL/OOM 硬杀时，agent CLI
经 ``start_new_session`` 脱离进程组，无人回收——recursive 继续跑完剩余几十分钟
并持续写 worktree；接管者（resume-retry / 重派）再开工即与孤儿并发写。

方案：kill-before-start（接管前清场），不追「死时清场」——那需要存活于 worker
之外的触发器（PDEATHSIG 仅 Linux、看门狗线程随 worker 同死）。两半：

- spawn 侧（:func:`write_run_lock` / :func:`clear_run_lock`）：Popen 成功后把
  pid/命令落盘为普通 JSON；正常结束清理。worker 硬死时文件残留即遗言。
- 接管侧（:func:`cleanup_stale_run`）：新 agent 开工前探测——pid 已死 → 清锁
  放行；pid 活着且身份核实通过（pgid==pid 且命令含记录的 argv0 基名）→
  killpg 整组、等死后放行；活着但身份无法核实 → 不杀不放行，抛
  :class:`RunLockBusy` 由调用方决定（编排节点=报错终态；reaper=告警继续）。

设计取舍：

- **普通文件而非 flock**：worker 硬死时内核锁随 fd 自动释放，锁住的是已死
  持有者，挡不住孤儿；文件 + pid 探测才带得出「杀谁」。
- **集中目录**（``~/.agentproc/run-locks/<sha256(key)>.json``）而非 workspace
  内：避免污染 git 工作区（keeper WIP 快照 ``git add -A`` 会收进提交）；
  key = workspace 绝对路径。``AGENTPROC_RUN_LOCK_DIR`` 可改（测试用）。
- **早退不清理是安全的（自愈）**：所有早退点子进程已死（communicate/wait 已
  返回或已 killpg），残留锁的 pid 必死，下次探测按 stale 清理；若异常发生在
  wait 之前（罕见），存活 pid 恰是真孤儿——被杀正是期望行为。
- **双因子身份核实防 pid 重用误杀**：孤儿判定要求 pgid==pid（会话领袖）且
  命令行含记录的 argv0 基名；任一不符按 stale 处理，绝不盲杀。
- Windows 无 killpg：探测返回 ``unsupported``（不阻塞主流程），锁读写照常。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOCK_DIR_ENV = "AGENTPROC_RUN_LOCK_DIR"
_DEFAULT_LOCK_DIR = os.path.join("~", ".agentproc", "run-locks")

# 同 key 进程内互斥（issue #17）：write/clear 全程持锁，防止多线程交错导致
# 世代号回退或 clear 误删。仅覆盖本进程——跨进程仍靠文件 + pid 探测。
_GUARD = threading.Lock()
_KEY_MUTEX: Dict[str, threading.Lock] = {}


def _key_mutex(key: str) -> threading.Lock:
    with _GUARD:
        lock = _KEY_MUTEX.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_MUTEX[key] = lock
        return lock


class RunLockBusy(RuntimeError):
    """workspace 疑似仍有存活 agent 占用且身份无法核实——拒绝开工（fail-safe）。"""


def _lock_dir() -> Path:
    return Path(os.environ.get(LOCK_DIR_ENV) or _DEFAULT_LOCK_DIR).expanduser()


def lock_path_for(key: str) -> Path:
    digest = hashlib.sha256(os.path.abspath(key).encode("utf-8")).hexdigest()[:32]
    return _lock_dir() / f"{digest}.json"


def write_run_lock(key: str, pid: int, argv: List[str]) -> Optional[Tuple[Path, int]]:
    """Popen 成功后落遗言锁（best-effort：失败只损失孤儿可见性，不阻断运行）。

    返回 ``(path, generation)``；generation 为该 key 的单调递增世代号，
    供 :func:`clear_run_lock` 只清自己世代——先结束者不得误清仍在跑者的锁。
    """
    mutex = _key_mutex(key)
    try:
        with mutex:
            path = lock_path_for(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            generation = 0
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
                generation = int(prev.get("generation", 0))
            except (OSError, ValueError, TypeError):
                pass
            generation += 1
            record = {
                "key": os.path.abspath(key),
                "pid": int(pid),
                "command": Path(argv[0]).name if argv else "",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "generation": generation,
            }
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh, ensure_ascii=False)
                os.replace(tmp_name, path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return path, generation
    except OSError:
        return None


def clear_run_lock(key: str, generation: Optional[int] = None) -> None:
    """正常收尾清锁（best-effort）。带 ``generation`` 时只清自己世代——
    锁文件已属更高世代（仍在跑的后来者）则不动。"""
    mutex = _key_mutex(key)
    try:
        with mutex:
            path = lock_path_for(key)
            if generation is None:
                path.unlink(missing_ok=True)
                return
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                current = int(record.get("generation", 0))
            except (OSError, ValueError, TypeError):
                return  # 文件不存在/损坏：无事可清
            if current == generation:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 他人用户的进程：视为存活（后续身份核实必然不匹配）
    except OverflowError:
        return False
    return True


def _pid_gone(pid: int, grace_secs: float) -> bool:
    """击杀后等待进程「不再存在」；僵尸（等父收尸）也算——对清场而言它已
    不再写任何东西。父进程死亡的孤儿由 init/launchd 即刻收尸，此分支主要
    覆盖「调用方自己就是父进程」的测试/同进程场景。"""
    deadline = time.monotonic() + grace_secs
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        try:
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False  # 拿不到状态：保守视作仍存活
        if (out.stdout or "").strip().startswith("Z"):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _ps_fields(pid: int) -> Optional[Tuple[str, str]]:
    """``(pgid, command)``；ps 不可用/超时返回 None——调用方不得据此做杀决策。"""
    try:
        proc = subprocess.run(
            ["ps", "-o", "pgid=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (proc.stdout or "").strip()
    if proc.returncode != 0 or not line:
        return None
    parts = line.split(None, 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def cleanup_stale_run(key: str, *, grace_secs: float = 10.0) -> Dict[str, Any]:
    """kill-before-start：探测并清理 key workspace 的孤儿 agent 进程组。

    返回 ``{"action": clean|stale|killed|unsupported, ...}``；存疑（身份无法
    核实/杀后未死/杀组被拒）抛 :class:`RunLockBusy`——不删锁、不盲杀。
    """
    if not hasattr(os, "killpg"):
        return {"action": "unsupported"}
    path = lock_path_for(key)
    if not path.exists():
        return {"action": "clean"}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        pid = int(record["pid"])
        command = str(record.get("command") or "")
    except (OSError, ValueError, TypeError, KeyError):
        _unlink(path)
        return {"action": "stale", "reason": "corrupt"}
    if not _pid_alive(pid):
        _unlink(path)
        return {"action": "stale", "reason": "pid-gone", "pid": pid}

    fields = _ps_fields(pid)
    if fields is None:
        raise RunLockBusy(
            f"workspace {os.path.abspath(key)!r} 的遗言锁指向存活 pid {pid}，"
            f"但身份无法核实（ps 不可用）——拒绝开工以免并发写；"
            f"请人工确认后删除锁文件 {path}")
    pgid_now, cmd_now = fields
    if str(pgid_now) != str(pid) or (command and command not in cmd_now):
        _unlink(path)
        return {"action": "stale", "reason": "pid-reused", "pid": pid}

    # 身份核实通过：确为本机制派生的孤儿 agent → 杀整组再放行
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise RunLockBusy(
            f"workspace {os.path.abspath(key)!r} 孤儿 pid {pid} 身份已核实但 "
            f"killpg 被拒：{exc}") from exc
    deadline = time.monotonic() + grace_secs
    if not _pid_gone(pid, grace_secs):
        raise RunLockBusy(
            f"workspace {os.path.abspath(key)!r} 孤儿 pid {pid} SIGKILL 后 "
            f"{grace_secs}s 仍存活（D 状态?）——拒绝开工")
    _unlink(path)
    return {"action": "killed", "pid": pid, "command": command}


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
