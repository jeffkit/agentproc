"""并发原语（issue #17 / spec「Concurrency」章节）。

- :class:`SessionSerializer`：同 key（如 session_id）的并发 run 串行执行——
  第二条在第一条落定（result/error/timeout）后才开始，杜绝两个 agent 进程
  并发 resume 同一会话导致 transcript 分叉。
- :class:`ConcurrencyGate`：per-runner 全局并发上限；默认无限制（向后兼容）。
  超限行为显式二选一：``"queue"``（默认，FIFO 排队）或 ``"reject"``（立即以
  协议 error 终态拒绝，文案含固定标记 ``agentproc: concurrency limit``）。

并发闸在 spawn 之前判定；不新增任何 wire 事件类型。
"""
from __future__ import annotations

import contextlib
import threading
from types import TracebackType
from typing import Dict, Iterator, Optional, Tuple, Type

CONCURRENCY_LIMIT_MARKER = "agentproc: concurrency limit"


class ConcurrencyLimitError(RuntimeError):
    """`on_saturated="reject"` 时并发闸满员——turn 立即以 error 终态拒绝。"""

    def __init__(self, max_concurrent: int):
        super().__init__(
            f"{CONCURRENCY_LIMIT_MARKER}: max_concurrent={max_concurrent} "
            f"reached; turn rejected (on_saturated=reject)"
        )
        self.max_concurrent = max_concurrent


class _NullLock:
    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        return None


class SessionSerializer:
    """同 key 串行化：per-key ``threading.Lock``（dict + 全局守卫锁）。

    key 为 ``None`` 时是 no-op（未配置 session_key 的调用行为不变）。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}

    def lock(self, key: Optional[str]):
        if key is None:
            return _NullLock()
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    @contextlib.contextmanager
    def serialize(self, key: Optional[str]) -> Iterator[None]:
        with self.lock(key):
            yield


class ConcurrencyGate:
    """全局并发闸：``BoundedSemaphore`` + queue/reject 语义。

    ``max_concurrent`` 为 ``None`` 表示无限制（默认；acquire 是 no-op）。
    ``"queue"`` 模式阻塞等待空位（信号量本身 FIFO）；``"reject"`` 模式
    非阻塞获取，满员抛 :class:`ConcurrencyLimitError`。
    """

    def __init__(self, max_concurrent: Optional[int], on_saturated: str = "queue"):
        if on_saturated not in ("queue", "reject"):
            raise ValueError(f"on_saturated must be 'queue' or 'reject', got {on_saturated!r}")
        if max_concurrent is not None and max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1 or None, got {max_concurrent!r}")
        self.max_concurrent = max_concurrent
        self.on_saturated = on_saturated
        self._sem = threading.BoundedSemaphore(max_concurrent) if max_concurrent else None

    def acquire(self) -> None:
        if self._sem is None:
            return
        if self.on_saturated == "reject":
            if not self._sem.acquire(blocking=False):
                raise ConcurrencyLimitError(self.max_concurrent or 0)
            return
        self._sem.acquire()

    def release(self) -> None:
        if self._sem is None:
            return
        try:
            self._sem.release()
        except ValueError:
            pass  # 过度释放：无害（防御 finally 双路径）

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()
