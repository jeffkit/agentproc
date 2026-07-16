# spec/conformance

Cross-implementation conformance fixtures for the AgentProc protocol (wire 0.4).

## What's here

- `cases.json` — single stdout lines paired with the expected
  `{kind, value[, role[, session_id]]}` classification. Each case is one NDJSON
  event line an agent might emit, plus what a conformant bridge must classify
  it as.
- `scenarios.json` — multi-line stdout sequences paired with the expected
  observable runner output (reply, session_id, error, exit_code, partials).
  Each scenario is a full agent turn (a sequence of NDJSON event lines),
  exercising interaction semantics that single-line cases can't: first
  non-empty `session_id`, error mid-stream, session-with-error, invalid-session
  handling, single-`result` body rules, partial-with-role, streaming vs
  one-shot, and legacy `session`/`text` events treated as malformed.
- `sdk.json` — SDK entry-point (`createProfile` / `create_profile`) scenarios.
  Each scenario drives the SDK entry as a subprocess: the harness writes a
  `{"type":"turn",...}` object to its stdin and runs a handler of a named
  `kind`, then asserts the exact NDJSON stdout lines and exit code. Covers
  return-string, return-`AgentResult`, return-`None` after `send_partial`,
  raised `ProtocolError`, `send_error`-then-return, partial-with-role, and
  sync handlers (return-string, bare `send_partial`) — pinning that both SDKs
  accept sync and async handlers. Guards the user-facing SDK contract — not
  just the runner internals — against cross-language drift. Output vocabulary:
  `partial` / `result` / `error` / `permission_request` (no `session` or
  `text` events).
- `hub_bridge.json` — shared hub bridge-engine (`_shared/stream_utils`)
  scenarios for Python and Node.
- `diagnostics.json` — shared `(pattern, hint)` table for the runner's
  post-mortem stderr diagnosis (the friendly "agent script not found" hints
  surfaced when the agent exits non-zero with no `{"type":"error"}` event).
  Both runners embed an identical copy of the rules (the file is not shipped
  with the npm/pypi package, so the runner cannot read it at runtime); the
  conformance tests assert the embedded copies match this file rule-for-rule
  and that each rule's `sample` produces the expected `hint`.

## Wire 0.4 in one paragraph

Every byte on the agent's stdin and stdout is NDJSON (one JSON object per
line). Input is a single `{"type":"turn",...}` line (message, session_id,
session_name, attachments, permission, protocol_version). Output is
a stream of typed events: `{"type":"partial","text":...[, "role":...]}`,
`{"type":"result","text":...[, "usage":...]}`,
`{"type":"error","message":...}`, and (when permission is on)
`{"type":"permission_request",...}` / `{"type":"permission_response",...}`.
Optional `session_id` may appear on stdout events; the bridge persists the
**first** non-empty value (early omit OK; SHOULD attach once known; conflicting
later value = keep first). There is no `{"type":"session"}` or
`{"type":"text"}`. Stateless agents omit `session_id` entirely. The reply body
comes from `result` / streaming `partial` rules (see the protocol spec).

## How it's used

Both reference SDKs run the same fixtures through their runners:

- `cases.json` → line classifiers:
  - Python: `sdk/python/tests/test_conformance.py` → `agentproc.runner.classify_line`
  - Node:   `sdk/node/src/conformance.test.js`    → `runner.classifyLine`
- `scenarios.json` → end-to-end `run()`:
  - Python: `sdk/python/tests/test_scenarios.py` → `agentproc.runner.run`
  - Node:   `sdk/node/src/scenarios.test.js`     → `runner.run`
- `sdk.json` → SDK entry points (subprocess):
  - Python: `sdk/python/tests/test_sdk.py` → spawns `tests/_sdk_harness.py` under `create_profile`
  - Node:   `sdk/node/src/sdk.test.js`     → spawns `src/sdk_harness.js` under `createProfile`
- `diagnostics.json` → stderr diagnosis table:
  - Python: `sdk/python/tests/test_diagnostics.py` → `agentproc.runner.diagnose_stderr_failure` + `STDERR_DIAGNOSTICS`
  - Node:   `sdk/node/src/diagnostics.test.js`     → `runner.diagnoseStderrFailure` + `STDERR_DIAGNOSTICS`

