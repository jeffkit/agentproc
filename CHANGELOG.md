# Changelog

All notable changes to AgentProc are documented here. Three version tracks are kept independent:

- **Wire protocol** — the string injected as `AGENT_PROTOCOL_VERSION`. Currently `0.2`. Only changes when bytes on stdin/stdout change.
- **Spec document revision** — editorial changes to `spec/protocol.md`. Currently `0.8`. Does not change the wire contract.
- **SDK package version** — `sdk/python/pyproject.toml` and `sdk/node/package.json`. Currently `0.6.0`. Includes runner/CLI/SDK behaviour changes.

## Unreleased

### Spec / SDK 0.6.0 — wire `0.2`: optional tool permission channel

Adds an **opt-in** mid-turn tool-authorization channel so IM bridges can replace `--dangerously-skip-permissions` / `--yolo` when the underlying CLI supports stdio approval (e.g. Claude Code `--permission-prompt-tool stdio`).

- **Not general HIL.** Clarifying questions stay in the reply body / next IM turn. Disabling questionnaire tools such as `AskUserQuestion` remains recommended. This channel is only for allow/deny before a tool runs.
- **Profile.** `permission: false` (default) — unchanged behaviour. `permission: true` → inject `AGENT_PERMISSION=1`, keep stdin open for the turn, honor permission frames.
- **Wire.** stdout `AGENT_PERMISSION_REQUEST:<json-object>`; stdin `AGENT_PERMISSION_RESPONSE:<json-object>` (`request_id`, `behavior: allow|deny`, optional `updated_input` / `message`).
- **Opt-in only.** Agents/CLIs without a control-request style prompt keep using auto-approve flags. Bridges that do not implement the channel ignore it.
- **Versions.** Wire protocol `0.1` → `0.2` (new line prefixes + mid-turn stdin frames). Spec document revision `0.8`. SDK packages `0.6.0` (Python + Node) so `PROTOCOL_VERSION` matches the wire string.
- **This PR scope.** Spec (EN + ZH), docs quickref, `PROTOCOL_VERSION` constant + tests. Runner recognition / Hub claude-code translation land in follow-ups.

---

### fix: runner SIGKILL timer leak + streaming `max_reply_chars` now enforced

Two correctness fixes to the bridge-side runner (both Node and Python, where applicable):

**1. runner.js — SIGKILL timer handle saved and cleared on process exit**

The SIGKILL follow-up `setTimeout` (fired `kill_grace_secs` after the initial SIGTERM) was a fire-and-forget: its handle was never saved, so it could not be cancelled. If the agent exited during the grace period (e.g. it responded cleanly to SIGTERM), the timer fired against an already-dead process — suppressed by `try/catch` but leaving a dangling handle that prevented Node from exiting cleanly in test/script contexts. `killTimer` is now declared in the outer scope alongside `timer`, and both are cleared in the `close` handler.

**2. `max_reply_chars` now enforced in streaming mode**

Previously `max_reply_chars` (default 8000) only truncated the final reply body in non-streaming mode. A streaming agent that forwarded everything via `AGENT_PARTIAL:` could deliver an unlimited amount of text to the platform. The two runners now track the cumulative length of forwarded partial chunks; once the total reaches `max_reply_chars`, a truncation notice (`truncation_suffix`) is emitted and further partials from the same turn are suppressed. Non-streaming truncation is unchanged.

- **Spec.** `spec/protocol.md` (EN + ZH) adds a `max_reply_chars` section under Reply body describing both-mode semantics. Table comment updated to "truncate at this length (body + streaming partials)".
- **Conformance.** Three new `scenarios.json` scenarios cover non-streaming truncation, mid-chunk streaming truncation, and first-chunk-exceeds-cap; `scenarios.test.js` and `test_scenarios.py` both support the new `profile_overrides` field so scenarios can set per-test `max_reply_chars`.
- **Wire protocol stays `0.1`.** Both fixes are bridge-side only; no change to the stdout line format.

**3. `AGENT_PROTOCOL_VERSION` clarified as diagnostics-only**

The env-var table comment now states that the variable's only practical use is logging and diagnostics — it carries no negotiation or feature-detection semantics. Both spec mirrors (EN + ZH) updated.

---

### SDK 0.5.2 — security: `env_allowlist` is now a real trust boundary

When a profile declares `env_allowlist`, the agent process **no longer inherits the bridge's full environment**. Its environment is now built from: a minimal infra set (`PATH`/`HOME`/`USER`/`SHELL`/`LANG`/`TERM`/`TMPDIR`/`PWD`/…, plus Windows `SystemRoot`/`TEMP`/`USERPROFILE`/…) + the profile `env` block (allowlist-filtered) + the injected `AGENT_*` vars + CLI `--env` extras.

- **Why.** Pre-0.5.2 the child inherited `process.env` / `os.environ` wholesale, so any secret the bridge happened to hold (cloud tokens, `AWS_SECRET_ACCESS_KEY`, …) reached the agent regardless of `env_allowlist`. The spec marketed `env_allowlist` as "shrinking the trust boundary", but the boundary still leaked through inheritance — the allowlist was a cosmetic filter on `${VAR}` expansion, not a boundary. A profile author setting `env_allowlist: [ANTHROPIC_API_KEY]` could not actually prove the agent only saw `ANTHROPIC_API_KEY`.
- **What changes.** With `env_allowlist` present, undeclared bridge env vars no longer reach the agent. `${VAR}` blocking and the stderr warning are unchanged. `env_allowlist` absent keeps the back-compat full-inheritance behaviour, so existing profiles that don't set the field are unaffected.
- **Infra set.** Curated, non-credential operational vars the agent needs to find its interpreter / temp dir / locale. A profile needing an additional non-secret var must declare it in `env` and list it in `env_allowlist`.
- **Hub profile impact.** Every hub bridge reads its config knobs (`CLAUDE_MODEL`, `AGY_TIMEOUT`, `CODEBUDDY_DISALLOW_TOOLS`, `QWEN_SANDBOX`, …) from `os.environ` with safe defaults, and the API keys they need (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) are already declared in each profile's `env` block and listed in its `env_allowlist`, so hub profiles keep working. The one behavioural change: a knob the bridge reads directly from `os.environ` but the profile never declares (e.g. `AGY_TIMEOUT` set in the user's shell) no longer reaches the bridge — it falls back to its default. To override such a knob, declare it in the profile's `env` block (`AGY_TIMEOUT: "${AGY_TIMEOUT}"`) and add it to `env_allowlist`. This is the documented "uncomment to use" model, now actually enforced.
- **Spec.** `spec/protocol.md` doc revision bumped to `0.7` (EN + ZH mirror) describing the inheritance rule and enumerating the infra set. Wire protocol stays `0.1`.
- **Tests.** New `env_allowlist stops undeclared secrets from leaking via inheritance` + `env_allowlist absent → child still inherits full process.env` on both runners, pinning both the new boundary and the back-compat path.

