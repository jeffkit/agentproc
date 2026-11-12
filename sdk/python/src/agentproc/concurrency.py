"""Process-wide concurrency gate + per-session serializer (issue #17).

The wire protocol is scoped to one turn — one message, one process, one
reply — and says nothing about what happens when a *host* issues overlapping
``run()`` calls. A messaging-platform bridge absolutely can: two fast
messages in the same chat, an IM burst, a retry racing the original. Before
this module every such overlap simply fanned out one subprocess per call
until the machine ran out of memory.

Two opt-in primitives, both **process-wide** and both defaulting to the
historical behaviour (unbounded), so existing deployments see no change:

- **Global run gate** — at most ``max_concurrent_runs`` ``run()`` calls
  execute at once. When the limit is hit, ``overflow`` decides:
  ``"wait"`` (default) blocks the caller until a slot frees; ``"reject"``
  raises :class:`ConcurrencyRejected` immediately so the bridge can answer
  "busy". This is the explicit "queue or drop" decision point the spec's
  Concurrency section tells bridges to own.
- **Per-session serializer** — when ``serialize_same_session`` is on, a
  ``run()`` whose ``session_id`` equals one already in flight **waits**
  until the earlier turn finishes. Two fast messages to the same chat can
  no longer spawn two concurrent ``--resume`` processes against the same
  agent session; different sessions still run in parallel.

Configuration (first use wins, explicit ``configure_concurrency()`` beats
env — env is read once, lazily, on the first ``acquire()``)::

    from agentproc import configure_concurrency
    configure_concurrency(
        max_concurrent_runs=4,          # None = unbounded (default)
        overflow="wait",                # "wait" | "reject"
        serialize_same_session=True,    # default False
    )

    # or purely via environment (read once at first run):
    #   AGENTPROC_MAX_CONCURRENT_RUNS=4
    #   AGENTPROC_CONCURRENCY_OVERFLOW=wait|reject
    #   AGENTPROC_SERIALIZE_SAME_SESSION=1

Thread-safety: ``run()`` is thread-safe (the runner spawns subprocesses);
the gate uses one lock + condition variable. The gate does NOT make
``run()`` re-entrant safe per thread — a handler must not call ``run()``
with the same session from inside that session's own turn (that is a
deadlock by definition, same as taking a non-reentrant mutex twice).

The gate lives at the ``run()`` entry point only (spawn + executor paths
are both dispatched through it); direct ``run_via_executor()`` calls from
SDK-internal harnesses bypass it by design.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

ENV_MAX_CONCURRENT = "AGENTPROC_MAX_CONCURRENT_RUNS"
ENV_OVERFLOW = "AGENTPROC_CONCURRENCY_OVERFLOW"
ENV_SERIALIZE_SESSION = "AGENTPROC_SERIALIZE_SAME_SESSION"

_OVERFLOWS = ("wait", "reject")
_TRUTHY = ("1", "true", "yes", "on")


class ConcurrencyRejected(RuntimeError):
    """The global run gate rejected this run (``overflow="reject"`` and no
    slot was free). Nothing was spawned; no session state was touched.

    Bridges should catch this to answer "busy" — it is the protocol-visible
    "drop" arm of the queue-or-drop decision the spec assigns to the bridge.
    """

    def __init__(self, message: str = "", max_concurrent_runs: Optional[int] = None):
        super().__init__(message or "run rejected: concurrent run limit reached")
        self.max_concurrent_runs = max_concurrent_runs


class _SessionGuard:
    """Per-session mutex with refcounted lifetime (created on first waiter,
    deleted when the last holder leaves)."""

    def __init__(self, gate: "_RunGate", session_id: str) -> None:
        self._gate = gate
        self.session_id = session_id
        self._lock: Optional[threading.Lock] = None

    def acquire(self) -> None:
        gate = self._gate
        with gate._lock:
            lock = gate._session_locks.get(self.session_id)
            if lock is None:
                lock = threading.Lock()
                gate._session_locks[self.session_id] = lock
            gate._session_refs[self.session_id] = gate._session_refs.get(self.session_id, 0) + 1
        # Acquire outside the dict lock so a long turn doesn't block new
        # sessions from registering their own locks.
        lock.acquire()
        self._lock = lock

    def release(self) -> None:
        gate = self._gate
        if self._lock is None:
            return
        self._lock.release()
        self._lock = None
        with gate._lock:
            remaining = gate._session_refs.get(self.session_id, 1) - 1
            if remaining <= 0:
                gate._session_refs.pop(self.session_id, None)
                gate._session_locks.pop(self.session_id, None)
            else:
                gate._session_refs[self.session_id] = remaining


class _GateToken:
    """Released exactly once by the ``run()`` wrapper's ``finally``."""

    def __init__(self, gate: "_RunGate", session: Optional[_SessionGuard]) -> None:
        self._gate = gate
        self._session = session

    def release(self) -> None:
        gate = self._gate
        with gate._cond:
            gate._active = max(0, gate._active - 1)
            gate._cond.notify()  # one waiter per freed slot
        if self._session is not None:
            self._session.release()


