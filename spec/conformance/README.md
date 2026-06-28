# spec/conformance

Cross-implementation conformance fixtures for the AgentProc protocol.

## What's here

- `cases.json` — input stdout lines paired with the expected `{kind, value}`
  classification. Each case is a single line an agent might emit, plus what a
  conformant bridge must classify it as.

## How it's used

Both reference SDKs run the same `cases.json` through their line classifiers:

- Python: `sdk/python/tests/test_conformance.py` → `agentproc.runner.classify_line`
- Node:   `sdk/node/src/conformance.test.js`    → `runner.classifyLine`

If the two disagree on any case, at least one of them fails. This is the
guardrail that keeps the Python and Node implementations honest against the
same spec text.

## When to add a case

Whenever the spec's line-recognition rules change (new sentinel, new
disambiguation rule, new lenient-mode behaviour), add a case to `cases.json`
**before** changing either implementation. The failing tests tell you what to
fix; once both pass, the two implementations are provably aligned on the new
rule.

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
