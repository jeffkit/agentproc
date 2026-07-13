# hub/_shared

Shared bridge utilities used by the hub profiles (wire 0.3). The Python and
Node implementations stay at parity: same input contract, same output contract.

## What it does

The shared module handles the parts that are identical across every
NDJSON-speaking CLI:

- reading the `{"type":"turn",...}` object from stdin (one NDJSON line)
- subprocess spawn + `FileNotFoundError` → `{"type":"error"}`
- line-by-line stdout reading + JSON decoding
- partial / final / session / error classification → NDJSON events
- always-emit-partial policy (the runner forwards `{"type":"partial"}` only
  when the profile's `streaming` is true) + dedup
- exit-code mapping + stderr capture

A bridge that wraps an NDJSON-speaking CLI only needs to provide two functions:

```python
# Python
def build_args(message: str, session_id: str, env) -> list[str]: ...
def parse_event(event: dict) -> EventResult | None: ...
```

```js
// Node
function buildArgs(message, sessionId, env) { return [...]; }
function parseEvent(event) { return { partialText?, finalText?, sessionId?, error? } | null; }
```

`run_bridge` / `runBridge` then: read the turn from stdin, spawn the CLI built
by `build_args`, call `parse_event` on each stdout NDJSON line, and emit
AgentProc NDJSON events on stdout. A final `{"type":"text"}` event is always
emitted at the end (the `final_text`, or the last `partial_text` as fallback) —
that is the reply body.

That keeps each bridge under ~50 lines. See `gemini-cli/bridge.py` for a
minimal example.

`parse_event` may be a closure with internal state. The `cursor` bridge uses
this to track accumulated streamed text and suppress Cursor's duplicate "full
assembled" event that follows the delta stream — see `cursor/bridge.py`'s
`make_parse_event()`.

## Plain-text one-shot CLIs

CLIs that return the full reply as plain stdout (no streaming, no session id) —
`aider`, `pi`, `deepseek`, `agy` — use `run_plain_cli` / `runPlainCli` instead.
It reads the turn, runs the CLI with a timeout, and emits the trimmed stdout as
a single `{"type":"text"}` event (or `{"type":"error"}` on failure). The bridge
only supplies `build_args(message)`.

## When NOT to use it

CLIs that need cross-turn transcript state the shared helper does not model
(e.g. `recursive`) keep a bespoke bridge. That is fine — `stream_utils` is an
opt-in helper, not a mandatory base class.

## Files

| File | Purpose |
|------|---------|
| `stream_utils.py` | Python: `EventResult` dataclass + `run_bridge()` / `main_entry()` / `run_plain_cli()` + emit helpers + `read_turn()` |
| `stream_utils.js` | Node: `runBridge()` / `runPlainCli()` + emit helpers + `readTurn()` |

## Design note: `partial_text` vs `final_text`

The shared module distinguishes two text channels:

- `partial_text` — incremental chunks emitted mid-turn as `{"type":"partial"}`
  (the bridge always emits these; the runner forwards them only when the
  profile's `streaming` is true).
- `final_text` — terminal text, emitted as the final `{"type":"text"}` reply
  event (used as-is when present, otherwise the last `partial_text` is the
  fallback).

This split prevents the duplicate-emission bug seen when a CLI streams partial
chunks AND THEN emits a `result` event containing the full assembled text.
Without the split, the user receives the text twice (once as partials, once as
the reply). See `claude-code/bridge.py` for the canonical
`result.result → final_text` mapping.