If the two disagree on any case or scenario, at least one of them fails. This
is the guardrail that keeps the Python and Node implementations honest
against the same spec text — for both single-line classification and full
multi-line turns.

## When to add a case or scenario

Whenever the spec's event vocabulary changes (new event `type`, new field,
new disambiguation rule), add a case to `cases.json` **before** changing
either implementation. The failing tests tell you what to fix; once both
pass, the two implementations are provably aligned on the new rule.

Whenever a spec change touches **multi-line** interaction semantics
(first-non-empty `session_id`, error's effect on partials, session preserved
across error, invalid-session handling, result-body assembly,
streaming/one-shot differences), add a scenario to `scenarios.json` instead.
Single-line cases can't catch these — the bug only shows up when several
lines interact in one turn.

## Event classification rule

Every stdout line is parsed as JSON. A conformant bridge classifies it as:

- `partial` — `{"type":"partial","text":<string>}`; `value` is the text
  (empty string if `text` is missing/not a string). If a string `role` field
  is present, the classification carries it as `role`. Optional `session_id`
  is recorded when present and non-empty.
- `result` — `{"type":"result","text":<string>}`; `value` is the text.
  Optional `session_id` / `usage`.
- `error` — `{"type":"error","message":<string>}`; `value` is the message.
  Optional `session_id`.
- `permission_request` — `{"type":"permission_request",...}`; `value` is the
  whole event object.
- `malformed` — anything else (non-JSON, non-object, no `type`, or an
  unknown `type` including legacy `session` / `text`); `value` is the raw
  line. Malformed lines are ignored (not appended to the reply body — there
  is no implicit body in 0.4).

There is no lenient fallback in 0.4: a line that is not a valid JSON object
event is `malformed` and dropped. The 0.2 `AGENT_PARTIAL:"hi"` decoding rules
no longer apply.

## CI

The `.github/workflows/test.yml` workflow has a dedicated `conformance` job
that runs both SDKs' conformance suites against this file. A divergence gets
its own red light there, separate from the per-SDK matrices. The regular
`test-node` / `test-python` jobs also include conformance as part of their
full suites.

## Format

```json
{
  "cases": [
    {"line": "<raw stdout line, no trailing newline — a JSON event>",
     "expect": {"kind": "partial|result|error|permission_request|malformed",
                "value": "<string|object>",
                "role": "<optional, only for partial with a role>"}}
  ]
}
```

`kind` matches the return shape of `classify_line` / `classifyLine` in both
SDKs. For `partial` and `result`, `value` is the `text` string; for `error` it
is the `message` string; for `permission_request` it is the whole event
object; for `malformed` it is the raw line. `role` is asserted only when
present and string-typed.

### scenarios.json format

```json
{
  "scenarios": [
    {
      "name": "<short description>",
      "lines": ["<NDJSON event line 1>", "<NDJSON event line 2>", "..."],
      "streaming": true,
      "expect": {
        "reply": "<body from result / streaming rules>",
        "session_id": "<first non-empty session_id, '' if none>",
        "error": "<error message, '' if none>",
        "exit_code": 0,
        "partials": ["<text delivered via on_partial>", "..."]
      }
    }
  ]
}
```

`lines` is the agent's full stdout for the turn, in order (each line a JSON
event). `expect` matches the runner's observable `RunResult` plus the
`partials` collected via the `on_partial` / `onPartial` callback. `streaming`
defaults to `true`; set `false` to exercise the one-shot path (where
`{"type":"partial"}` events are ignored and `partials` should be `[]`).

### sdk.json format

```json
{
  "scenarios": [
    {
      "name": "<short description>",
      "kind": "<handler kind the harness runs>",
      "turn": {"type":"turn","message":"...","session_id":"","session_name":"default","protocol_version":"0.4"},
      "expect": {
        "exit": 0,
        "stdout_lines": ["<exact NDJSON event line>", "..."]
      }
    }
  ]
}
```

`turn` is the input object the test writes to the SDK subprocess's stdin (one
NDJSON line). `stdout_lines` lists the exact NDJSON event lines the SDK must
emit (compact JSON, no spaces — both SDKs serialize compactly so the bytes
match). `exit` is the expected process exit code.
