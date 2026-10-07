# spec/conformance

Cross-implementation conformance fixtures for the AgentProc protocol (wire 0.4).

## What's here

- `cases.json` — single stdout lines paired with the expected
  `{kind, value[, role[, session_id]]}` classification. Each case is one NDJSON
  event line an agent might emit, plus what a conformant bridge must classify
  it as. The same file also carries an `env_compose` section: table-driven
  cases for the three-layer child-env policy (see
  [`cases.json` `env_compose` format](#casesjson-env_compose-format)) — one
  fixture file, both runner internals. It also carries the permission-posture
  matrix for in-process executors: `auto_approve_flags` (the argv tokens that
  count as auto-approval) and `posture_cases` (per executor: which argv a turn
  must produce, and whether the runner must refuse to spawn at all). See
  [posture_cases format](#posture_cases-format). Finally it carries
  `partial_role_cases` — the value the runner passes as the `on_partial` /
  `onPartial` callback's second argument for one `partial` line each, which the
  `cases` classifications cannot observe. See
  [partial_role_cases format](#partial_role_cases-format).
- `scenarios.json` — multi-line stdout sequences paired with the expected
  observable runner output (reply, session_id, error, exit_code, partials).
  Each scenario is a full agent turn (a sequence of NDJSON event lines),
  exercising interaction semantics that single-line cases can't: first
  non-empty `session_id`, error mid-stream, session-with-error, invalid-session
  handling, single-`result` body rules, partial-with-role, streaming vs
  one-shot, and legacy `session`/`text` events treated as malformed. Also
  covers turn-level timing: a `budget_secs` expiry (exit code 124) and the
  no-budget regression (see `sleep_secs` / `profile_overrides` below).
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
- `executors.json` — executor (in-process) path scenarios (issue #12). Each
  scenario's `lines` are agent stdout NDJSON event **objects** (not pre-encoded
  strings) fed through a fake executor, and asserts the full RunResult:
  `{reply, session_id, error, exit_code, usage, partials}`. The fake parse
  rules are embedded in the `_comment` and replicated in each consumer test:
  `{type:"partial",text}` → `partial_text`; `{type:"result",text?,
  session_id?,usage?}` → `final_text` (text may be `""`) / `session_id` /
  `usage`; `{type:"error",message[,session_id[,usage]]}` → `error` /
  `session_id` / `usage`; any other type → ignored. Covers usage passthrough,
  streaming reply dedup, non-streaming final-only assembly,
  empty-reply-is-success, error mid-stream, usage arriving on a later event or
  on an `error` event, first-non-empty `usage`, missing / conflicting /
  invalid `session_id`, and second-`result` suppression.

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
- `cases.json` `env_compose` → shared child-env composition:
  - Python: `sdk/python/tests/test_conformance.py` → `agentproc.runner._compose_env`
  - Node:   `sdk/node/src/conformance.test.js`    → `runner.composeEnv`
- `cases.json` `partial_role_cases` → the `on_partial` / `onPartial` second argument:
  - Python: `sdk/python/tests/test_conformance.py` → `agentproc.runner.run` (fake bash agent)
  - Node:   `sdk/node/src/conformance.test.js`    → `runner.run` (fake bash agent)
- `scenarios.json` → end-to-end `run()`:
  - Python: `sdk/python/tests/test_scenarios.py` → `agentproc.runner.run`
  - Node:   `sdk/node/src/scenarios.test.js`     → `runner.run`
- `sdk.json` → SDK entry points (subprocess):
  - Python: `sdk/python/tests/test_sdk.py` → spawns `tests/_sdk_harness.py` under `create_profile`
  - Node:   `sdk/node/src/sdk.test.js`     → spawns `src/sdk_harness.js` under `createProfile`
- `diagnostics.json` → stderr diagnosis table:
  - Python: `sdk/python/tests/test_diagnostics.py` → `agentproc.runner.diagnose_stderr_failure` + `STDERR_DIAGNOSTICS`
  - Node:   `sdk/node/src/diagnostics.test.js`     → `runner.diagnoseStderrFailure` + `STDERR_DIAGNOSTICS`
- `cases.json` `posture_cases` → in-process executor permission posture:
  - Python: `sdk/python/tests/test_conformance.py` → `agentproc.runner.run`
  - Node:   `sdk/node/src/conformance.test.js`     → `runner.run`
  - Rust:   `sdk/rust/src/conformance.rs`          → executor `build_args` + `posture_refusal`
  Each driver also asserts that its SDK's embedded auto-approve flag list is
  equal, item for item, to `auto_approve_flags` — the matrix is driven from
  this file, so those lists must not drift.


- `executors.json` → executor-path `run_via_executor` / `runViaExecutor` / Rust `run` (in-process):
  - Python: `sdk/python/tests/test_conformance.py` → `agentproc.runner.run_via_executor` (fake printf-backed executor)
  - Node:   `sdk/node/src/conformance.test.js`     → `runner.runViaExecutor` (fake tmp-script executor)
  - Rust:   `sdk/rust/src/conformance.rs`         → `agentproc::run` with a registered fake executor (`--features executors`)

  All three consumers live in the language's existing conformance entry point,
  so each conformance job runs this fixture. Each of them asserts a minimum
  scenario count (13) as well as the per-scenario expectations, so a fixture
  that is emptied, renamed, or mis-pathed fails loudly instead of passing
  vacuously.

If two SDKs disagree on any case or scenario, at least one of them fails. This
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

Run-time-only semantics go to `scenarios.json` too: a per-turn time budget or
deadline expiring, partial forwarding around a kill, exit code 124. These are
observable in `run()`, not in any single line's classification — `cases.json`
carries `classify_line` / `classifyLine` cases and nothing else, so the
`budget_secs` / `deadline` feature deliberately adds no case there.

Whenever a spec change touches the **executor permission posture** (which argv
an executor builds for `permission: true`, which executors declare
`supportsPermission`, what the runner must refuse to spawn), add a
`posture_cases` entry instead. It pins the decision for every executor at once
and keeps the three SDKs from drifting apart on a security-relevant default.

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

### cases.json `env_compose` format

```json
{
  "env_compose": [
    {
      "profile_env": {"DECLARED": "${ALLOWED}", "BLOCKED": "${SECRET}"},
      "env_allowlist": ["ALLOWED"],
      "extra_env": {"EXTRA_FLAG": "extra-val", "DECLARED": "overridden"},
      "host_env": {"ALLOWED": "ok-val", "SECRET": "top-secret"},
      "expect_contains": {"DECLARED": "overridden", "BLOCKED": ""},
      "expect_absent": ["SECRET"]
    }
  ]
}
```

### cases.json `partial_role_cases` format

```json
{
  "partial_role_cases": [
    {
      "name": "<short description>",
      "line": "{\"type\":\"partial\",\"text\":\"...\",\"role\":\"...\"}",
      "expect_text": "<the chunk text>",
      "expect_role": "thinking|plan|null"
    }
  ]
}
```

Each case is one complete `partial` NDJSON line, run through a real spawn with
`streaming: true`; `expect_text` / `expect_role` are the two arguments the
runner must pass to `on_partial` / `onPartial` for that line. `null` means the
callback's second argument is `None` / `undefined` — the runner MUST NOT
synthesise `"output"` when the event carries no `role`. A string role is
forwarded **as-is**, including values outside `output` / `thinking` (the spec
says unknown values are forwarded as-is), which is what pins all three SDKs to
the same passthrough rule. The executor path has no `role` on its
`ParseResult`, so it always passes `null`; these cases cover the spawn path
only.

### posture_cases format

```json
{
  "auto_approve_flags": ["--dangerously-skip-permissions", "--yolo", "..."],
  "posture_cases": [
    {
      "name": "<short description>",
      "executor": "<executor name>",
      "permission": true,
      "env": {"AGENTPROC_AUTO_APPROVE": "0"},
      "expect": {
        "error": true,
        "refused": true,
        "exit_zero": false,
        "reply": "ok",
        "argv_contains": ["<argv token that must be present>"],
        "argv_excludes": ["<argv token that must be absent>"]
      }

    }
  ]
}
```

`host_env` fakes the bridge's own environment — it is both the `${VAR}`
expansion source and the environment the infra set is copied from (Python
monkeypatches `os.environ` and deletes everything not listed; Node passes it
as `composeEnv`'s `sourceEnv`). `extra_env` goes in as
`RunOptions.extra_env` / `extraEnv` (the CLI `--env` flag). The composed
result must be the spec's three layers in order: infra set → profile `env`
(expanded, `env_allowlist`-filtered — a blocked name expands to the empty
string but the key is still set) → `extra_env` (later layers override
earlier). `expect_contains` lists exact key/value pairs that MUST be present
and `expect_absent` lists names that MUST NOT appear at all — the latter is
what proves no `{**host_env}` passthrough survives.

`executor` names an in-process executor; the runner is driven with a profile
`{executor, permission?}` and a fake CLI on `PATH` that records its own argv,
so the case asserts observable behaviour rather than internal signatures.
`permission` is omitted when the profile does not declare the field at all
(which is not the same as `false`). `env` is written both to the runner
process environment and to the per-run env extras, so the value reaches
`AGENTPROC_AUTO_APPROVE` either way.

`expect.error` (`true` = the turn must fail) and `expect.exit_zero` (the
opposite) are mutually exclusive; `reply` is the expected reply body.
`argv_contains` / `argv_excludes` are checked against the argument list the
CLI actually received.

`expect.refused` (`true` = the runner must never spawn the CLI) is the only
field that distinguishes a posture refusal from any other failure: the driver
MUST assert that the fake CLI's argv file does not exist, because a failing
turn can also come from an unrecognised stdout body. When `refused` is set the
token lists are not checked — no argv exists to check. The Python and Node
drivers assert `refused` for every refusal case; the Rust driver asserts the
refusal decision and the argv of a non-refused case only (`reply` /
`exit_zero` need a real spawn and are likewise covered by the Python and Node
drivers).

`auto_approve_flags` is the single source of truth for "this argv token means
auto-approve". Every SDK embeds the same list (the file is not shipped with
the packages) and the conformance drivers assert equality with it.

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

Two optional keys extend a scenario beyond "print these lines and exit":

- `sleep_secs` — the generated agent sleeps this many seconds after printing
  its lines (before exiting), so the harness can exercise a turn that outlives
  its time limit. Both harnesses emit a `sleep <n>` line last.
- `profile_overrides` — extra profile fields merged into the generated
  profile. Used by the timing scenarios, e.g.
  `{"budget_secs": 1, "kill_grace_secs": 1}` to make a per-turn budget expire
  against a sleeping agent.

`expect.partials_any_of` is the timing counterpart of `partials`: it lists
candidate `partials` sequences and passes if the observed one matches any of
them. Timing scenarios use it because whether the agent's first line reaches
the bridge before the budget fires is a scheduling race —
`[[], ["started"]]` accepts either but still pins that nothing else was
forwarded. Scenario shape is otherwise unchanged.

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
