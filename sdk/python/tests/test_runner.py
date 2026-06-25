"""Tests for agentproc.runner — the canonical bridge-side implementation."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import List

import pytest

from agentproc.runner import (
    PROTOCOL_VERSION,
    RunOptions,
    classify_line,
    decode_json_value,
    expand_env_ref,
    expand_path,
    normalize_profile,
    run,
    substitute,
)


# ---------------------------------------------------------------------------
# 1. Pure-function tests
# ---------------------------------------------------------------------------

class TestClassifyLine:
    def test_session_line(self):
        assert classify_line("AGENT_SESSION:abc-123") == {"kind": "session", "value": "abc-123"}

    def test_session_strips_whitespace(self):
        assert classify_line("AGENT_SESSION:  abc-123  ") == {"kind": "session", "value": "abc-123"}

    def test_partial_json_string(self):
        assert classify_line('AGENT_PARTIAL:"hello"') == {"kind": "partial", "value": "hello"}

    def test_partial_with_newline_in_json(self):
        assert classify_line('AGENT_PARTIAL:"line1\\nline2"') == {"kind": "partial", "value": "line1\nline2"}

    def test_partial_lenient_on_bad_json(self):
        assert classify_line("AGENT_PARTIAL:not json") == {"kind": "partial", "value": "not json"}

    def test_partial_empty_value(self):
        assert classify_line("AGENT_PARTIAL:") == {"kind": "partial", "value": ""}

    def test_error_line(self):
        assert classify_line('AGENT_ERROR:"rate limited"') == {"kind": "error", "value": "rate limited"}

    def test_error_lenient_on_bad_json(self):
        assert classify_line("AGENT_ERROR:boom") == {"kind": "error", "value": "boom"}

    def test_body_line(self):
        assert classify_line("hello world") == {"kind": "body", "value": "hello world"}

    def test_line_starting_with_space_is_not_protocol(self):
        assert classify_line(" AGENT_SESSION:foo") == {"kind": "body", "value": " AGENT_SESSION:foo"}

    def test_prefix_like_but_not_exact(self):
        assert classify_line("AGENT_SESSION") == {"kind": "body", "value": "AGENT_SESSION"}


class TestDecodeJsonValue:
    def test_json_string(self):
        assert decode_json_value('"hi"') == "hi"

    def test_json_string_with_newline(self):
        assert decode_json_value('"a\\nb"') == "a\nb"

    def test_empty(self):
        assert decode_json_value("") == ""

    def test_non_json_returns_trimmed_raw(self):
        assert decode_json_value("  not json  ") == "not json"

    def test_json_number(self):
        assert decode_json_value("42") == "42"


class TestSubstitute:
    def test_message(self):
        assert substitute("You said: {{MESSAGE}}", {"message": "hi"}) == "You said: hi"

    def test_session_id(self):
        assert substitute("s={{SESSION_ID}}", {"session_id": "abc"}) == "s=abc"

    def test_session_name(self):
        assert substitute("n={{SESSION_NAME}}", {"session_name": "work"}) == "n=work"

    def test_empty_session_id(self):
        assert substitute("s={{SESSION_ID}}", {"session_id": ""}) == "s="

    def test_multiple_placeholders(self):
        out = substitute(
            "{{MESSAGE}} [{{SESSION_ID}}] ({{SESSION_NAME}})",
            {"message": "hi", "session_id": "s1", "session_name": "work"},
        )
        assert out == "hi [s1] (work)"


class TestExpandEnvRef:
    def test_known_var(self):
        assert expand_env_ref("${HOME}", {"HOME": "/u/x"}) == "/u/x"

    def test_unknown_var_becomes_empty(self):
        assert expand_env_ref("${NOPE}", {}) == ""

    def test_no_refs(self):
        assert expand_env_ref("plain value", {}) == "plain value"

    def test_mixed(self):
        assert expand_env_ref("key=${HOME} and ${missing}", {"HOME": "/h"}) == "key=/h and "


class TestExpandPath:
    def test_tilde_alone(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert expand_path("~") == str(tmp_path)

    def test_tilde_slash(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert expand_path("~/foo") == str(tmp_path / "foo")

    def test_absolute_unchanged(self):
        assert expand_path("/usr/bin") == "/usr/bin"

    def test_relative_unchanged(self):
        assert expand_path("./foo") == "./foo"


class TestNormalizeProfile:
    def test_minimal_valid(self):
        p = normalize_profile({"command": "bash ./x.sh"})
        assert p["argv"] == ["bash", "./x.sh"]
        assert p["stdin"] == "none"
        assert p["streaming"] is True

    def test_hub_form(self):
        p = normalize_profile({"agentproc": {"command": "node ./x.js"}})
        assert p["argv"] == ["node", "./x.js"]

    def test_rejects_missing_command(self):
        with pytest.raises(ValueError, match="command must be a non-empty string"):
            normalize_profile({})

    def test_rejects_empty_command(self):
        with pytest.raises(ValueError, match="command must be a non-empty string"):
            normalize_profile({"command": "   "})

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            normalize_profile(None)  # type: ignore

    def test_argv_splits_on_whitespace(self):
        p = normalize_profile({"command": "bash    ./spaced.sh"})
        assert p["argv"] == ["bash", "./spaced.sh"]

    def test_args_default_empty(self):
        p = normalize_profile({"command": "x"})
        assert p["args"] == []

    def test_args_cast_to_str(self):
        p = normalize_profile({"command": "x", "args": ["--foo", 42]})
        assert p["args"] == ["--foo", "42"]

    def test_cwd_tilde_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        p = normalize_profile({"command": "x", "cwd": "~/proj"})
        assert p["cwd"] == str(tmp_path / "proj")

    def test_stdin_message_vs_other(self):
        assert normalize_profile({"command": "x", "stdin": "message"})["stdin"] == "message"
        assert normalize_profile({"command": "x", "stdin": "none"})["stdin"] == "none"
        assert normalize_profile({"command": "x", "stdin": "bogus"})["stdin"] == "none"
        assert normalize_profile({"command": "x"})["stdin"] == "none"

    def test_streaming_false_honored(self):
        assert normalize_profile({"command": "x", "streaming": False})["streaming"] is False
        assert normalize_profile({"command": "x", "streaming": True})["streaming"] is True
        assert normalize_profile({"command": "x"})["streaming"] is True


def test_protocol_version_is_0_1():
    assert PROTOCOL_VERSION == "0.1"


# ---------------------------------------------------------------------------
# 2. run() end-to-end tests with tiny agent scripts
# ---------------------------------------------------------------------------

def write_script(content: str, tmp_path: Path) -> Path:
    f = tmp_path / "agent.sh"
    f.write_text(content)
    f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return f


@pytest.fixture
def agent_script(tmp_path):
    def _make(content: str) -> Path:
        return write_script(content, tmp_path)
    return _make


class TestRunEndToEnd:
    def test_simple_reply_body(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\necho 'hello'\n")
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.reply.strip() == "hello"
        assert r.session_id == ""
        assert r.error == ""
        assert r.exit_code == 0

    def test_session_last_wins(self, agent_script):
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "AGENT_SESSION:first"
            echo "AGENT_SESSION:second"
            echo "done"
        """))
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.session_id == "second"
        assert r.reply.strip() == "done"

    def test_partial_triggers_callback(self, agent_script):
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo 'AGENT_PARTIAL:"chunk1"'
            echo 'AGENT_PARTIAL:"chunk2"'
            echo "final"
        """))
        partials: List[str] = []
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", on_partial=partials.append),
        )
        assert partials == ["chunk1", "chunk2"]
        assert r.reply.strip() == "final"

    def test_partial_skipped_when_streaming_false(self, agent_script):
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo 'AGENT_PARTIAL:"chunk1"'
            echo "final"
        """))
        partials: List[str] = []
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", streaming=False, on_partial=partials.append),
        )
        assert partials == []
        assert r.reply.strip() == "final"

    def test_error_surfaces(self, agent_script):
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo 'AGENT_PARTIAL:"thinking..."'
            echo 'AGENT_ERROR:"rate limited"'
            exit 1
        """))
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.error == "rate limited"
        assert r.exit_code == 1

    def test_error_marks_exit_1_even_if_process_exits_0(self, agent_script):
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo 'AGENT_ERROR:"soft fail"'
            exit 0
        """))
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.error == "soft fail"
        assert r.exit_code == 1

    def test_body_lines_with_leading_space_not_treated_as_protocol(self, agent_script):
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo " AGENT_SESSION:foo"
            echo "real reply"
        """))
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.session_id == ""
        assert "\n" in r.reply

    def test_exit_code_propagates(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\nexit 3\n")
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.exit_code == 3

    def test_message_injected_as_env(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\necho \"got: $AGENT_MESSAGE\"\n")
        r = run({"command": str(agent)}, RunOptions(message="payload"))
        assert r.reply.strip() == "got: payload"

    def test_session_id_injected_from_options(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\necho \"prev: $AGENT_SESSION_ID\"\n")
        r = run({"command": str(agent)}, RunOptions(message="hi", session_id="prev-123"))
        assert r.reply.strip() == "prev: prev-123"

    def test_protocol_version_injected(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\necho \"pv=$AGENT_PROTOCOL_VERSION\"\n")
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.reply.strip() == f"pv={PROTOCOL_VERSION}"

    def test_streaming_reflects_option(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\necho \"stream=$AGENT_STREAMING\"\n")
        r1 = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r1.reply.strip() == "stream=1"
        r2 = run({"command": str(agent)}, RunOptions(message="hi", streaming=False))
        assert r2.reply.strip() == "stream=0"

    def test_profile_env_with_var_ref(self, agent_script, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "/some/path")
        agent = agent_script("#!/usr/bin/env bash\necho \"v=$MY_KEY\"\n")
        r = run(
            {"command": str(agent), "env": {"MY_KEY": "${MY_TEST_VAR}"}},
            RunOptions(message="hi"),
        )
        assert r.reply.strip() == "v=/some/path"

    def test_message_placeholder_in_args(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\necho \"args: $1\"\n")
        r = run(
            {"command": str(agent), "args": ["{{MESSAGE}}"]},
            RunOptions(message="hello"),
        )
        assert r.reply.strip() == "args: hello"

    def test_extra_env_applied(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\necho \"x=$X\"\n")
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", extra_env={"X": "extra"}),
        )
        assert r.reply.strip() == "x=extra"

    def test_stdin_message_written_and_eof(self, tmp_path):
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/usr/bin/env bash\nread line\necho \"stdin: $line\"\n")
        agent.chmod(agent.stat().st_mode | 0o111)
        r = run(
            {"command": str(agent), "stdin": "message"},
            RunOptions(message="via-stdin"),
        )
        assert r.reply.strip() == "stdin: via-stdin"

    def test_timeout_kills_long_agent(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\nsleep 30\necho 'should not reach'\n")
        r = run(
            {"command": str(agent), "kill_grace_secs": 1},
            RunOptions(message="hi", timeout_secs=1),
        )
        assert r.timed_out is True
        assert r.exit_code == 124

    def test_multiline_reply_preserves_newlines(self, agent_script):
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "line 1"
            echo "line 2"
            echo "line 3"
        """))
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.reply == "line 1\nline 2\nline 3"

    def test_spawn_error_command_not_found(self):
        r = run(
            {"command": "/nonexistent/command/xyz"},
            RunOptions(message="hi"),
        )
        assert r.exit_code == 1
