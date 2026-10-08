//! Concurrency primitives (issue #17 / spec "Concurrency" section).
//!
//! - [`SessionSerializer`]: serialize concurrent runs sharing a key (e.g. a
//!   session id) — the second run starts only after the first settles; two
//!   agent processes never concurrently resume one session. A `None` key is a
//!   no-op (runs without a key are unaffected).
//! - [`ConcurrencyGate`]: per-runner global cap on concurrent agent processes,
//!   default unlimited (backward compatible). Burst semantics are explicit:
//!   [`Saturated::Queue`] (FIFO wait, default) or [`Saturated::Reject`] —
//!   immediate terminal error containing the fixed marker
//!   `agentproc: concurrency limit`.
//!
//! The gate is evaluated before spawn; no new wire event types are introduced.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use once_cell::sync::Lazy;
use tokio::sync::{Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore};

/// Fixed marker carried in the rejection error message (spec-mandated).
pub const CONCURRENCY_LIMIT_MARKER: &str = "agentproc: concurrency limit";

/// Burst behaviour when the concurrency cap is reached.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum Saturated {
    /// Wait in FIFO order until a slot frees up (default).
    #[default]
    Queue,
    /// Immediately terminate the turn with a protocol `error` containing
    /// [`CONCURRENCY_LIMIT_MARKER`].
    Reject,
}

/// Per-session serialization: one async mutex per key, shared process-wide.
#[derive(Default)]
pub struct SessionSerializer {
    locks: Mutex<HashMap<String, Arc<AsyncMutex<()>>>>,
}

impl SessionSerializer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Acquire the per-key mutex. `None` returns a dummy (no serialization).
    pub async fn lock(&self, key: Option<&str>) -> Option<tokio::sync::OwnedMutexGuard<()>> {
        let guard = key.and_then(|k| {
            let mut map = self.locks.lock().unwrap();
            Some(map.entry(k.to_string()).or_default().clone())
        })?;
        Some(guard.lock_owned().await)
    }
}

/// Global concurrency gate backed by a [`Semaphore`].
pub struct ConcurrencyGate {
    max_concurrent: Option<usize>,
    on_saturated: Saturated,
    sem: Option<Arc<Semaphore>>,
}

impl ConcurrencyGate {
    pub fn new(max_concurrent: Option<usize>, on_saturated: Saturated) -> Self {
        Self {
            max_concurrent,
            on_saturated,
            sem: max_concurrent.map(|n| Arc::new(Semaphore::new(n))),
        }
    }

    /// Acquire one slot. `Err` carries the rejection message (marker included).
    pub async fn acquire(&self) -> Result<Option<tokio::sync::SemaphorePermit<'_>>, String> {
        let Some(sem) = &self.sem else {
            return Ok(None);
        };
        match self.on_saturated {
            Saturated::Queue => match sem.acquire().await {
                Ok(permit) => Ok(Some(permit)),
                Err(_) => Err("semaphore closed".to_string()),
            },
            Saturated::Reject => match sem.try_acquire() {
                Ok(permit) => Ok(Some(permit)),
                Err(_) => Err(self.rejection_message()),
            },
        }
    }

    /// Owned variant of [`acquire`](Self::acquire): the permit carries its own
    /// `Arc` on the semaphore, so the caller can hold a slot for the whole run
    /// without borrowing the gate (which lives in the process-wide registry).
    pub async fn acquire_owned(
        self: &Arc<Self>,
    ) -> Result<Option<OwnedSemaphorePermit>, String> {
        let Some(sem) = &self.sem else {
            return Ok(None);
        };
        match self.on_saturated {
            Saturated::Queue => match sem.clone().acquire_owned().await {
                Ok(permit) => Ok(Some(permit)),
                Err(_) => Err("semaphore closed".to_string()),
            },
            Saturated::Reject => match sem.clone().try_acquire_owned() {
                Ok(permit) => Ok(Some(permit)),
                Err(_) => Err(self.rejection_message()),
            },
        }
    }

    fn rejection_message(&self) -> String {
        format!(
            "{CONCURRENCY_LIMIT_MARKER}: max_concurrent={} reached; turn rejected (on_saturated=reject)",
            self.max_concurrent.unwrap_or(0)
        )
    }
}

/// Process-wide gate registry: one gate per `(max_concurrent, on_saturated)`
/// configuration, so every `run` call in the process contends on the same
/// slots — and two configurations in flight never clobber each other.
static GATES: Lazy<Mutex<HashMap<(usize, Saturated), Arc<ConcurrencyGate>>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

