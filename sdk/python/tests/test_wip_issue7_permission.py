"""[wip] issue #7 — executor permission posture (repro tests, expected to FAIL on baseline 7327f18).

Baseline behaviour, both SDKs' in-process executor path:

* `{executor: claude-code, permission: true}` silently spawns
  `claude -p <msg> --output-format stream-json --verbose --dangerously-skip-permissions`
  — no `--permission-prompt-tool stdio`, no warning, workspace-wide auto-approve
  even though the profile asked for the approval channel.
* An executor with **no** permission channel (`codebuddy`, `gemini-cli`, `cursor`,
  `qwen-code`, …) is spawned with its auto-approve flag (`--dangerously-skip-permissions`
  / `--yolo`) with no warning either.
* There is no environment-level posture switch: no way to make the SDK refuse
  every auto-approve argv.

Required end state (issue acceptance + Rust `claude_code_permission_mode_uses_bidirectional_argv`):

1. `permission: true` + `executor: claude-code` → argv contains
   `--permission-prompt-tool stdio` and no `--dangerously-skip-permissions`.
2. `permission: true` + an executor with no permission channel → hard fail
   (error event, non-zero exit), never a silent skip-permissions / `--yolo` run.
3. `AGENTPROC_AUTO_APPROVE=0` → fail closed on every auto-approve argv, in both SDKs.
4. Those decisions, plus the per-executor support matrix, come from
   `spec/conformance/cases.json` (`posture_cases`) so Node and Python can't drift.

These tests are observable-only: a fake CLI on `PATH` records its own argv, so
they survive any internal signature change (e.g. `build_args` growing a
permission ctx argument). Remove the `[wip]` prefix when green.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentproc.executors import EXECUTORS  # noqa: E402
from agentproc.runner import RunOptions, run  # noqa: E402

CONFORMANCE_CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases.json"

# Fake CLI: record every argv token in <dir>/argv.txt, then report a clean turn.
_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$@" > {argv_file}
echo '{{"type":"result","result":"ok","session_id":"sess-1"}}'
"""

AUTO_APPROVE_FLAGS = (
    "--dangerously-skip-permissions",
    "--yolo",
    "--always-approve",
    "--yes-always",
    "--approve",
    "--auto",
)


class PermissionPostureRepro(unittest.TestCase):
    def _run(self, executor_name: str, permission, env=None, extra_env=None, message="hi"):
        """Run `executor_name` with a fake CLI on PATH; return (RunResult, argv or None)."""
        cli_name = EXECUTORS[executor_name]["cli_name"]
        tmpdir = tempfile.mkdtemp(prefix="ap-issue7-")
        argv_file = Path(tmpdir) / "argv.txt"
        shim = Path(tmpdir) / cli_name
        shim.write_text(_SHIM.format(argv_file=str(argv_file)))
        shim.chmod(0o755)

        environ = {"PATH": tmpdir + os.pathsep + os.environ["PATH"]}
        environ.update(env or {})
        profile = {"executor": executor_name}
        if permission is not None:
            profile["permission"] = permission
        with patch.dict(os.environ, environ, clear=False):
            result = run(profile, RunOptions(message=message, extra_env=extra_env or {}))

        argv = argv_file.read_text().splitlines() if argv_file.exists() else None
        return result, argv

    # -- (1) claude-code permission mode ---------------------------------

    def test_claude_code_permission_true_uses_permission_argv(self):
        result, argv = self._run("claude-code", permission=True)
        self.assertIsNotNone(argv, "claude-code was never spawned")
        self.assertIn("--permission-prompt-tool", argv)
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "stdio")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(result.error, "")
        self.assertEqual(result.exit_code, 0)

    def test_claude_code_permission_absent_keeps_unattended_argv(self):
        result, argv = self._run("claude-code", permission=None)
        self.assertIsNotNone(argv, "claude-code was never spawned")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--permission-prompt-tool", argv)
        self.assertEqual(result.error, "")
        self.assertEqual(result.exit_code, 0)

    # -- (2) executor without a permission channel -----------------------

    def test_permission_true_with_channelless_executor_hard_fails(self):
        result, argv = self._run("codebuddy", permission=True)
        self.assertNotEqual(result.error, "", "expected a hard failure, got a silent run")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("permission", result.error.lower())
        if argv is not None:
            self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_permission_true_with_yolo_executor_hard_fails(self):
        result, argv = self._run("gemini-cli", permission=True)
        self.assertNotEqual(result.error, "")
        self.assertNotEqual(result.exit_code, 0)
        if argv is not None:
            self.assertNotIn("--yolo", argv)

    # -- (3) AGENTPROC_AUTO_APPROVE=0 posture switch ---------------------

    def test_auto_approve_off_refuses_skip_permissions(self):
        result, argv = self._run(
            "claude-code", permission=None, env={"AGENTPROC_AUTO_APPROVE": "0"},
            extra_env={"AGENTPROC_AUTO_APPROVE": "0"},
        )
        self.assertNotEqual(result.error, "", "expected fail-closed, got a skip-permissions run")
        self.assertNotEqual(result.exit_code, 0)
        if argv is not None:
            for flag in AUTO_APPROVE_FLAGS:
                self.assertNotIn(flag, argv)

    def test_auto_approve_off_refuses_yolo(self):
        result, argv = self._run(
            "gemini-cli", permission=None, env={"AGENTPROC_AUTO_APPROVE": "0"},
            extra_env={"AGENTPROC_AUTO_APPROVE": "0"},
        )
        self.assertNotEqual(result.error, "")
        self.assertNotEqual(result.exit_code, 0)
        if argv is not None:
            self.assertNotIn("--yolo", argv)

    def test_auto_approve_off_still_allows_permission_mode(self):
        result, argv = self._run(
            "claude-code", permission=True, env={"AGENTPROC_AUTO_APPROVE": "0"},
            extra_env={"AGENTPROC_AUTO_APPROVE": "0"},
        )
        self.assertEqual(result.error, "")
        self.assertEqual(result.exit_code, 0)
        self.assertIsNotNone(argv)
        self.assertIn("--permission-prompt-tool", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    # -- (4) single source of truth: spec/conformance/cases.json ---------

    def test_conformance_posture_cases(self):
        data = json.loads(CONFORMANCE_CASES.read_text(encoding="utf-8"))
        if "posture_cases" not in data:
            self.fail("spec/conformance/cases.json has no posture_cases section")
        self.assertTrue(data["posture_cases"], "posture_cases is empty")
        for case in data["posture_cases"]:
            with self.subTest(case=case["name"]):
                result, argv = self._run(
                    case["executor"],
                    permission=case.get("permission"),
                    env=case.get("env") or {},
                    extra_env=case.get("env") or {},
                )
                expect = case["expect"]
                if expect.get("error"):
                    self.assertNotEqual(result.error, "", f"{case['name']}: expected an error")
                    self.assertNotEqual(result.exit_code, 0)
                if expect.get("exit_zero"):
                    self.assertEqual(result.error, "", f"{case['name']}: {result.error}")
                    self.assertEqual(result.exit_code, 0)
                if expect.get("reply") is not None:
                    self.assertEqual(result.reply, expect["reply"])
                if argv is not None:
                    for token in expect.get("argv_contains", []):
                        self.assertIn(token, argv, f"{case['name']}: {token} missing from {argv}")
                    for token in expect.get("argv_excludes", []):
                        self.assertNotIn(token, argv, f"{case['name']}: {token} present in {argv}")
                elif expect.get("argv_contains"):
                    self.fail(f"{case['name']}: CLI never spawned, cannot check argv")


if __name__ == "__main__":
    unittest.main()
