'use strict';
/**
 * Concurrency primitives (issue #17 / spec "Concurrency" section).
 *
 * - SessionSerializer: serialize concurrent runs sharing a key (e.g. a session
 *   id) — the second run starts only after the first settles; two agent
 *   processes never concurrently resume one session. A `null` key is a no-op
 *   (runs without a key are unaffected).
 * - ConcurrencyGate: per-runner global cap on concurrent agent processes.
 *   Default unlimited (backward compatible). Burst semantics are an explicit
 *   choice: 'queue' (default, FIFO) or 'reject' — immediate terminal error
 *   containing the fixed marker `agentproc: concurrency limit`.
 *
 * The gate is evaluated before spawn; no new wire event types are introduced.
 */

const CONCURRENCY_LIMIT_MARKER = 'agentproc: concurrency limit';

class ConcurrencyLimitError extends Error {
  constructor(maxConcurrent) {
    super(
      `${CONCURRENCY_LIMIT_MARKER}: maxConcurrent=${maxConcurrent} ` +
      `reached; turn rejected (onSaturated=reject)`
    );
    this.name = 'ConcurrencyLimitError';
    this.maxConcurrent = maxConcurrent;
  }
}

class SessionSerializer {
  constructor() {
    this._tails = new Map(); // key -> Promise chain tail
  }

  /**
   * Serialize `fn` (async) under `key`. Returns fn's promise.
   * `key == null` runs fn immediately (no serialization).
   */
  run(key, fn) {
    if (key == null) return fn();
    const prev = this._tails.get(key) || Promise.resolve();
    const next = prev.then(fn, fn); // prior failure must not block the queue
    this._tails.set(key, next.catch(() => {}));
    return next;
  }
}

class ConcurrencyGate {
  /**
   * @param {?number} maxConcurrent - null/undefined = unlimited
   * @param {'queue'|'reject'} onSaturated - burst semantics, default 'queue'
   */
  constructor(maxConcurrent, onSaturated = 'queue') {
    if (onSaturated !== 'queue' && onSaturated !== 'reject') {
      throw new Error(`onSaturated must be 'queue' or 'reject', got ${JSON.stringify(onSaturated)}`);
    }
    if (maxConcurrent != null && (!Number.isInteger(maxConcurrent) || maxConcurrent < 1)) {
      throw new Error(`maxConcurrent must be an integer >= 1 or null, got ${JSON.stringify(maxConcurrent)}`);
    }
    this.maxConcurrent = maxConcurrent;
    this.onSaturated = onSaturated;
    this._active = 0;
    this._waiters = [];
  }

  async acquire() {
    if (this.maxConcurrent == null) return;
    if (this._active < this.maxConcurrent) {
      this._active++;
      return;
    }
    if (this.onSaturated === 'reject') {
      throw new ConcurrencyLimitError(this.maxConcurrent);
    }
    await new Promise((resolve) => this._waiters.push(resolve)); // FIFO
    this._active++;
  }

  release() {
    if (this.maxConcurrent == null) return;
    this._active = Math.max(0, this._active - 1);
    const next = this._waiters.shift();
    if (next) next();
  }

  /**
   * Run `fn` (async) holding one concurrency slot.
   */
  async withSlot(fn) {
    await this.acquire();
    try {
      return await fn();
    } finally {
      this.release();
    }
  }
}

module.exports = {
  CONCURRENCY_LIMIT_MARKER,
  ConcurrencyLimitError,
  SessionSerializer,
  ConcurrencyGate,
};