### SDK 0.5.2 — Python YAML parser replaced by PyYAML

The hand-rolled YAML subset parser in `sdk/python/src/agentproc/cli.py` (~138 lines) is replaced by a thin `PyYAML` wrapper at `sdk/python/src/agentproc/yaml.py`. PyYAML becomes a runtime dependency (`PyYAML>=5.1`). The Node SDK already used `js-yaml`; the two SDKs are now at parity on YAML parsing.

- **Why.** The hand-rolled parser did not strip inline `#` comments, so `streaming: false # one-shot mode` parsed as the string `"false # one-shot mode"` and the runner's `is not False` check left streaming **on** — the exact bug that retired the Node SDK's hand-rolled parser in 0.5.1, now fixed on the Python side too. It also mis-handled tab indentation, hard-coded 2-space block scalars, and didn't understand quoted commas in flow sequences. This was a direct violation of `AGENTS.md`'s "Don't hand-roll a YAML parser" rule.
- **Public API unchanged.** `parse_yaml` keeps the same signature and is still importable from `agentproc.cli` (re-exported) for the `hub` module and external callers. `parse_yaml_simple` alias retained.
- **Tests.** New `sdk/python/tests/test_yaml.py` pins the previously-broken behaviours: inline comments stripped, empty `env:` value is `null` (not `""`), block scalars, flow sequences, nested maps, JSON input.

### SDK 0.5.2 — SDK entry-point conformance suite

New `spec/conformance/sdk.json` fixture + harnesses extend cross-implementation conformance coverage to the SDK entry points (`create_profile` / `createProfile`), not just the runner internals.

- **Why.** The existing conformance suite tested `classify_line` and `run()` — the bridge side. The user-facing SDK entry (return types, `send_partial` / `send_error` semantics, `ProtocolError` mapping, exit codes) had **no** cross-language guardrail, so the two SDKs could drift on exactly the surface users touch.
- **What it covers.** Five scenarios, run as subprocesses on both SDKs with identical `AGENT_*` env: async handler returning a string; returning an `AgentResult` (response + session_id); returning `None` after `send_partial`; raising `ProtocolError`; calling `send_error` then returning a body (pins that `send_error` is non-terminal in both SDKs). Each asserts exact stdout lines + exit code.
- **Known divergence pinned, not fixed.** Python requires an `async` handler (`asyncio.run`); Node accepts sync or async. The shared fixture uses async handlers so both pass; the sync/async divergence is documented in the fixture and left as future parity work rather than silently ignored.
- **Files.** `spec/conformance/sdk.json`, `sdk/node/src/sdk_harness.js` + `sdk/node/src/sdk.test.js` (added to `npm test`), `sdk/python/tests/_sdk_harness.py` + `sdk/python/tests/test_sdk.py`.

### SDK 0.5.2 — fix: `session_file_path` / `sessionFilePath` false positive on `..`-containing ids

The defense-in-depth guard added in 0.5.1 rejected any id *containing* `..` (Node `sessionId.includes('..')`, Python `".." in session_id`), which falsely rejected legitimate ids like `a..b` — a valid spec-compliant session id (the charset allows `.`) whose filename `a..b.jsonl` does not traverse. The guard is now unified with the runner's single source of truth: an id is rejected iff it fails `is_valid_session_id` (charset: non-empty, no whitespace / control / colon / path separators) **or** is exactly `.` / `..` (which pass the charset but do traverse). Both SDKs now import the validator from their runner instead of re-hand-rolling separator/`..` checks.

- **Why.** The 0.5.1 guard conflated "contains `..`" with "is a traversal". Only the exact components `.` and `..` traverse; `a..b` is a normal filename. The runner-side `SESSION_ID_RE` already excludes every path separator, so the entry-point check only needed to add the two literal components the regex can't catch.
- **Tests.** New `accepts legitimate ids that contain '..'` / `test_accepts_dot_dot_inside_id` pin `a..b` as accepted on both SDKs; the existing traversal cases (`a/b`, `a\b`, `..`, `../../tmp/x`) still throw.
- **Wire protocol stays `0.1`.** Entry-point helper behaviour only; no change to the `AGENT_SESSION:` line format or the bridge-side charset.

### SDK 0.5.2 — Python `create_profile` accepts sync handlers (parity with Node)

The Python SDK entry point no longer requires an `async` handler. `create_profile` now calls the handler and inspects the return: if it is a coroutine (an `async def` handler, or a sync one returning a coroutine) it is awaited via `asyncio.run`; otherwise the plain return value is used directly. This mirrors the Node SDK, which accepts sync or async via `Promise.resolve().then(() => handler(ctx))`. The known sync/async divergence pinned in the `sdk.json` fixture is now resolved.

`AgentContext.send_partial` / `send_error` are no longer `async def` — they now write the `AGENT_PARTIAL:` / `AGENT_ERROR:` line at **call time** and return a no-op awaitable. This keeps `await ctx.send_partial(...)` working in async handlers (the await is a no-op) while letting a sync handler call `ctx.send_partial(...)` bare and still emit the chunk — previously a bare call returned a never-awaited coroutine and silently dropped the line.

- **Why.** The two SDKs are meant to agree on observable behaviour at the user-facing surface. Node accepting sync but Python rejecting it was a parity gap exactly where users touch the SDK. The `async def` `send_partial`/`send_error` also made sync handlers a trap (bare call → dropped chunk + "coroutine was never awaited" warning), so supporting sync handlers required making those writes eager.
- **Conformance.** `spec/conformance/sdk.json` adds `sync-returns-string` and `sync-uses-send-partial-bare` scenarios, run against both SDKs, pinning that a sync handler returning a string and a sync handler calling `send_partial` bare both produce the expected stdout + exit code on both implementations. The fixture description no longer claims Python requires async.
- **Back compat.** Existing async handlers (`async def handler` + `await ctx.send_partial(...)`) behave identically — the write still happens at the await expression's call step, and the awaited value is `None`.
- **Wire protocol stays `0.1`.** SDK entry-point behaviour only; the stdout line format is unchanged.