/// The shared gate for the given configuration; `None` = unlimited (no gate).
pub(crate) fn gate_for(
    max_concurrent: Option<usize>,
    on_saturated: Saturated,
) -> Option<Arc<ConcurrencyGate>> {
    let max = max_concurrent?;
    let mut gates = GATES.lock().unwrap();
    Some(
        gates
            .entry((max, on_saturated))
            .or_insert_with(|| Arc::new(ConcurrencyGate::new(Some(max), on_saturated)))
            .clone(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn serializer_serializes_same_key() {
        let ser = Arc::new(SessionSerializer::new());
        let order = Arc::new(Mutex::new(Vec::new()));
        let mut handles = Vec::new();
        for name in ["A", "B"] {
            let ser = ser.clone();
            let order = order.clone();
            handles.push(tokio::spawn(async move {
                let _g = ser.lock(Some("k")).await;
                order.lock().unwrap().push(format!("start-{name}"));
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                order.lock().unwrap().push(format!("end-{name}"));
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        let o = order.lock().unwrap().clone();
        assert_eq!(o.len(), 4);
        // Intervals must not interleave.
        assert!(o[0].starts_with("start-") && o[1].starts_with("end-"));
        assert!(o[2].starts_with("start-") && o[3].starts_with("end-"));
    }

    #[tokio::test]
    async fn serializer_none_key_is_noop() {
        let ser = SessionSerializer::new();
        assert!(ser.lock(None).await.is_none());
    }

    #[tokio::test]
    async fn gate_reject() {
        let gate = ConcurrencyGate::new(Some(1), Saturated::Reject);
        let p = gate.acquire().await.unwrap();
        assert!(p.is_some());
        let err = gate.acquire().await.unwrap_err();
        assert!(err.contains(CONCURRENCY_LIMIT_MARKER));
        drop(p);
        assert!(gate.acquire().await.unwrap().is_some());
    }

    #[tokio::test]
    async fn gate_queue_caps_concurrency() {
        let gate = Arc::new(ConcurrencyGate::new(Some(1), Saturated::Queue));
        let active = Arc::new(Mutex::new(0usize));
        let peak = Arc::new(Mutex::new(0usize));
        let mut handles = Vec::new();
        for _ in 0..3 {
            let gate = gate.clone();
            let active = active.clone();
            let peak = peak.clone();
            handles.push(tokio::spawn(async move {
                let _p = gate.acquire().await.unwrap();
                {
                    // One scope, one lock order: `peak` must never be locked
                    // twice in one expression (std mutexes are not reentrant).
                    let mut active = active.lock().unwrap();
                    *active += 1;
                    let mut peak = peak.lock().unwrap();
                    *peak = (*peak).max(*active);
                }
                tokio::time::sleep(std::time::Duration::from_millis(30)).await;
                *active.lock().unwrap() -= 1;
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        assert_eq!(*peak.lock().unwrap(), 1);
    }

    #[test]
    fn gate_for_shares_one_gate_per_config() {
        let a = gate_for(Some(3), Saturated::Reject).unwrap();
        let b = gate_for(Some(3), Saturated::Reject).unwrap();
        assert!(Arc::ptr_eq(&a, &b), "same config must reuse one gate");
        let c = gate_for(Some(3), Saturated::Queue).unwrap();
        assert!(!Arc::ptr_eq(&a, &c), "different mode must not share a gate");
        let d = gate_for(Some(4), Saturated::Reject).unwrap();
        assert!(!Arc::ptr_eq(&a, &d), "different cap must not share a gate");
        assert!(gate_for(None, Saturated::Queue).is_none());
    }

    #[tokio::test]
    async fn acquire_owned_contends_on_the_shared_gate() {
        let gate = gate_for(Some(1), Saturated::Reject).unwrap();
        let held = gate.acquire_owned().await.unwrap();
        assert!(held.is_some());
        // A second call through the registry sees the same saturated gate.
        let same = gate_for(Some(1), Saturated::Reject).unwrap();
        assert!(same.acquire_owned().await.unwrap_err().contains(CONCURRENCY_LIMIT_MARKER));
        drop(held);
        assert!(same.acquire_owned().await.unwrap().is_some());
    }

    #[tokio::test]
    async fn gate_unlimited() {
        let gate = ConcurrencyGate::new(None, Saturated::Queue);
        for _ in 0..5 {
            assert!(gate.acquire().await.unwrap().is_none());
        }
    }
}
