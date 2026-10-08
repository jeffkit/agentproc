# Concurrency

A bridge is long-lived: turns arrive whenever users type, and nothing stops two users (or one impatient user double-tapping) from arriving at the same moment. The protocol is one-turn-per-process, so by default each turn forks a fresh agent process — a burst fans out processes until memory runs out.

The runner exposes two opt-in primitives for this. Both are runner-side orchestration: **no new wire events**, and the CLI is unaffected. See the [Concurrency section](/spec/) of the spec for the normative rules.

## Per-session serialization

Give a turn a session key and same-key turns run **one at a time**, in arrival order. This is the fix for the worst failure mode: two agent processes concurrently `--resume`-ing one session for the same key.

```python
# Python — agentproc.runner
from agentproc.runner import run, RunOptions

run(profile, RunOptions(message=text, session_key=ctx.session_id))
```

```js
// Node.js — agentproc/src/runner
const { run } = require('agentproc/src/runner');

await run(profile, { message: text, sessionKey: ctx.sessionId });
```

```rust
// Rust
run(&profile, RunOptions::new(text).with_session_key(&ctx.session_id)).await?;
```

Use the session id you would otherwise pass as `session_id`: it is the identity that must not be resumed twice concurrently. Turns with no key are unaffected — serialization only applies when you provide one.

The same key also protects the session's history file. A handler that calls `append_history` / `appendHistory` for `~/.agentproc/sessions/<id>.jsonl` writes without any file lock of its own, so two turns of one session running at once can interleave lines — keying by session id is what keeps those appends in order.

## Global concurrency limit

`max_concurrent` / `maxConcurrent` caps how many agent processes a runner instance runs at once (default: unlimited). When the cap is reached you choose the burst behaviour explicitly:

| `on_saturated` / `onSaturated` | Behaviour |
|--------------------------------|-----------|
| `"queue"` (default) | The excess turn waits in FIFO order, then runs normally. |
| `"reject"` | The turn is terminated immediately with an `error` containing the fixed marker `agentproc: concurrency limit` — no process is spawned. |

```python
run(profile, RunOptions(
    message=text,
    session_key=session_id,
    max_concurrent=4,
    on_saturated="reject",   # "queue" to absorb the peak instead
))
```

Queue for chat traffic you would rather delay than drop; reject when the caller can retry (or when a bridge wants to tell the user "slow down") and you would rather shed load than grow the queue.

Rejections are terminal results, not exceptions:

| SDK | Rejection surfaces as |
|-----|-----------------------|
| Python | `RunResult.error` containing the marker; `on_error` also fires |
| Node.js | `result.error` containing the marker; `onError` also fires |
| Rust | `RunResult.error` containing the marker; `on_error` also fires |

Match on the marker rather than the surrounding wording — it is fixed across all three SDKs and both burst modes share the same `error` channel used by every other agent failure.

## What is still yours to decide

The SDK decides *when* a turn runs; the bridge still decides what to do with the outcome. A rejected turn is a normal `error` result: retry it, tell the user, or drop it — and if you want per-user fairness beyond a global cap, keep that policy in the bridge and hand the runner a session key plus a cap.
