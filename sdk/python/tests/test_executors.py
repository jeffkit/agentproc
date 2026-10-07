"""Tests for sdk/python/src/agentproc/executors.py and run_via_executor.

Mirrors the coverage in sdk/node/src/executors.test.js.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agentproc.executors import EXECUTORS, executor_names
from agentproc.runner import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    RunOptions,
    RunResult,
    run_via_executor,
)


# ---------------------------------------------------------------------------
# Registry shape tests
# ---------------------------------------------------------------------------

class TestRegistry(unittest.TestCase):
    REQUIRED_NAMES = [
        "claude-code", "codebuddy", "codex", "cursor", "gemini-cli", "grok-build",
        "kimi-code", "opencode", "qwen-code", "agy", "aider", "deepseek", "dsh", "pi",
    ]

    def test_executor_names_list(self):
        for name in self.REQUIRED_NAMES:
            self.assertIn(name, executor_names, f"executor '{name}' missing from executor_names")

    def test_executors_dict_keys(self):
        for name in self.REQUIRED_NAMES:
            self.assertIn(name, EXECUTORS, f"executor '{name}' missing from EXECUTORS")

    def test_each_executor_has_cli_name(self):
        for name, ex in EXECUTORS.items():
            self.assertIn("cli_name", ex, f"executor '{name}' missing cli_name")
            self.assertIsInstance(ex["cli_name"], str)

    def test_each_executor_has_install_hint(self):
        for name, ex in EXECUTORS.items():
            self.assertIn("install_hint", ex, f"executor '{name}' missing install_hint")
            self.assertIsInstance(ex["install_hint"], str)

    def test_plain_executors(self):
        plain_executors = {"agy", "aider", "deepseek", "dsh", "pi"}
        for name in plain_executors:
            self.assertTrue(EXECUTORS[name].get("plain"), f"executor '{name}' should be plain")

    def test_supports_permission_only_for_claude_code(self):
        supporting = [n for n in self.REQUIRED_NAMES if EXECUTORS[n].get("supports_permission")]
        self.assertEqual(supporting, ["claude-code"])

    def test_ndjson_executors_have_parse_event_or_make_handlers(self):
        ndjson_executors = {"claude-code", "codebuddy", "codex", "cursor", "gemini-cli",
                           "grok-build", "kimi-code", "opencode", "qwen-code"}
        for name in ndjson_executors:
            ex = EXECUTORS[name]
            has_parse_event = callable(ex.get("parse_event"))
            has_make_handlers = callable(ex.get("make_handlers"))
            self.assertTrue(
                has_parse_event or has_make_handlers,
                f"NDJSON executor '{name}' needs parse_event or make_handlers"
            )


# ---------------------------------------------------------------------------
# build_args tests for pure-function executors
# ---------------------------------------------------------------------------

class TestClaudeCodeBuildArgs(unittest.TestCase):
    def _build(self, message="hello", session_id="", env=None):
        ex = EXECUTORS["claude-code"]
        return ex["build_args"](message, session_id, env or {})

    def test_basic_args(self):
        args = self._build("hello")
        self.assertIn("claude", args)
        self.assertIn("-p", args)
        self.assertIn("hello", args)
        self.assertIn("--output-format", args)
        self.assertIn("stream-json", args)
        self.assertIn("--dangerously-skip-permissions", args)

    def test_resume_when_session_id_present(self):
        args = self._build("hi", "my-session")
        self.assertIn("--resume", args)
        idx = args.index("--resume")
        self.assertEqual(args[idx + 1], "my-session")

    def test_no_resume_when_empty_session_id(self):
        args = self._build("hi", "")
        self.assertNotIn("--resume", args)

    def test_model_flag(self):
        args = self._build(env={"CLAUDE_MODEL": "claude-opus-4-5"})
        self.assertIn("--model", args)
        self.assertIn("claude-opus-4-5", args)

    def test_disallowed_tools(self):
        args = self._build(env={"CLAUDE_DISALLOW_TOOLS": "Bash"})
        self.assertIn("--disallowed-tools", args)
        self.assertIn("Bash", args)

    def test_default_disallowed_tools(self):
        args = self._build()
        self.assertIn("--disallowed-tools", args)
        idx = args.index("--disallowed-tools")
        self.assertEqual(args[idx + 1], "AskUserQuestion")

    def test_permission_ctx_switches_to_approval_channel(self):
        ex = EXECUTORS["claude-code"]
        args = ex["build_args"]("hello", "", {}, {"permission": True})
        self.assertIn("--permission-prompt-tool", args)
        self.assertEqual(args[args.index("--permission-prompt-tool") + 1], "stdio")
        self.assertIn("--print", args)
        self.assertIn("--input-format", args)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("hello", args)


class TestCodexBuildArgs(unittest.TestCase):
    def _build(self, message="hi", session_id="", env=None):
        ex = EXECUTORS["codex"]
        return ex["build_args"](message, session_id, env or {})

    def test_basic(self):
        args = self._build("test")
        self.assertIn("codex", args)
        self.assertIn("--json", args)
        self.assertIn("test", args)

    def test_model_config(self):
        args = self._build(env={"CODEX_MODEL": "gpt-4o"})
        # codex uses -c model="..." syntax
        self.assertTrue(any("gpt-4o" in a for a in args))


class TestAiderBuildArgs(unittest.TestCase):
    def _build(self, message="hi", session_id="", env=None):
        ex = EXECUTORS["aider"]
        return ex["build_args"](message, session_id, env or {})

    def test_basic(self):
        args = self._build("fix bug")
        self.assertIn("aider", args)
        self.assertIn("--message", args)
        self.assertIn("fix bug", args)
        self.assertIn("--yes-always", args)

    def test_no_stream_flag(self):
        args = self._build()
        self.assertIn("--no-stream", args)


class TestDeepSeekBuildArgs(unittest.TestCase):
    def _build(self, message="hi", session_id="", env=None):
        ex = EXECUTORS["deepseek"]
        return ex["build_args"](message, session_id, env or {})

    def test_basic(self):
        args = self._build("hello")
        self.assertIn("deepseek", args)
        self.assertIn("hello", args)


class TestDshBuildArgs(unittest.TestCase):
    def _build(self, message="hi", session_id="", env=None):
        ex = EXECUTORS["dsh"]
        return ex["build_args"](message, session_id, env or {})

    def test_basic(self):
        args = self._build("hello")
        self.assertEqual(args, ["dsh", "--profile", "headless", "hello"])


class TestPiBuildArgs(unittest.TestCase):
    def _build(self, message="hi", session_id="", env=None):
        ex = EXECUTORS["pi"]
        return ex["build_args"](message, session_id, env or {})

    def test_basic(self):
        args = self._build("hello")
        self.assertIn("pi", args)
        self.assertIn("hello", args)
        self.assertIn("--approve", args)


# ---------------------------------------------------------------------------
# agy makeHandlers tests
# ---------------------------------------------------------------------------

class TestAgyMakeHandlers(unittest.TestCase):
    def test_generates_uuid_when_no_session_id(self):
        ex = EXECUTORS["agy"]
        handlers = ex["make_handlers"]()
        args = handlers["build_args"]("hi", "", {})
        sid = handlers["get_session_id"]()
        self.assertIn("--conversation", args)
        idx = args.index("--conversation")
        self.assertEqual(args[idx + 1], sid)
        self.assertTrue(len(sid) > 0)

    def test_reuses_existing_session_id(self):
        ex = EXECUTORS["agy"]
        handlers = ex["make_handlers"]()
        args = handlers["build_args"]("hi", "existing-session", {})
        sid = handlers["get_session_id"]()
        self.assertEqual(sid, "existing-session")
        idx = args.index("--conversation")
        self.assertEqual(args[idx + 1], "existing-session")

    def test_per_turn_isolation(self):
        ex = EXECUTORS["agy"]
        h1 = ex["make_handlers"]()
        h2 = ex["make_handlers"]()
        h1["build_args"]("first", "", {})
        h2["build_args"]("second", "second-session", {})
        self.assertNotEqual(h1["get_session_id"](), h2["get_session_id"]())

    def test_dangerously_skip_permissions_default(self):
        ex = EXECUTORS["agy"]
        handlers = ex["make_handlers"]()
        args = handlers["build_args"]("hi", "", {})
        self.assertIn("--dangerously-skip-permissions", args)

    def test_model_flag(self):
        ex = EXECUTORS["agy"]
        handlers = ex["make_handlers"]()
        args = handlers["build_args"]("hi", "", {"AGY_MODEL": "my-model"})
        self.assertIn("--model", args)
        self.assertIn("my-model", args)


# ---------------------------------------------------------------------------
# kimi-code makeHandlers tests
# ---------------------------------------------------------------------------

class TestKimiCodeMakeHandlers(unittest.TestCase):
    def test_has_make_handlers(self):
        ex = EXECUTORS["kimi-code"]
        self.assertTrue(callable(ex.get("make_handlers")))

    def test_handlers_have_build_args_and_parse_event(self):
        ex = EXECUTORS["kimi-code"]
        handlers = ex["make_handlers"]()
        self.assertTrue(callable(handlers.get("build_args")))
        self.assertTrue(callable(handlers.get("parse_event")))

    def test_per_turn_isolation(self):
        ex = EXECUTORS["kimi-code"]
        h1 = ex["make_handlers"]()
        h2 = ex["make_handlers"]()
        # Each call produces a fresh handlers object
        self.assertIsNot(h1, h2)


# ---------------------------------------------------------------------------
# run_via_executor tests
# ---------------------------------------------------------------------------

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


class TestRunViaExecutorPlain(unittest.TestCase):
    """run_via_executor for plain executors."""

    def _make_plain_executor(self):
        session = {"id": None}

        def build_args(message, session_id, env, ctx):
            session["id"] = session_id or "generated-id"
            return ["echo", message]

        def get_session_id():
            return session["id"]

        return {
            "cli_name": "echo-plain",
            "install_hint": "",
            "plain": True,
            "make_handlers": lambda: {
                "build_args": build_args,
                "get_session_id": get_session_id,
            },
        }

    def test_plain_executor_returns_reply(self):
        ex = self._make_plain_executor()
        result = run_via_executor(ex, _make_opts(message="world"))
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.reply, "world")

    def test_plain_executor_propagates_session_id(self):
        ex = self._make_plain_executor()
        result = run_via_executor(ex, _make_opts(message="hi", session_id="my-sess"))
        self.assertEqual(result.session_id, "my-sess")

    def test_on_session_callback_called(self):
        captured = []
        ex = self._make_plain_executor()
        run_via_executor(
            ex, _make_opts(message="hi", session_id="cb-sess", on_session=captured.append)
        )
        self.assertIn("cb-sess", captured)

    def test_missing_command_returns_error(self):
        def build_args(message, session_id, env, ctx):
            return ["__nonexistent_cmd_agentproc__", message]

        ex = {
            "cli_name": "missing",
            "install_hint": "install it",
            "plain": True,
            "build_args": build_args,
        }
        errors = []
        result = run_via_executor(ex, _make_opts(on_error=errors.append))
        self.assertEqual(result.exit_code, EXIT_ERROR)
        self.assertTrue(errors)


class TestRunViaExecutorNDJSON(unittest.TestCase):
    """run_via_executor for NDJSON executors."""

    def _make_ndjson_executor(self, output_lines):
        joined = "\n".join(output_lines)

        def build_args(message, session_id, env, ctx):
            return ["echo", joined]

        def parse_event(event):
            t = event.get("type")
            if t == "partial":
                return {"partial_text": event.get("text")}
            if t == "result":
                return {
                    "final_text": event.get("text"),
                    "session_id": event.get("session_id", ""),
                }
            return None

        return {
            "cli_name": "echo-ndjson",
            "install_hint": "",
            "plain": False,
            "build_args": build_args,
            "parse_event": parse_event,
        }

    def test_ndjson_reply_assembled(self):
        import json
        lines = [
            json.dumps({"type": "partial", "text": "hello "}),
            json.dumps({"type": "result", "text": "world", "session_id": "s1"}),
        ]
        ex = self._make_ndjson_executor(lines)
        partials = []
        result = run_via_executor(ex, _make_opts(on_partial=partials.append))
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        # streaming + partials forwarded → body already delivered via
        # on_partial; reply must not duplicate it
        self.assertEqual(result.reply, "")
        self.assertEqual(partials, ["hello "])
        self.assertEqual(result.session_id, "s1")

    def test_ndjson_on_partial_role_is_none(self):
        import json
        lines = [json.dumps({"type": "partial", "text": "hello "})]
        ex = self._make_ndjson_executor(lines)
        seen = []
        run_via_executor(
            ex, _make_opts(on_partial=lambda text, role=None: seen.append((text, role)))
        )
        # `ParseResult` has no `role` — the second argument is always None here.
        self.assertEqual(seen, [("hello ", None)])

    def test_ndjson_non_streaming_reply_is_final_text(self):
        import json
        lines = [
            json.dumps({"type": "partial", "text": "hello "}),
            json.dumps({"type": "result", "text": "world", "session_id": "s1"}),
        ]
        ex = self._make_ndjson_executor(lines)
        result = run_via_executor(ex, _make_opts(streaming=False))
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.reply, "world")
        self.assertEqual(result.session_id, "s1")


class TestOnProtocolLineContract(unittest.TestCase):
    """协议契约：executor 路径的每条 stdout 行都过 on_protocol_line
    （与 spawn 路径对齐——观测/审计消费原始行的依据）。"""

    def test_plain_path_forwards_lines(self):
        ex = {
            "cli_name": "echo-plain", "install_hint": "", "plain": True,
            "build_args": lambda m, s, e, c: ["echo", '{"type":"result","text":"ok"}'],
        }
        seen = []
        result = run_via_executor(ex, _make_opts(on_protocol_line=seen.append))
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertTrue(any("result" in l for l in seen), seen)

    def test_ndjson_path_forwards_lines(self):
        import json
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hi"}]}}),
            json.dumps({"type": "result", "result": "hi", "usage":
                        {"input_tokens": 3, "output_tokens": 1}}),
        ]
        ex = {
            "cli_name": "echo-ndjson", "install_hint": "", "plain": False,
            # printf 对每个参数复用格式串 → 每行一个事件
            "build_args": lambda m, s, e, c: (
                ["printf", "%s\n"] + lines),
            "parse_event": lambda e: (
                {"final_text": e.get("result")} if e.get("type") == "result" else None),
        }
        seen = []
        result = run_via_executor(ex, _make_opts(on_protocol_line=seen.append))
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(len(seen), 2, seen)
        # usage 兜底消费方可从原始行取回 result 事件的 usage
        result_event = json.loads(seen[-1])
        self.assertEqual(result_event["usage"]["input_tokens"], 3)


# ---------------------------------------------------------------------------
# run() routing via executor: field in profile
# ---------------------------------------------------------------------------

class TestRunWithExecutorProfile(unittest.TestCase):
    def test_unknown_executor_no_command_returns_error(self):
        """Case 4: unknown executor + no command → hard fail."""
        from agentproc.runner import run
        opts = _make_opts()
        result = run({"executor": "nonexistent-executor"}, opts)
        self.assertEqual(result.exit_code, EXIT_ERROR)
        self.assertIn("nonexistent-executor", result.error)

    def test_unknown_executor_with_command_falls_back_to_spawn(self):
        """Case 3: unknown executor + command present → warn + fallback spawn."""
        import sys
        from agentproc.runner import run
        stderr_lines = []
        errors = []
        # Use `echo` as a trivial command that always exits 0.
        # The profile has an unknown executor but also a command, so it should
        # warn and fall back to spawning the command directly.
        echo_cmd = "echo" if sys.platform != "win32" else "cmd"
        result = run(
            {"executor": "nonexistent-executor", "command": echo_cmd},
            _make_opts(
                message="hi",
                on_stderr=stderr_lines.append,
                on_error=errors.append,
            ),
        )
        # Must have emitted a warning about the unknown executor
        warn_text = " ".join(stderr_lines)
        self.assertIn("nonexistent-executor", warn_text)
        self.assertIn("falling back to spawn", warn_text)
        # Must NOT have returned an "unknown executor" error result
        self.assertNotIn("Unknown executor", result.error or "")

    def test_profile_with_executor_field_routes_correctly(self):
        """A profile with executor: agy should use the agy executor (even if CLI absent)."""
        from agentproc.runner import run
        errors = []
        result = run({"executor": "agy"}, _make_opts(message="hi", on_error=errors.append))
        # The executor runs; if agy is not installed, we get a "command not found" error
        # rather than a profile error — proving routing worked.
        if result.exit_code != EXIT_SUCCESS:
            combined = (result.error or "") + " ".join(errors)
            # Either agy ran fine, or we got a not-found / install error
            self.assertTrue(
                "agy" in combined or "not found" in combined or "install" in combined.lower(),
                f"Unexpected error: {combined}"
            )


# ---------------------------------------------------------------------------
# Permission posture on the executor path (spec doc 1.6)
# ---------------------------------------------------------------------------

# Fake CLI: record every argv token in <dir>/argv.txt, then report a clean turn.
_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$@" > {argv_file}
echo '{{"type":"result","result":"ok","session_id":"sess-1"}}'
"""