### SDK 0.5.2 — runner stderr buffer bounded + diagnosis table shared

The runner's `stderr_full` / `stderrFull` capture — used for post-mortem "agent script not found" hints — was unbounded, so a noisy or hostile agent could grow it without limit. It is now a **1 MB head cap**: the first 1 MB of stderr is retained (the diagnostic patterns target interpreter-startup errors — `can't open file`, `Cannot find module`, `bash: line N: ...: No such file or directory`, generic `ENOENT` — which land in the initial bytes), and beyond the cap the tail is dropped with a one-shot `[agentproc runner] stderr capped at 1 MB; trailing output dropped` marker. The 8 KB sliding `stderr_window` / `stderrWindow` for UI display is unchanged.

The four hand-rolled diagnostic regexes (duplicated in `runner.js` and `runner.py`, each requiring wording parity by hand) are now driven by a single source of truth at `spec/conformance/diagnostics.json` — an ordered `(id, pattern, flags, hint, sample, expect)` table. Each runner embeds an identical copy (the file is not shipped with the npm/pypi package, so the runner cannot read it at runtime); new conformance tests (`sdk/node/src/diagnostics.test.js`, `sdk/python/tests/test_diagnostics.py`) assert the embedded copies match the fixture rule-for-rule and that each `sample` produces the expected `hint` on both SDKs. A `{n}` token in a hint is replaced by capture group `n`; `{{PROFILE_DIR}}` stays literal.

- **Why.** An unbounded stderr buffer is a memory-exhaustion vector, and two hand-maintained copies of the diagnostic regexes silently drifted risk (a hint improved in one SDK would miss the other). The shared table + conformance test make parity provable; the head cap bounds memory while keeping the high-value startup-error signal.
- **Tests.** New `stderr diagnosis survives a >1 MB noisy stderr (head cap keeps early signal)` on both runners spawns an agent that writes the `can't open file` line to stderr followed by >2 MB of noise and asserts the friendly hint still fires — pinning both the cap and that early signal survives it.
- **Wire protocol stays `0.1`.** Runner-internal diagnostics only; no change to stdout line format or exit codes.

### SDK 0.5.2 — hub: cross-language parity test for the `recursive` bridge

