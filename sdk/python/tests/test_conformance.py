"""Cross-implementation conformance tests.

Drives the shared `spec/conformance/cases.json` fixture through the Python
runner's `classify_line` and asserts the result matches the expected
{kind, value}. The Node SDK runs the same fixture through its `classifyLine`
in `sdk/node/src/conformance.test.js` — together they guarantee the two
reference implementations classify stdout identically.

The same file also carries `posture_cases` — the per-executor permission
posture matrix. These are driven through `run()` with a fake CLI on PATH that
records its own argv, so a case pins observable behaviour (which argv the CLI
received, or that it was never spawned) rather than internal signatures.


Also drives `spec/conformance/executors.json` through the in-process executor
path (`run_via_executor`) with a fake printf-backed executor, asserting the
full RunResult. The Node SDK (`conformance.test.js`) and the Rust SDK
(`sdk/rust/src/conformance.rs`) drive the same fixture.

When you change the spec's line-recognition rules, add a case here first;
both SDKs will fail until they agree.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agentproc.executors import EXECUTORS
from agentproc.runner import AUTO_APPROVE_FLAGS, RunOptions, _compose_env, classify_line, normalize_profile, run, run_via_executor

CONFORMANCE_DIR = Path(__file__).resolve().parents[3] / "spec" / "conformance"
CASES_PATH = CONFORMANCE_DIR / "cases.json"
CASES_DATA = json.loads(CASES_PATH.read_text(encoding="utf-8"))
EXECUTORS_PATH = CONFORMANCE_DIR / "executors.json"


def _load_cases():
    return [pytest.param(c["line"], c["expect"], id=c["line"][:60]) for c in CASES_DATA["cases"]]


@pytest.mark.parametrize("line,expect", _load_cases())
def test_classify_line_conformance(line: str, expect: dict) -> None:
    got = classify_line(line)
    assert got == expect, f"line={line!r}: got {got}, expected {expect}"


def _load_env_cases():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data.get("env_compose", [])


@pytest.mark.parametrize("case", _load_env_cases())
def test_env_compose_conformance(case: dict, monkeypatch) -> None:
    """The shared env-composition policy, driven through _compose_env.

    _compose_env reads os.environ both for ${VAR} expansion and for the
    infra set, so the fake host env is monkeypatched in — everything not
    listed is deleted to prove no other host variable leaks through.
    """
    host_env = case["host_env"]
    for name in list(host_env) + ["_AGENTPROC_FAKE_MARKER"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("_AGENTPROC_FAKE_MARKER", "1")
    for name, value in host_env.items():
        monkeypatch.setenv(name, value)

    profile = normalize_profile(
        {"executor": "conformance", "env": case["profile_env"],
         "env_allowlist": case["env_allowlist"]}
    )
    env = _compose_env(
        profile,
        RunOptions(message="hi", extra_env=dict(case["extra_env"])),
    )

    for name, value in case["expect_contains"].items():
        assert env.get(name) == value, f"{name}: expected {value!r}, got {env.get(name)!r}"
    for name in case["expect_absent"]:
        assert name not in env, f"{name} leaked into the composed child env"


# Fake CLI: record every argv token in <dir>/argv.txt, then report a clean turn.
_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$@" > {argv_file}
echo '{{"type":"result","result":"ok","session_id":"sess-1"}}'
"""


def _run_posture_case(case):
    """Drive one `posture_cases` entry through `run`; return (RunResult, argv|None)."""
    cli_name = EXECUTORS[case["executor"]]["cli_name"]
    case_env = dict(case.get("env") or {})
    with tempfile.TemporaryDirectory(prefix="ap-posture-") as tmpdir:
        argv_file = Path(tmpdir) / "argv.txt"
        shim = Path(tmpdir) / cli_name
        shim.write_text(_SHIM.format(argv_file=str(argv_file)))
        shim.chmod(0o755)
        # `env` is written to the runner process environment and to the per-run
        # env extras, so the case reaches the runner either way. Cases without
        # the knob explicitly blank it, so the ambient environment cannot leak
        # a posture switch into the fixture.
        environ = {"PATH": tmpdir + os.pathsep + os.environ["PATH"]}
        environ.update(case_env)
        environ.setdefault("AGENTPROC_AUTO_APPROVE", "")
        profile = {"executor": case["executor"]}
        if "permission" in case:
            profile["permission"] = case["permission"]
        with patch.dict(os.environ, environ, clear=False):
            result = run(profile, RunOptions(message="hi", extra_env=case_env))
        argv = argv_file.read_text().splitlines() if argv_file.exists() else None
    return result, argv


def test_auto_approve_flags_match_fixture() -> None:
    assert list(AUTO_APPROVE_FLAGS) == CASES_DATA["auto_approve_flags"]


