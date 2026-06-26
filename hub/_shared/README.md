# hub/_shared

Shared bridge utilities used by the NDJSON-based hub profiles. The Python and
Node implementations stay at parity: same input contract, same output contract.

## What it does

The shared module handles the parts that are identical across every NDJSON-speaking CLI:

- subprocess spawn + `FileNotFoundError` → `AGENT_ERROR:`
- line-by-line stdout reading + JSON decoding
- partial / final / session / error classification
- streaming vs non-streaming emission policy (with dedup)
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

That keeps each bridge under ~50 lines. See `gemini-cli/bridge.py` for a minimal example.

`parse_event` may be a closure with internal state. The `cursor` bridge uses this to track accumulated streamed text and suppress Cursor's duplicate "full assembled" event that follows the delta stream — see `cursor/bridge.py`'s `make_parse_event()`.

## When NOT to use it

CLIs that emit plain text rather than NDJSON (e.g. `agy`, `echo-agent`) do not fit this abstraction. They have bespoke bridges that call `subprocess.run` directly. That is fine — `stream_utils` is an opt-in helper, not a mandatory base class.

## Files

| File | Purpose |
|------|---------|
| `stream_utils.py` | Python: `EventResult` dataclass + `run_bridge()` / `main_entry()` |
| `stream_utils.js` | Node: `runBridge()` + emit helpers |

## Design note: `partial_text` vs `final_text`

The shared module distinguishes two text channels:

- `partial_text` — incremental chunks emitted mid-turn as `AGENT_PARTIAL:` (streaming only)
- `final_text` — terminal text, emitted as reply body in non-streaming mode (or as fallback when no partials were streamed)

This split prevents the duplicate-emission bug seen when a CLI streams partial chunks AND THEN emits a `result` event containing the full assembled text. Without the split, the user receives the text twice (once as partials, once as final). See `claude-code/bridge.py` for the canonical `result.result → final_text` mapping.