`hub/recursive/bridge.py` and `bridge.js` are bespoke — they share no code with the `_shared/stream_utils` helper the other NDJSON profiles use, because recursive needs cross-turn transcript state that helper doesn't model — so their observable-parity claim (`hub/README.md`: "both produce identical AgentProc output") had no automated guardrail. A new cross-language parity fixture at `hub/recursive/tests/parity.json` drives both bridges' pure helpers through identical cases: argument building from `RECURSIVE_*` / `AGENT_STREAMING` env (`provider_args`/`providerArgs`, `_global_args`/`globalArgs`), the `session: recording to <dir>` stderr parse (`extract_session_dir`/`extractSessionDir`), and the last-assistant-turn transcript read (`_last_assistant_text`/`lastAssistantText`). `bridge.js` gained a `require.main === module` guard (mirroring `bridge.py`'s `__main__` guard) so it is importable by the Node test without running the bridge.

- **Note on the handoff claim.** The prior review noted "`recursive/bridge.js` doesn't read any `RECURSIVE_*` env". That is no longer the case — both bridges read the full `RECURSIVE_*` set at parity; the arg-building parity cases now pin it.
- **Scope.** The full NDJSON event classification (`handleLine` inside `main()`) is not covered — it is nested in `main()` and not refactored to an importable helper. That remains future parity work.
- **CI.** The `test-hub-bridges` job runs both parity tests.
- **Wire protocol stays `0.1`.** Test-only; no bridge behaviour change.

### SDK 0.5.1 — security: tighten session-id charset (path-traversal fix)

The `AGENT_SESSION:` value's valid character set is tightened from `^[A-Za-z0-9._~+/=-]+$` to `^[A-Za-z0-9._~=-]+$` — `/` and `+` are removed.

- **Why.** The SDK history helpers (`session_file_path` / `sessionFilePath`) store each session as `<id>.jsonl` directly under the sessions directory. The old charset allowed `/`, so a session id like `../../tmp/x` — which a malicious or buggy agent could emit as `AGENT_SESSION:../../tmp/x` — would pass bridge validation and then path-traverse out of the sessions directory when a handler called `load_history` / `loadHistory` with it. `+` was removed at the same time to make the spec's "URL-safe" label truthful (standard base64 uses `+`/`/`; base64url uses `-`/`_`).
- **Impact on agents.** Real CLI tools emit UUIDs or base64url handles (no `/` or `+`), so conformant agents are unaffected. An agent that emitted standard-base64 session ids will now have its `AGENT_SESSION:` line ignored (with a stderr warning) and the previous session id preserved — same handling as any other invalid id.
- **Defense in depth.** `session_file_path` / `sessionFilePath` now also reject ids containing path separators or `..`, raising `ValueError` / throwing — so even a handler that bypasses bridge validation cannot traverse.
- **Wire protocol stays `0.1`.** The `AGENT_SESSION:` line format is unchanged; only the set of values a bridge accepts is tightened. The spec document revision is unchanged (this is a conformance tightening, not an editorial change).

### SDK 0.5.1 — yaml.js retired in favour of `js-yaml`

The hand-rolled YAML parser (`sdk/node/src/yaml.js`, ~126 lines) is replaced by a thin wrapper around `js-yaml`, which becomes a runtime dependency.

- **Why.** A diff of the hand-rolled parser against `js-yaml` over every checked-in `profile.yaml` exposed two latent bugs: (1) inline `#` comments were not stripped, so `streaming: false  # agy's --print mode …` parsed as the string `"false  # agy's …"` and the runner's `is not False` check left streaming **on** — the `agy` profile has been silently running in streaming mode despite declaring `streaming: false`; (2) an empty `env:` value parsed as `""` instead of `null`. `js-yaml` is the ecosystem standard; maintaining a subset parser forever is not a trade worth making.
- **Behaviour change.** `agy` now correctly runs with `streaming: false` (one-shot, full text at end), as the profile intended. No other checked-in profile's parsed shape changed.
- **Public API unchanged.** `parseYaml` / `parseYamlSimple` keep the same signature; `cli.js` and `hub.js` are unchanged.

### Spec: protocol document 0.6 — remove `AGENT_ATTACHMENTS` (wire protocol unchanged)

The multi-attachment variable `AGENT_ATTACHMENTS` (a JSON array in an env var) is **removed** from the spec and the SDKs. The single-attachment convenience variables `AGENT_IMAGE_URL` / `AGENT_FILE_URL` stay (P0).

- **Why removed.** No conformant bridge ever emitted `AGENT_ATTACHMENTS` — the runner/CLI had no code path to inject it, so it never reached an agent end-to-end. It was spec-only plus an SDK parse helper. Worse, putting JSON in an env var broke the bash `echo "You said: $AGENT_MESSAGE"` promise that is central to the protocol's design. Keeping a P0 field that nothing exercises, and that contradicts the no-shell-agent premise, was worse than cutting it.
- **What stays.** `AGENT_IMAGE_URL` / `AGENT_FILE_URL` are plain strings, bash-friendly, and cover the realistic single-image / single-file case for a messaging bridge. They are also now wired through the runner (see SDK 0.5.0 below), so they actually work end-to-end via `agentproc` / `agentproc hub run`.
- **When it comes back.** Real bridges that need to carry several attachments will reintroduce a delivery mechanism a hand-written shell agent can still consume — not JSON-in-env.
- **Wire protocol stays `0.1`.** The runner never emitted `AGENT_ATTACHMENTS`, so removing it changes no bytes on the wire. No conformant agent or bridge that relied on the runner needs to change; only code that read `ctx.attachments` / `parseAttachments` (SDK consumers parsing the draft var themselves) is affected — see breaking SDK changes below.

### SDK 0.5.0

- **Removed** `Attachment` / `parseAttachments` / `_parse_attachments` and the `ctx.attachments` field from both SDKs. `ctx.image_url` / `ctx.file_url` (`ctx.imageUrl` / `ctx.fileUrl` on Node) remain. This is a breaking API change for SDK consumers who read `ctx.attachments`; migrate to `ctx.image_url` / `ctx.file_url`.
- **Added runner passthrough for single-attachment vars.** `RunOptions` gains `image_url` / `file_url` (Node: `options.imageUrl` / `options.fileUrl`); the runner injects them as `AGENT_IMAGE_URL` / `AGENT_FILE_URL` on the spawned agent's environment when non-empty. The CLI gains `--image-url` / `--file-url`. This closes the gap that made attachments "not actually supported" via the runner/CLI.
- Hub `stream_utils` (both languages) and the `recursive` bridge no longer look at `AGENT_ATTACHMENTS`; the "empty `AGENT_MESSAGE` is legal when an attachment is present" rule now keys only off `AGENT_IMAGE_URL` / `AGENT_FILE_URL`.

### Spec: protocol document 0.5 (wire protocol unchanged)

Editorial pass on `spec/protocol.md` in response to a second deep review (protocol-level, security, cross-implementation consistency). The wire protocol remains `0.1`; no conformant agent or bridge needs to change **unless** it does something the spec previously left ambiguous (see breaking-ish items below). Highlights:

- **Empty `AGENT_MESSAGE` is now defined.** Spec previously didn't say whether an empty message was legal. Now: empty + no attachment = error; empty + attachment present = legal turn (the "image-only message" case). Hub `stream_utils` updated to reject only the truly-empty case.
- **`command`/`args` disambiguation.** `args: []` (explicit empty list) now means "do not split command" — distinct from `args` absent (which keeps the whitespace-splitting shorthand). This gives a clean escape hatch for executable paths containing spaces that take no extra argv tokens. `args: null` (YAML `args:` with no value) is treated as absent.
- **`${VAR}` security warning in profile `env`.** Spec now explicitly states that profile `env` values are substituted against the bridge's full environment, so a profile is trusted input — do not run profiles from untrusted sources via `agentproc hub run`.
- **Optional `env_allowlist` profile field.** A new opt-in field that shrinks the `${VAR}` trust boundary: when present, references to names not in the list expand to empty + a stderr warning instead of the variable's value. Absent (the default) keeps the current full-environment behaviour. Hub profiles SHOULD set it so `hub run` exposes only the credentials a profile actually needs.
- **`AGENT_ERROR:` interaction with already-delivered partials.** Spec now states that partials forwarded before the error are not retracted (most platforms can't), only future partials are suppressed. Bridges MAY stop reading stdout entirely after emitting the error — the result is already captured.
- **Session ID format enforced.** Spec restates that the `AGENT_SESSION:` value MUST NOT contain whitespace, control characters, or colons, and now defines bridge behaviour: an invalid value is ignored (the previous valid id is preserved; if none, the session stays empty) with a stderr warning. Runners implement this via a new `is_valid_session_id` / `isValidSessionId` helper.
- **Exit-code precedence codified.** When multiple failure signals arrive, precedence is: timeout (124) > `AGENT_ERROR:` (1) > process exit code. Both runners already followed this; now the spec matches.
- **SDK `send_error` terminality documented.** Spec notes that SDKs treat `send_error` as terminal (no further partial/reply body after), stricter than the raw protocol allows.

### Runner / SDK

- `sdk/{python,node}/src/runner` parse `env_allowlist`; when set, `${VAR}` references in the `env` block not on the list expand to empty + an `on_stderr`/`onStderr` warning.
- `sdk/python/src/agentproc/runner.py` adds `RunOptions.forward_stdin` (parity with Node's `forwardStdin`).
- `args: []` and `args: null` handling aligned between Python and Node runners.

### Hub

- `hub/_shared/stream_utils.{py,js}` — empty-message rejection now considers attachments; only rejects when there is no message text AND no attachment of any kind.

### Tests

- New `spec/conformance/cases.json` + `sdk/python/tests/test_conformance.py` + `sdk/node/src/conformance.test.js`: both reference implementations run the same stdout-classification fixture, so future spec changes can't cause silent drift.
- New `spec/conformance/scenarios.json` + `sdk/python/tests/test_scenarios.py` + `sdk/node/src/scenarios.test.js`: both reference implementations run the same multi-line-turn fixture (last-wins, error-mid-stream, session-with-error, invalid-session, streaming vs one-shot), so interaction-level semantics can't drift either.
- New tests for `args: []` / `args: null` behaviour, empty-message-with-attachment acceptance in the hub layer, `env_allowlist` end-to-end, invalid-session-id handling.

### Breaking-ish (clarifications, not wire changes)

None of these change bytes on the wire. They pin down behaviour the spec previously left ambiguous. An implementation that exploited the ambiguity (e.g. a hub that rejected empty messages unconditionally even with attachments, or a profile author relying on `args: []` being treated as absent) will see different behaviour after this revision. The reference implementations were already correct on most of these; the change is to make the spec match.

### No wire protocol changes

Still protocol `0.1`. This revision clarifies the document; no conformant agent or bridge needs to change.



Editorial pass on `spec/protocol.md` in response to a deep review. The wire protocol remains `0.1`; no conformant agent or bridge needs to change. Highlights:

- **Versioning section added.** Codifies that `AGENT_PROTOCOL_VERSION` is opaque and non-comparable. Agents MUST NOT order or range-check it; feature presence is signalled by the corresponding env var, not the version string.
- **`AGENT_ATTACHMENTS` promoted from Draft to P0.** The SDK already parsed it; the spec now matches. Added a consistency requirement: when a bridge sets `AGENT_ATTACHMENTS` alongside `AGENT_IMAGE_URL` / `AGENT_FILE_URL`, the two MUST agree.
- **Session-line ordering clarified.** When a CLI emits `AGENT_SESSION:` together with `AGENT_ERROR:` (the common `result{is_error}` shape from claude/cursor), bridges MUST preserve the session id for the next turn even though the current turn is reported as failed. The hub `stream_utils` module now emits `AGENT_SESSION:` before `AGENT_ERROR:` so the id is not lost.
- **`AGENT_ERROR:` → bridge MUST treat as failed** (was SHOULD). Exit code 0 + `AGENT_ERROR:` is still a failed turn.
- **`command` defined as argv[0], `args` as the rest.** Adds an explicit quoting rule so paths with whitespace remain expressible without a shell.
- **Windows caveat** noted for the SIGTERM/SIGKILL timeout contract.

### Hub: codex resume bug fix

- `hub/codex/bridge.{py,js}` — the `codex exec resume <thread_id> <message>` path was missing `--json`, so multi-turn resume produced non-NDJSON output that the bridge could not parse. Fixed in both bridges.

### Runner: cross-platform timeout + full-stderr diagnosis

- `sdk/{python,node}/src/runner` — timeout now uses `terminate()` / `kill()` instead of `SIGTERM` / `SIGKILL` directly, so it works on Windows (where those signal constants are absent). Behaviour on POSIX is unchanged.
- `sdk/{python,node}/src/runner` — post-mortem stderr diagnosis now reads the **full** captured stderr instead of the 8 KB sliding window, so a noisy agent can no longer push the real error out of the diagnostic window. The displayed/captured buffer is still bounded for memory; only diagnosis uses the unbounded copy.

### Tests: hub bridge end-to-end coverage

- New `sdk/python/tests/test_bridges.py` exercises every hub bridge's `parse_event` against fixture NDJSON streams, asserting the resulting `AGENT_PARTIAL:` / `AGENT_SESSION:` / `AGENT_ERROR:` output. Covers the codex resume path, the error-with-session-id case, and cursor's accumulated-dedup closure.

### Hub: two new profiles + shared bridge module

Added two new NDJSON-based hub profiles and extracted the duplicated bridge logic into a shared module.

**New profiles:**

| Profile | CLI | Tested |
|---------|-----|--------|
| `gemini-cli` | `gemini` (Google Gemini CLI) | official |
| `cursor` | `agent` (Cursor Agent) | official |
| `qwen-code` | `qwen` (Alibaba Qwen Code) | community |

`gemini-cli` was verified end-to-end against the CLI's published `stream-json` event schema (`init` / `message` / `error` / `result`) by reading the source at `google-gemini/gemini-cli/packages/core/src/output/types.ts`. Session id is emitted up-front in the `init` event (unlike claude/codex where it appears in the terminal `result` event).

`cursor` wraps the Cursor Agent CLI (binary name `agent`, not `cursor` — the binary is a standalone download separate from the Cursor IDE). Verified end-to-end against the installed CLI (2026.06.24): schema is claude-code-compatible (`system/init` → session_id, `assistant` content blocks → text deltas, `result` → session_id + result text). Cursor's `--stream-partial-output` flag emits N delta chunks AND THEN a final `assistant` event with the full assembled text; the bridge tracks accumulated emitted text and drops the duplicate via `make_parse_event()` closure state. Multi-turn resume via `--resume <chatId>` confirmed working.

`qwen-code` is a fork of gemini-cli; its `--output-format stream-json` flag set matches gemini's (`-p / -o / -r / -y`), and the bridge reuses the gemini-cli parser. Marked `community` because the published stream-json schema was not verified against a real `qwen` invocation — please report drift.

**Shared bridge module (`hub/_shared/`):**

Three of the existing bridges (`claude-code`, `codex`, `codebuddy`) shared ~80% identical logic: subprocess spawn, NDJSON line reading, JSON decoding, AGENT_* emission, exit-code mapping. Extracted to:

- `hub/_shared/stream_utils.py` — `EventResult` dataclass + `run_bridge()` / `main_entry()`
- `hub/_shared/stream_utils.js` — `runBridge()` + emit helpers

Each bridge is now ~30 lines supplying two functions: `build_args()` (CLI-specific) and `parse_event()` (event-shape-specific). The shared module handles subprocess lifecycle, line reading, dedup, and the streaming-vs-non-streaming emission policy.

**New `partial_text` vs `final_text` split in `EventResult`:**

The previous bridges had a latent duplicate-emission bug: when a CLI streams partial chunks AND THEN emits a `result` event containing the full assembled text (the common Claude / CodeBuddy pattern), the user received the text twice. The shared module fixes this by distinguishing incremental `partial_text` (streamed live) from terminal `final_text` (emitted only in non-streaming mode, or as fallback when nothing was streamed). See `hub/_shared/README.md`.

**Other:**

- Added a coverage matrix to `hub/README.md` tracking AgentProc Hub's coverage of the [ACP Registry](https://agentclientprotocol.com/get-started/registry) agent list. Five ACP-listed agents are covered (4 official, 1 community); seven are explicitly out of scope with documented reasons (Cursor ships inside its app, Goose is cargo-install, Copilot's JSONL schema is undocumented, Junie/Cline/GLM-Agent have no standalone CLI to wrap).
- Bridges that don't fit the NDJSON abstraction (`agy`, `echo-agent`) keep their bespoke implementations — `stream_utils` is opt-in.

### No protocol changes

Still protocol `0.1`. The new profiles and the shared module are bridge-side concerns; agents see the same env vars and emit the same stdout protocol.

## 0.4.2 — 2026-07-02

Hub distribution: bundle the profiles into the packages and serve remote fetches from jsDelivr. No protocol changes — `AGENT_*` env vars, stdout sentinels, and profile schema are unchanged.

### The problem

`raw.githubusercontent.com` and `api.github.com` are often unreachable or slow in some regions (notably China), and every `hub run` / `hub list` depended on them. A latent bug also meant NDJSON-style profiles (claude-code, codex, cursor, …) could not actually run from a `hub run` fetch: their `bridge.py` does `from _shared.stream_utils import …`, but `fetch_profile` only cached the profile directory, not `_shared/`, so the import failed at runtime (echo-agent worked only because its bridge is self-contained).

### The fix

- **Profiles are now bundled inside the packages.** The entire `hub/` directory (including `_shared/`) is copied into the npm package at `<pkg>/hub/` (via `prepublishOnly`) and into the Python package at `agentproc/hub_data/` (via `scripts/prepare_hub.py`, run before `python -m build` in `publish.yml`). `hub run` and `hub list` read from the bundled copy by default — **zero network** in the common case. npm/PyPI both have fast China mirrors (npmmirror, 清华/阿里), so installing the package also installs the hub.
- **Remote fallback switched from GitHub to jsDelivr.** For `--refresh` or a profile newer than the installed CLI, file fetches now come from `cdn.jsdelivr.net/gh/jeffkit/agentproc@main/…` (Fastly CDN, reachable where GitHub is not) and the directory listing from jsDelivr's data API (`data.jsdelivr.com`). The GitHub Trees API and `raw.githubusercontent.com` are no longer used, so the ~60/hr anonymous rate limit and `GITHUB_TOKEN` hint are gone.
- **`_shared/` is now populated in the cache** alongside any fetched profile (from the bundle or from jsDelivr), so NDJSON profiles actually run. `install_profile` also installs `_shared/` next to the profile.

### Changes

- `sdk/node/src/hub.js`, `sdk/python/src/agentproc/hub.py`: bundled-copy fast path (zero network) → jsDelivr remote fallback → `_shared/` cache population; jsDelivr data API replaces the GitHub Trees API (nested-tree flattener added).
- `sdk/node/src/yaml.js` (new): the YAML parser extracted from `cli.js` so `hub.js` can parse bundled `profile.yaml` without a circular `require('./cli.js')` (cli.js requires hub.js at the top and is only fully exported after `main()` runs — the sync bundled path hit a partial-exports bug).
- `sdk/node/src/cli.js`: now imports `parseYaml` from `./yaml.js`; behavior unchanged.
- `sdk/node/scripts/prepare-hub.js`, `sdk/python/scripts/prepare_hub.py` (new): copy `hub/` into the package at build/publish time, excluding `__pycache__`/`*.pyc`.
- `sdk/node/package.json`: `prepublishOnly` script + `"files": ["src", "hub"]`.
- `sdk/python/pyproject.toml`: `[tool.setuptools.package-data]` includes `hub_data/**`.
- `.github/workflows/publish.yml`: PyPI build runs `prepare_hub.py` before `python -m build`.
- `.gitignore`: ignore the generated `sdk/node/hub/` and `sdk/python/src/agentproc/hub_data/`.
- Tests rewritten on both sides: jsDelivr data-API shape, bundled-path coverage, `_shared` population, `install` installs `_shared`.

## 0.4.1 — 2026-07-01

Hub rate-limit relief. No protocol changes — `AGENT_*` env vars, stdout sentinels, and profile schema are unchanged.

### The problem

Every `agentproc hub` command called the GitHub **Trees API** (`api.github.com/.../git/trees`), which rate-limits anonymous callers to **~60/hour per IP** (shared across everyone behind the same NAT). A handful of `hub list` / `hub run` invocations would blow the budget and the next user got `HTTP 403`. Telling ordinary users to `export GITHUB_TOKEN` is not a real fix.

### The fix (Node + Python parity)

- **`agentproc hub run <name>` no longer calls the Trees API in the happy path.** Profile files are fetched directly via `raw.githubusercontent.com` (Fastly CDN, not rate-limited) using a fixed candidate set (`profile.yaml`, `bridge.py`, `bridge.js`, `bridge.sh`, `README.md` — per the hub convention in `hub/README.md`). Optional files that 404 (e.g. `bridge.sh` on a non-echo profile) are simply skipped. Only an unknown profile name (profile.yaml 404) falls back to the tree to produce a "did you mean" suggestion.
- **The repo tree is now cached on disk** at `~/.agentproc/cache/hub/tree.json` with the same 24h TTL as profiles (previously in-memory only, which never survives a fresh CLI process). So `agentproc hub list` — and the unknown-name fallback — make at most ~1 Trees API call per day, no matter how many times they're run.
- Net effect: a normal user's daily `hub` usage makes **zero** `api.github.com` calls in the common case (files come from the CDN; the tree is disk-cached). The 60/hr limit is no longer reachable in practice, and `GITHUB_TOKEN` is only needed for heavy/automated use.

### Notes

- New `clearTreeCache()` (Node) / `_clear_tree_cache()` (Python) clears both layers; `hub run --refresh` uses it so a refresh sees newly-added profiles.
- `listRemoteProfileFiles` / `_list_remote_profile_files` and the single-file download helpers were removed (the raw-URL candidate fetch replaces them).
- Tests updated: fake HTTP returns 404 (not an assertion) for unmatched raw URLs; new tests assert `hub run`'s happy path makes 0 Trees API calls, optional files 404 cleanly, and the tree is disk-cached across calls.

## 0.4.0 — 2026-06-26

A round of UX and resilience fixes after running the CLI as a non-coder would. The 5-minute path now actually works on the first try. No protocol changes — `AGENT_*` env vars, stdout sentinels, and profile schema are all backward-compatible.

### Highlights

- **`agentproc hub run <name> -p "hi"` finally works.** The `-p` short flag was treated as a positional in the hub subcommand parser (it's `--profile` short form in the main parser), so the homepage's own smoke-test command failed. Fixed in both Node and Python CLIs.
- **Hub fetch failures are now human-readable.** GitHub 403/404/network errors used to dump raw stack traces. They now raise a typed `HubError` with a remediation hint (set `GITHUB_TOKEN`, run against a local checkout, etc.).
- **Hub profiles no longer ship a `cwd: ~/your-project` placeholder.** All 5 profiles switched to `command: python3 {{PROFILE_DIR}}/bridge.py` with cwd unset — `hub run` defaults cwd to your current directory, so the wrapped CLI operates on whatever project you're in. The `{{PROFILE_DIR}}` placeholder decouples bridge-script location from agent cwd.
- **New troubleshooting page** at `/guide/troubleshooting` (EN + ZH) — decision tree for the most common errors.

### CLI fixes (Node + Python parity)

- `agentproc hub run echo-agent -p "hello"` works (was: "requires --prompt").
- `agentproc hub run echo-agent --refresh` works (was: `unknown option: --refresh`).
- `agentproc hub run --help` shows the hub help (was: "requires a profile name").
- `agentproc hub runn ...` exits 2 (was: exit 0 — shell scripts couldn't detect the failure).
- `agentproc hub install <name>` prints a "Next:" hint with the exact command to run.
- Top-level `agentproc --help` now leads with the hub three-liner; `--profile` mode moved under "Advanced".

### Runner resilience (Node + Python parity)

- New `{{PROFILE_DIR}}` placeholder in `command`, `args`, `cwd`, and `env` values. Resolves to the profile's own directory.
- Relative `cwd` now resolves against `{{PROFILE_DIR}}`, not the bridge process cwd.
- `spawn ENOENT` errors get a tailored hint: "cwd does not exist" / "command not on PATH" / "argument file not found" / "permission denied".
- When the wrapped interpreter (python/node/bash) writes "No such file" to its own stderr before exiting non-zero, the runner surfaces it as `AGENT_ERROR:"agent script not found: ..."` instead of letting raw text leak through.
- stderr accumulated as a sliding 8KB window (last 8KB wins, not first) for post-mortem diagnosis.

### Hub fetch resilience (Node + Python parity)

- `HubError` class with `.hint` and `.status` fields.
- `GITHUB_TOKEN` / `GH_TOKEN` env vars add `Authorization: Bearer <token>`, raising the rate limit from ~60/hr to ~5,000/hr.
- 403/429 hint lists the rate-limit fix; 404 hint suggests `agentproc hub list`.
- Typo'd profile names get a "Did you mean X?" suggestion via prefix match + edit distance (length-scaled threshold).
- `agentproc hub list` skips `_`-prefixed utility directories such as `_shared/`. Previously `_shared` showed up as `(failed to read metadata)` because the hub tried to fetch a non-existent `profile.yaml` for it.

### Hub profile schema

The five hub profiles (`claude-code`, `codex`, `codebuddy`, `agy`, `echo-agent`) changed shape:

```yaml
# Before (0.3.0)
agentproc:
  command: python3 ./bridge.py
  cwd: ~/your-project

# After (0.4.0)
agentproc:
  command: python3 {{PROFILE_DIR}}/bridge.py
  # cwd intentionally omitted: `hub run` defaults it to your current directory.
```

**Compatibility**: this is technically a breaking change for users who hand-edited hub profile templates. The hub design has always been "don't edit, use `hub run`," so impact is limited. Custom profiles (outside the hub) are unaffected — relative `./xxx` paths still work, they just resolve against `{{PROFILE_DIR}}` instead of process cwd.

### Docs

- New `/guide/troubleshooting` (EN + ZH): rate limit, wrong name, spawn ENOENT, agent AGENT_ERROR, timeout, network down.
- Homepage: `hub run` smoke test leads, GITHUB_TOKEN tip, "short replies may not show AGENT_PARTIAL" tip, macOS `pip`-via-`ensurepip` tip.
- `cli/`, `hub/`, `guide/`, `spec/` rewritten to lead with `hub run` and document `{{PROFILE_DIR}}`.
- SDK pages: "Local testing" now uses `agentproc --profile` first; raw env-var forms moved under `<details>`.
- All 5 hub profile READMEs reorganized: "Quick test" leads, raw-env-var tests under `<details>`.
- `CLAUDE_MODEL` / `CODEX_MODEL` env-var comments added to profile.yaml templates.

### Protocol

No changes. Still protocol `0.1`. The `{{PROFILE_DIR}}` placeholder is a bridge-level convention; agents don't see it (the bridge resolves it before spawning).

## 0.3.0 — 2026-06-25

### `agentproc hub` subcommands

Hub profiles can now be fetched and run directly from GitHub — no clone, no copy, no YAML editing.

```bash
agentproc hub list                          # list available profiles
agentproc hub show <name>                   # print a profile's README
agentproc hub run <name> -p "hello"         # fetch (if needed) and run
agentproc hub install <name>                # copy to current dir for editing
```

Profiles are cached at `~/.agentproc/cache/hub/<name>/` with a 24-hour TTL. Pass `--refresh` to force re-fetch.

Implementation uses the GitHub git-tree API (1 call for the entire file listing) plus raw.githubusercontent.com for downloads, so unauthenticated users stay below rate limits.

### New `agentproc.hub` module

Both SDKs now expose a `hub` module:

```js
// Node
const { fetchProfile, listProfiles, showReadme, installProfile } = require('agentproc/src/hub');
```

```python
# Python
from agentproc import hub
hub.fetch_profile("claude-code")
```

Zero new dependencies. Python uses `urllib.request` (stdlib), Node uses global `fetch` (Node 18+).

### Python CLI version display fix

`agentproc --version` now correctly shows the installed package version (was showing `0.0.0+unknown` after pip install due to pyproject.toml not being shipped in the wheel). Uses `importlib.metadata.version("agentproc")` with a pyproject.toml fallback for source checkouts.

Also exposes `agentproc.__version__` for SDK users who need it.

### Homepage onboarding flow updated

Step ③ of the "Get started in 5 minutes" guide now uses `agentproc hub run` instead of `git clone + cp + edit YAML`, reducing the time-to-first-success from minutes to seconds.

### Tests

- Python: 17 new tests in `tests/test_hub.py` (mock-based, no real network)
- Node: 16 new tests in `src/hub.test.js` (mock-based, no real network)
- All existing tests still pass

### No protocol changes

0.3.0 adds tooling on the bridge runner side. The wire protocol is unchanged from 0.1. Existing agents and bridges remain compatible.

## 0.2.1 — 2026-06-25

### Python CLI

The Python package now ships the `agentproc` CLI alongside the SDK. After `pip install agentproc` (or `pipx install agentproc`), the `agentproc` command is available — same flags, same output semantics as the Node CLI.

- New `sdk/python/src/agentproc/runner.py` — the canonical bridge-side engine in Python (mirrors `sdk/node/src/runner.js`)
- New `sdk/python/src/agentproc/cli.py` — argparse-based CLI entry point
- `[project.scripts] agentproc = "agentproc.cli:main"` added to `pyproject.toml`
- 60 new tests in `tests/test_runner.py` (parity with Node's `runner.test.js`)

Both language implementations stay at parity: same protocol behavior, same exit codes, same CLI flags. Pick whichever language fits your stack.

### Docs: homepage onboarding flow

The homepage (`/` and `/zh/`) now has a 4-step "Get started in 5 minutes" guide:

1. **Install** — npm / pipx / pip in a code-group
2. **Pick a profile** — links to the 5 official hub profiles
3. **Run it** — echo-agent smoke test, then a real claude-code call with session capture
4. **Connect to messaging** — a ~30-line Node.js bridge example showing how AgentProc fits between your platform and the agent

Feature cards updated to lead with concrete value (5-minute setup, supported CLIs, any platform, open spec) instead of protocol mechanics.

## 0.2.0 — 2026-06-25

### agentproc CLI

The Node SDK package now ships a canonical bridge-side runner. Install it globally and drive any profile:

```bash
npm install -g agentproc
agentproc --profile hub/claude-code/profile.yaml --prompt "hello"
```

Features:

- `--profile <path.yaml>` + `--prompt <text>` (or `--stdin`)
- `--session <id>` for multi-turn continuity
- `--cwd`, `--env KEY=VALUE` (repeatable), `--timeout` overrides
- `--no-stream` to set `AGENT_STREAMING=0`
- `--verbose` (default) / `--quiet` for protocol line visibility
- `--raw` for verbatim stdout passthrough
- Final session id printed on stderr as `agentproc:session:<id>` for shell capture
- Exit codes per spec (0 / 1 / 124)

The CLI is a thin wrapper over the new `run()` function in `sdk/node/src/runner.js`, which is the canonical reference implementation of the bridge-side contract. Implementers writing bridges in other languages can read `runner.js` as the spec in code form.

Runner tests: 60 unit + end-to-end tests covering profile parsing, placeholder substitution, env injection, stdout classification (session/partial/error/body), timeout/SIGTERM, stdin EOF, exit codes.

### Profile Hub

Five drop-in profiles added under `hub/`, each with `profile.yaml` + `bridge.py` + `bridge.js` + `README.md`:

| Profile | CLI | Tested |
|---------|-----|--------|
| `claude-code` | Anthropic Claude Code | official |
| `codex` | OpenAI Codex | official |
| `codebuddy` | Tencent CodeBuddy | official |
| `agy` | agy | community |
| `echo-agent` | (no CLI, hello world) | official |

CI added: `test-hub-bridges` job does syntax checks, echo-agent end-to-end, and profile YAML schema validation.

### Docs

- New `/cli/` VitePress section (EN + ZH)
- Hub pages documented at `/hub/`
- `llms.txt` and `llms-full.txt` updated with hub + CLI sections
- `AGENTS.md` and `CONTRIBUTING.md` reflect the new structure

### No protocol changes

0.2.0 adds tooling and profiles, but does not change the wire protocol. Existing 0.1 agents and bridges remain compatible.

## 0.1.1 — 2026-06-25

Republish to align the PyPI release with npm. The initial 0.1.0 tag fired the publish workflow before tests were wired into CI; the Python package was uploaded to PyPI from a pre-revision commit and never received the protocol/SDK improvements. npm 0.1.0 was published from the correct tree.

**No protocol or API changes vs 0.1.0.** Use 0.1.1+ for both SDKs.

## 0.1.0 — 2026-06-25

First public draft of the protocol and SDKs.

### Protocol

Initial definition of the AgentProc process-based interface between a messaging bridge and an agent process.

**Input contract:**

- Environment variables: `AGENT_MESSAGE`, `AGENT_SESSION_ID`, `AGENT_SESSION_NAME`, `AGENT_FROM_USER`, `AGENT_STREAMING`
- `AGENT_PROTOCOL_VERSION` injected by the bridge (currently `"0.1"`)
- Attachment env vars: `AGENT_IMAGE_URL`, `AGENT_FILE_URL` (P0, single attachment)
- `AGENT_ATTACHMENTS` JSON array (draft, multi-attachment) — agents SHOULD prefer it when present
- Optional stdin write when profile `stdin: message`; bridge closes stdin (EOF) after writing

**Output contract (sentinel-prefixed stdout lines):**

- `AGENT_SESSION:<opaque-id>` — declare session ID; **may appear anywhere; last occurrence wins**
- `AGENT_PARTIAL:<json-string>` — streaming chunk (ignored when `streaming: false`)
- `AGENT_ERROR:<json-string>` — error message forwarded to the user regardless of streaming mode
- All other lines = final reply body
- Reply body MUST NOT start with `AGENT_SESSION:`, `AGENT_PARTIAL:`, or `AGENT_ERROR:`
- JSON parse failure on `AGENT_PARTIAL:` defaults to lenient mode (raw text forwarded, warning logged)

**Profile fields:**

- `command` — split on whitespace into argv, NOT passed through a shell
- `args`, `cwd`, `env` — support `{{MESSAGE}}`, `{{SESSION_ID}}`, `{{SESSION_NAME}}` placeholders
- `stdin` — `none` (default) | `message`
- `timeout_secs` (default 1800), `kill_grace_secs` (default 5) — SIGTERM then SIGKILL
- `max_reply_chars` (default 8000), `truncation_suffix`
- `include_stderr_in_reply`, `send_error_reply`, `streaming`

**Exit codes:** `0` success · `1` error · `124` timeout · `130` SIGINT · `143` SIGTERM.

### SDKs

Python (`agentproc`) and Node.js (`agentproc`) SDKs released at parity.

- `create_profile(handler)` / `createProfile(handler)` — async handler with `AgentContext`
- `ctx.send_partial(text)` / `ctx.sendPartial(text)` — streaming
- `ctx.send_error(text)` / `ctx.sendError(text)` — surface user-readable errors
- `raise ProtocolError(text)` (Python) / `throw await sdk.protocolError(text)` (Node) — exception form
- `ctx.attachments` — parsed `AGENT_ATTACHMENTS` (draft)
- `ctx.protocol_version` / `ctx.protocolVersion`
- History helpers: `load_history` / `loadHistory`, `append_history` / `appendHistory`, `session_file_path` / `sessionFilePath`

### Docs

- VitePress site (`docs/`) in English and Chinese
- Protocol spec at `spec/protocol.md` and `spec/protocol.zh.md`
- Comparison with MCP, ACP, NDJSON, SSE, LSP, Unix filter
- Design rationale (why env vars, why sentinel lines, why last-wins, why AGENT_ERROR)

### Tests

- Node SDK: 25 tests via `node --test`
- Python SDK: 29 tests via `pytest`
