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

use tokio::sync::{Mutex as AsyncMutex, Semaphore};

/// Fixed marker carried in the rejection error message (spec-mandated).
pub const CONCURRENCY_LIMIT_MARKER: &str = "agentproc: concurrency limit";

/// Burst behaviour when the concurrency cap is reached.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
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
                Err(_) => Err(format!(
                    "{CONCURRENCY_LIMIT_MARKER}: max_concurrent={} reached; turn rejected (on_saturated=reject)",
                    self.max_concurrent.unwrap_or(0)
                )),
            },
        }
    }
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
                *active.lock().unwrap() += 1;
                *peak.lock().unwrap() = (*peak.lock().unwrap()).max(*active.lock().unwrap());
                tokio::time::sleep(std::time::Duration::from_millis(30)).await;
                *active.lock().unwrap() -= 1;
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        assert_eq!(*peak.lock().unwrap(), 1);
    }

    #[tokio::test]
    async fn gate_unlimited() {
        let gate = ConcurrencyGate::new(None, Saturated::Queue);
        for _ in 0..5 {
            assert!(gate.acquire().await.unwrap().is_none());
        }
    }
}
