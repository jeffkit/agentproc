"""Tests for sdk/python/src/agentproc/executors.py and run_via_executor.

Mirrors the coverage in sdk/node/src/executors.test.js.
"""
from __future__ import annotations

import os
import sys
import unittest
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
        "kimi-code", "opencode", "qwen-code", "agy", "aider", "deepseek", "pi",
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
        plain_executors = {"agy", "aider", "deepseek", "pi"}
        for name in plain_executors:
            self.assertTrue(EXECUTORS[name].get("plain"), f"executor '{name}' should be plain")

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

        def build_args(message, session_id, env):
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
        def build_args(message, session_id, env):
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

        def build_args(message, session_id, env):
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
        result = run_via_executor(ex, _make_opts())
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertIn("hello ", result.reply)
        self.assertIn("world", result.reply)
        self.assertEqual(result.session_id, "s1")


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


if __name__ == "__main__":
    unittest.main()