class TestPermissionPosture(unittest.TestCase):
    """`permission: true` and `AGENTPROC_AUTO_APPROVE=0` must fail closed.

    Observable-only: a fake CLI on PATH records its own argv, so the test
    pins what the CLI would have received (or that it was never spawned).
    """

    def _run(self, executor_name, permission=None, env=None):
        from agentproc.runner import run
        cli_name = EXECUTORS[executor_name]["cli_name"]
        env = env or {}
        with tempfile.TemporaryDirectory(prefix="ap-posture-") as tmpdir:
            argv_file = Path(tmpdir) / "argv.txt"
            shim = Path(tmpdir) / cli_name
            shim.write_text(_SHIM.format(argv_file=str(argv_file)))
            shim.chmod(0o755)
            # `env` is written to the runner process environment and to the
            # per-run env extras, so AGENTPROC_AUTO_APPROVE reaches the runner
            # regardless of which of the two it reads.
            environ = {
                "PATH": tmpdir + os.pathsep + os.environ["PATH"],
                "AGENTPROC_AUTO_APPROVE": env.get("AGENTPROC_AUTO_APPROVE", ""),
            }
            profile = {"executor": executor_name}
            if permission is not None:
                profile["permission"] = permission
            with patch.dict(os.environ, environ, clear=False):
                result = run(profile, RunOptions(message="hi", extra_env=env))
            argv = argv_file.read_text().splitlines() if argv_file.exists() else None
        return result, argv

    def test_permission_true_with_channelless_executor_is_never_spawned(self):
        result, argv = self._run("gemini-cli", permission=True)
        self.assertIsNone(argv, f"gemini-cli was spawned with {argv}")
        self.assertNotEqual(result.error, "", "expected a hard failure, got a silent run")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("permission", result.error.lower())

    def test_auto_approve_off_refuses_auto_approve_argv(self):
        result, argv = self._run(
            "gemini-cli", env={"AGENTPROC_AUTO_APPROVE": "0"},
        )
        self.assertIsNone(argv, f"gemini-cli was spawned with {argv}")
        self.assertNotEqual(result.error, "")
        self.assertNotEqual(result.exit_code, 0)

    def test_auto_approve_off_still_allows_the_approval_channel(self):
        result, argv = self._run(
            "claude-code", permission=True, env={"AGENTPROC_AUTO_APPROVE": "0"},
        )
        self.assertIsNotNone(argv, f"claude-code was never spawned: {result.error}")
        self.assertIn("--permission-prompt-tool", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(result.error, "")
        self.assertEqual(result.exit_code, 0)

    def test_auto_approve_off_is_case_insensitive(self):
        for value in ("0", "FALSE", "  false  "):
            with self.subTest(value=value):
                result, argv = self._run(
                    "claude-code", env={"AGENTPROC_AUTO_APPROVE": value},
                )
                self.assertIsNone(argv, f"{value!r} did not fail closed: {argv}")
                self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
