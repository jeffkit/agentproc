# spec/conformance

Cross-implementation conformance fixtures for the AgentProc protocol.

## What's here

- `cases.json` — input stdout lines paired with the expected `{kind, value}`
  classification. Each case is a single line an agent might emit, plus what a
  conformant bridge must classify it as.
- `scenarios.json` — multi-line stdout sequences paired with the expected
  observable runner output (reply, session_id, error, exit_code, partials).
  Each scenario is a full agent turn (a sequence of stdout lines), exercising
  interaction semantics that single-line cases can't: last-wins, error
  mid-stream, session-with-error, invalid-session handling, streaming vs
  one-shot.

## How it's used

Both reference SDKs run the same fixtures through their runners:

- `cases.json` → line classifiers:
  - Python: `sdk/python/tests/test_conformance.py` → `agentproc.runner.classify_line`
  - Node:   `sdk/node/src/conformance.test.js`    → `runner.classifyLine`
- `scenarios.json` → end-to-end `run()`:
  - Python: `sdk/python/tests/test_scenarios.py` → `agentproc.runner.run`
  - Node:   `sdk/node/src/scenarios.test.js`     → `runner.run`

If the two disagree on any case or scenario, at least one of them fails. This
is the guardrail that keeps the Python and Node implementations honest
against the same spec text — for both single-line classification and full
multi-line turns.

## When to add a case or scenario

Whenever the spec's line-recognition rules change (new sentinel, new
disambiguation rule, new lenient-mode behaviour), add a case to `cases.json`
**before** changing either implementation. The failing tests tell you what to
fix; once both pass, the two implementations are provably aligned on the new
rule.

Whenever a spec change touches **multi-line** interaction semantics
(last-wins ordering, error's effect on partials, session preserved across
error, invalid-session handling, streaming/one-shot differences), add a
scenario to `scenarios.json` instead. Single-line cases can't catch these —
the bug only shows up when several lines interact in one turn.

## Payload decoding rule (partial / error)

The value after `AGENT_PARTIAL:` / `AGENT_ERROR:` is text meant for the user.
The JSON-string encoding exists only to safely carry newlines and quotes —
it is **not** a channel for structured data. Decoders MUST apply this rule:

1. Empty payload → `""`.
2. Valid JSON **string** → the decoded string (`AGENT_PARTIAL:"hi"` → `hi`).
3. Valid JSON that is **not** a string (number, bool, null, array, object) →
   fall back to the raw text verbatim (`AGENT_PARTIAL:true` → `true`,
   `AGENT_PARTIAL:[1,2]` → `[1,2]`). Rationale: `str(True)` and `String(true)`
   differ across runtimes; the raw text is the only language-independent
   answer, and an agent emitting non-string JSON here has misused the API.
4. Invalid JSON (bare text) → the raw text verbatim (lenient mode).

Any new case in `cases.json` that exercises rule 3 is itself the regression
test for this rule — both SDKs must agree, and a change to either
`decode_json_value` / `decodeJsonValue` that breaks rule 3 will turn a case
red.

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
    {"line": "<raw stdout line, no trailing newline>",
     "expect": {"kind": "session|partial|error|body", "value": "<string>"}}
  ]
}
```

`kind` is one of `session`, `partial`, `error`, `body` — matching the return
shape of `classify_line` / `classifyLine` in both SDKs. For `partial` and
`error`, `value` is the **decoded** payload per the payload decoding rule
above; for `session` it is the stripped id; for `body` it is the raw line.

### scenarios.json format

```json
{
  "scenarios": [
    {
      "name": "<short description>",
      "lines": ["<stdout line 1>", "<stdout line 2>", "..."],
      "streaming": true,
      "expect": {
        "reply": "<concatenated non-protocol lines>",
        "session_id": "<final session id, '' if none>",
        "error": "<decoded AGENT_ERROR payload, '' if none>",
        "exit_code": 0,
        "partials": ["<chunk delivered via on_partial>", "..."]
      }
    }
  ]
}
```

`lines` is the agent's full stdout for the turn, in order. `expect` matches
the runner's observable `RunResult` plus the `partials` collected via the
`on_partial` / `onPartial` callback. `streaming` defaults to `true`; set
`false` to exercise the one-shot path (where `AGENT_PARTIAL:` lines are
ignored and `partials` should be `[]`).
