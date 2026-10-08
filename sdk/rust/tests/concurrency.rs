//! End-to-end concurrency behaviour of `run` (issue #17 / spec "Concurrency").
//!
//! The primitive unit tests live in `src/concurrency.rs`; only a real run
//! proves the serializer and the gate are actually *held* for the duration of
//! a turn (a permit dropped on the way into the run would satisfy every unit
//! test and still allow unbounded fan-out).

use std::sync::Arc;
use std::time::{Duration, Instant};

use agentproc::{run, Profile, RunOptions, Saturated, CONCURRENCY_LIMIT_MARKER};

const RESULT_LINE: &str = r#"{"type":"result","text":"ok"}"#;

/// A profile for `/bin/sh` that waits `ms`, then emits one conformant result.
fn slow_profile(ms: u64) -> Profile {
    Profile {
        command: "sh".to_string(),
        args: vec![
            "-c".to_string(),
            format!("sleep {}; printf '%s\\n' '{}'", ms as f64 / 1000.0, RESULT_LINE),
        ],
        timeout_secs: 30,
        ..Default::default()
    }
}

/// A profile that emits its result immediately.
fn fast_profile() -> Profile {
    Profile {
        command: "sh".to_string(),
        args: vec!["-c".to_string(), format!("printf '%s\\n' '{}'", RESULT_LINE)],
        timeout_secs: 30,
        ..Default::default()
    }
}

#[tokio::test]
async fn same_session_key_serializes_runs() {
    let profile = slow_profile(600);
    let started = Instant::now();
    let (a, b) = tokio::join!(
        run(&profile, RunOptions::new("hi").with_session_key("sess-1")),
        run(&profile, RunOptions::new("hi").with_session_key("sess-1")),
    );
    let elapsed = started.elapsed();

    let (a, b) = (a.unwrap(), b.unwrap());
    assert!(a.error.is_empty(), "first run failed: {}", a.error);
    assert!(b.error.is_empty(), "second run failed: {}", b.error);
    assert!(
        elapsed >= Duration::from_millis(1000),
        "same-key runs overlapped: {elapsed:?}"
    );
}

#[tokio::test]
async fn without_session_key_runs_are_concurrent() {
    let profile = slow_profile(600);
    let started = Instant::now();
    let (a, b) = tokio::join!(
        run(&profile, RunOptions::new("hi")),
        run(&profile, RunOptions::new("hi")),
    );
    let elapsed = started.elapsed();

    assert!(a.unwrap().error.is_empty());
    assert!(b.unwrap().error.is_empty());
    assert!(
        elapsed < Duration::from_millis(1000),
        "runs without a session key were serialized: {elapsed:?}"
    );
}

#[tokio::test]
async fn max_concurrent_queue_caps_fan_out() {
    let profile = slow_profile(200);
    let started = Instant::now();
    let (a, b, c) = tokio::join!(
        run(&profile, RunOptions::new("m").with_max_concurrent(1)),
        run(&profile, RunOptions::new("m").with_max_concurrent(1)),
        run(&profile, RunOptions::new("m").with_max_concurrent(1)),
    );
    let elapsed = started.elapsed();

    for r in [a.unwrap(), b.unwrap(), c.unwrap()] {
        assert!(r.error.is_empty(), "queued run failed: {}", r.error);
    }
    assert!(
        elapsed >= Duration::from_millis(500),
        "gate did not cap concurrency: {elapsed:?}"
    );
}

#[tokio::test]
async fn max_concurrent_reject_fails_fast_with_marker() {
    let slow = slow_profile(400);
    let holder = tokio::spawn(async move {
        run(
            &slow,
            RunOptions::new("m")
                .with_max_concurrent(1)
                .with_on_saturated(Saturated::Reject),
        )
        .await
        .unwrap()
    });
    tokio::time::sleep(Duration::from_millis(150)).await;

    let fast = fast_profile();
    let mut opts = RunOptions::new("m")
        .with_max_concurrent(1)
        .with_on_saturated(Saturated::Reject);
    let seen: Arc<std::sync::Mutex<Vec<String>>> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = seen.clone();
    opts.on_error = Some(Arc::new(move |msg: &str| {
        sink.lock().unwrap().push(msg.to_string());
    }));

    let rejected = run(&fast, opts).await.unwrap();
    let holder = holder.await.unwrap();

    assert!(holder.error.is_empty(), "holder failed: {}", holder.error);
    assert!(
        rejected.error.contains(CONCURRENCY_LIMIT_MARKER),
        "expected the fixed marker, got: {:?}",
        rejected.error
    );
    assert_eq!(rejected.exit_code, 1);
    {
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 1, "on_error must fire once: {seen:?}");
    }

    // The slot frees when the holder settles: the same config runs again.
    let after = run(
        &fast,
        RunOptions::new("m")
            .with_max_concurrent(1)
            .with_on_saturated(Saturated::Reject),
    )
    .await
    .unwrap();
    assert!(after.error.is_empty(), "slot was not released: {}", after.error);
}

#[tokio::test]
async fn zero_cap_is_a_config_error_not_a_hang() {
    let err = run(
        &fast_profile(),
        RunOptions::new("m").with_max_concurrent(0),
    )
    .await;
    assert!(err.is_err(), "max_concurrent=0 must fail fast");
}

#[tokio::test]
async fn gate_is_shared_across_configs_in_flight() {
    // Config A (cap 2, reject) is saturated by two slow runs. A concurrent run
    // with a *different* config must not evict A's gate — a fresh gate for A
    // would report free slots and let a third process fan out.
    let slow = slow_profile(400);
    let mut holders = Vec::new();
    for _ in 0..2 {
        let slow = slow.clone();
        holders.push(tokio::spawn(async move {
            run(
                &slow,
                RunOptions::new("m")
                    .with_max_concurrent(2)
                    .with_on_saturated(Saturated::Reject),
            )
            .await
            .unwrap()
        }));
    }
    tokio::time::sleep(Duration::from_millis(150)).await;

    let fast = fast_profile();
    let other = run(&fast, RunOptions::new("m").with_max_concurrent(3)).await.unwrap();
    assert!(other.error.is_empty(), "other-config run failed: {}", other.error);

    let rejected = run(
        &fast,
        RunOptions::new("m")
            .with_max_concurrent(2)
            .with_on_saturated(Saturated::Reject),
    )
    .await
    .unwrap();
    assert!(
        rejected.error.contains(CONCURRENCY_LIMIT_MARKER),
        "config A's gate was replaced: {:?}",
        rejected.error
    );

    for h in holders {
        let r = h.await.unwrap();
        assert!(r.error.is_empty(), "holder failed: {}", r.error);
    }
}