class _RunGate:
    """See module docstring. One instance per process (module-level)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._configured = False
        self._max: Optional[int] = None  # None = unbounded
        self._overflow = "wait"
        self._serialize = False
        self._active = 0
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_refs: Dict[str, int] = {}

    # -- configuration ------------------------------------------------------

    def _ensure_env_config(self) -> None:
        if self._configured:
            return
        with self._lock:
            if self._configured:
                return
            raw = (os.environ.get(ENV_MAX_CONCURRENT) or "").strip()
            max_runs: Optional[int] = None
            if raw:
                try:
                    n = int(raw)
                    if n > 0:
                        max_runs = n
                except ValueError:
                    pass  # garbage in env = unbounded (documented, no crash)
            overflow = (os.environ.get(ENV_OVERFLOW) or "").strip().lower()
            serialize = (os.environ.get(ENV_SERIALIZE_SESSION) or "").strip().lower() in _TRUTHY
            self._max = max_runs
            self._overflow = overflow if overflow in _OVERFLOWS else "wait"
            self._serialize = serialize
            self._configured = True

    def configure(
        self,
        max_concurrent_runs: Optional[int] = None,
        overflow: str = "wait",
        serialize_same_session: bool = False,
    ) -> None:
        """Explicit configuration; overrides the env bootstrap. Safe to call
        before first run; calling it while runs are in flight only affects
        *subsequent* acquisitions."""
        if overflow not in _OVERFLOWS:
            raise ValueError(f"overflow must be one of {_OVERFLOWS}, got {overflow!r}")
        if max_concurrent_runs is not None and (
            not isinstance(max_concurrent_runs, int) or max_concurrent_runs < 1
        ):
            raise ValueError(
                f"max_concurrent_runs must be a positive int or None (unbounded), "
                f"got {max_concurrent_runs!r}"
            )
        with self._lock:
            self._max = max_concurrent_runs
            self._overflow = overflow
            self._serialize = bool(serialize_same_session)
            self._configured = True

    # -- acquisition --------------------------------------------------------

    def acquire(self, session_id: str) -> _GateToken:
        """Order: session lock first, then the global slot. A gate-rejected
        run releases its session guard before propagating, so the next
        same-session waiter is not stuck behind a run that never started."""
        self._ensure_env_config()
        session: Optional[_SessionGuard] = None
        if self._serialize and session_id:
            session = _SessionGuard(self, session_id)
            session.acquire()
        try:
            with self._cond:
                while self._max is not None and self._active >= self._max:
                    if self._overflow == "reject":
                        raise ConcurrencyRejected(
                            f"run rejected: {self._active} runs already in flight "
                            f"(max_concurrent_runs={self._max}, overflow=reject); "
                            f"nothing was spawned",
                            max_concurrent_runs=self._max,
                        )
                    self._cond.wait()
                self._active += 1
        except ConcurrencyRejected:
            if session is not None:
                session.release()
            raise
        return _GateToken(self, session)

    # -- introspection (tests / bridges) ------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "max_concurrent_runs": self._max,
                "overflow": self._overflow,
                "serialize_same_session": self._serialize,
                "active_runs": self._active,
                "sessions_in_flight": len(self._session_refs),
            }


_gate = _RunGate()


def configure_concurrency(
    max_concurrent_runs: Optional[int] = None,
    overflow: str = "wait",
    serialize_same_session: bool = False,
) -> None:
    """Configure the process-wide gate. See module docstring for semantics."""
    _gate.configure(
        max_concurrent_runs=max_concurrent_runs,
        overflow=overflow,
        serialize_same_session=serialize_same_session,
    )


def concurrency_snapshot() -> Dict[str, Any]:
    """Read-only view of the gate (bridges may log it; tests assert on it)."""
    return _gate.snapshot()