@pytest.mark.parametrize(
    "case",
    CASES_DATA["posture_cases"],
    ids=[c["name"] for c in CASES_DATA["posture_cases"]],
)
def test_posture_conformance(case: dict) -> None:
    result, argv = _run_posture_case(case)
    expect = case["expect"]
    if expect.get("refused"):
        assert argv is None, f"{case['name']}: CLI was spawned with {argv}"
        assert result.error, f"{case['name']}: expected a hard failure"
        assert result.exit_code != 0, f"{case['name']}: expected a non-zero exit code"
        return
    assert argv is not None, f"{case['name']}: CLI was never spawned ({result.error})"
    if expect.get("error"):
        assert result.error, f"{case['name']}: expected an error"
        assert result.exit_code != 0, f"{case['name']}: expected a non-zero exit code"
    if expect.get("exit_zero"):
        assert result.error == "", f"{case['name']}: {result.error}"
        assert result.exit_code == 0, f"{case['name']}: exit {result.exit_code}"
    if expect.get("reply") is not None:
        assert result.reply == expect["reply"], f"{case['name']}: {result.reply!r}"
    for token in expect.get("argv_contains", []):
        assert token in argv, f"{case['name']}: {token} missing from {argv}"
    for token in expect.get("argv_excludes", []):
        assert token not in argv, f"{case['name']}: {token} present in {argv}"


def _fake_executor(lines):
    """printf-backed fake CLI emitting one NDJSON line per scenario entry,
    plus the fixture's shared rule-table parse_event."""
    joined = "\n".join(json.dumps(l) for l in lines)

    def build_args(message, session_id, env, ctx):
        return ["printf", "%s\\n", joined]

    def parse_event(event):
        t = event.get("type")
        if t == "partial":
            return {"partial_text": event.get("text")}
        if t == "result":
            out = {"final_text": event.get("text", "")}
            if event.get("session_id"):
                out["session_id"] = event["session_id"]
            if event.get("usage") is not None:
                out["usage"] = event["usage"]
            return out
        if t == "error":
            out = {"error": event.get("message")}
            if event.get("session_id"):
                out["session_id"] = event["session_id"]
            if event.get("usage") is not None:
                out["usage"] = event["usage"]
            return out
        return None

    return {
        "cli_name": "fake-cli",
        "install_hint": "",
        "plain": False,
        "build_args": build_args,
        "parse_event": parse_event,
    }


def _make_opts(**kwargs):
    defaults = dict(
        message="hello",
        session_id="",
        extra_env={},
        timeout_secs=10,
        streaming=None,
        on_partial=None,
        on_session=None,
        on_error=None,
        cwd=None,
        profile_dir=None,
    )
    defaults.update(kwargs)
    return RunOptions(**defaults)


def _load_executor_scenarios():
    scenarios = json.loads(EXECUTORS_PATH.read_text(encoding="utf-8"))["scenarios"]
    # Sanity: an emptied or mis-pathed fixture must fail, not pass vacuously.
    assert len(scenarios) >= 13, f"executors.json: expected >= 13 scenarios, got {len(scenarios)}"
    return [pytest.param(s, id=s["name"]) for s in scenarios]


@pytest.mark.parametrize("scenario", _load_executor_scenarios())
def test_executor_path_conformance(scenario: dict) -> None:
    exp = scenario["expect"]
    partials = []
    result = run_via_executor(
        _fake_executor(scenario["lines"]),
        _make_opts(streaming=scenario["streaming"], on_partial=partials.append),
    )
    assert result.reply == exp["reply"], "reply"
    assert result.session_id == exp["session_id"], "session_id"
    assert result.error == exp["error"], "error"
    assert result.exit_code == exp["exit_code"], "exit_code"
    assert result.usage == exp["usage"], "usage"
    assert partials == exp["partials"], "partials"


def _load_partial_role_cases():
    cases = CASES_DATA.get("partial_role_cases", [])
    # Sanity: an emptied or mis-pathed fixture must fail, not pass vacuously.
    assert len(cases) >= 3, f"cases.json: expected >= 3 partial_role_cases, got {len(cases)}"
    return [pytest.param(c, id=c["name"]) for c in cases]


@pytest.mark.parametrize("case", _load_partial_role_cases())
def test_partial_role_conformance(case: dict, tmp_path: Path) -> None:
    """The `on_partial` second argument, which the classifier cases cannot see."""
    quoted = "'" + case["line"].replace("'", "'\\''") + "'"
    script = tmp_path / "agent.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' {quoted}\n"
        "printf '%s\\n' '{\"type\":\"result\",\"text\":\"\"}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    seen = []
    run(
        {"command": str(script)},
        _make_opts(
            streaming=True,
            on_partial=lambda text, role=None: seen.append((text, role)),
        ),
    )
    assert seen == [(case["expect_text"], case["expect_role"])], (
        f"on_partial calls: got {seen!r}, expected "
        f"{[(case['expect_text'], case['expect_role'])]!r}"
    )
