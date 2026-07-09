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
    is_valid_session_id,
    decode_json_value,
    decode_json_object,
    format_permission_response,
    is_valid_permission_request,
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

    def test_permission_request(self):
        c = classify_line(
            'AGENT_PERMISSION_REQUEST:{"request_id":"1","tool_name":"Bash","input":{}}'
        )
        assert c["kind"] == "permission_request"
        assert c["value"] == {"request_id": "1", "tool_name": "Bash", "input": {}}

    def test_permission_request_malformed(self):
        c = classify_line("AGENT_PERMISSION_REQUEST:not-json")
        assert c["kind"] == "permission_request"
        assert c["value"] is None

    def test_body_line(self):
        assert classify_line("hello world") == {"kind": "body", "value": "hello world"}

    def test_line_starting_with_space_is_not_protocol(self):
        assert classify_line(" AGENT_SESSION:foo") == {"kind": "body", "value": " AGENT_SESSION:foo"}

    def test_prefix_like_but_not_exact(self):
        assert classify_line("AGENT_SESSION") == {"kind": "body", "value": "AGENT_SESSION"}


class TestPermissionHelpers:
    def test_format_allow(self):
        assert format_permission_response(
            {"request_id": "1", "behavior": "allow", "updated_input": {"c": "x"}}
        ) == 'AGENT_PERMISSION_RESPONSE:{"request_id": "1", "behavior": "allow", "updated_input": {"c": "x"}}'

    def test_format_deny(self):
        assert format_permission_response(
            {"request_id": "2", "behavior": "deny", "message": "nope"}
        ) == 'AGENT_PERMISSION_RESPONSE:{"request_id": "2", "behavior": "deny", "message": "nope"}'

    def test_is_valid(self):
        assert is_valid_permission_request(
            {"request_id": "1", "tool_name": "Bash", "input": {}}
        )
        assert not is_valid_permission_request(
            {"request_id": "1", "tool_name": "Bash"}
        )
        assert not is_valid_permission_request(
            {"request_id": "a b", "tool_name": "Bash", "input": {}}
        )
        assert not is_valid_permission_request(None)
        assert decode_json_object('{"a":1}') == {"a": 1}
        assert decode_json_object("[]") is None


class TestIsValidSessionId:
    def test_valid_uuid(self):
        assert is_valid_session_id("f47ac10b-58cc-4372-a567-0e02b2c3d479")

    def test_valid_cli_handle(self):
        assert is_valid_session_id("cli-sess-9f3a2c1e")

    def test_valid_short_token(self):
        assert is_valid_session_id("abc123")

    def test_empty_rejected(self):
        assert not is_valid_session_id("")

    def test_whitespace_rejected(self):
        assert not is_valid_session_id("has space")
        assert not is_valid_session_id("tab\there")

    def test_colon_rejected(self):
        assert not is_valid_session_id("thread:abc")

    def test_control_chars_rejected(self):
        assert not is_valid_session_id("ctrl\x07char")

    def test_url_safe_chars_allowed(self):
        # Valid set: letters, digits, . _ ~ = -  (no / or +)
        assert is_valid_session_id("a.b_c~d=e-h")

    def test_slash_rejected(self):
        # `/` is excluded so the id is safe as a <id>.jsonl filename component.
        assert not is_valid_session_id("a/b")
        assert not is_valid_session_id("../../tmp/x")

    def test_plus_rejected(self):
        # `+` is excluded to keep the "URL-safe" label honest.
        assert not is_valid_session_id("a+b")


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

    def test_allowlist_permits_listed_var(self):
        assert expand_env_ref("${HOME}", {"HOME": "/h"}, allowlist={"HOME"}) == "/h"

    def test_allowlist_blocks_unlisted_var(self):
        # AWS_SECRET is in env but not in allowlist → blocked, expands to empty.
        assert expand_env_ref(
            "${AWS_SECRET_ACCESS_KEY}", {"AWS_SECRET_ACCESS_KEY": "s3cr3t"},
            allowlist={"HOME"},
        ) == ""

    def test_allowlist_blocked_callback(self):
        blocked = []
        out = expand_env_ref(
            "${A} ${B}", {"A": "1", "B": "2"},
            allowlist={"A"},
            on_blocked=blocked.append,
        )
        assert out == "1 "
        assert blocked == ["B"]

    def test_allowlist_none_means_all_permitted(self):
        # allowlist=None (default) ⇒ no restriction, even on unknown vars.
        assert expand_env_ref("${ANYTHING}", {"ANYTHING": "x"}, allowlist=None) == "x"


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

    def test_permission_defaults_false(self):
        assert normalize_profile({"command": "x"})["permission"] is False
        assert normalize_profile({"command": "x", "permission": True})["permission"] is True
        assert normalize_profile({"command": "x", "permission": False})["permission"] is False
        assert normalize_profile({"command": "x", "permission": "true"})["permission"] is False

    def test_streaming_false_honored(self):
        assert normalize_profile({"command": "x", "streaming": False})["streaming"] is False
        assert normalize_profile({"command": "x", "streaming": True})["streaming"] is True
        assert normalize_profile({"command": "x"})["streaming"] is True

    def test_command_split_when_args_empty(self):
        """Legacy shorthand: `command: python3 ./bridge.py` with no args
        splits into argv on whitespace."""
        p = normalize_profile({"command": "python3 ./bridge.py"})
        assert p["argv"] == ["python3", "./bridge.py"]

    def test_command_kept_whole_when_args_present(self):
        """Explicit form: when args is non-empty, command is a single token
        (argv[0]) verbatim — lets paths with whitespace stay whole."""
        p = normalize_profile({
            "command": "/path with spaces/my agent",
            "args": ["--flag", "value"],
        })
        assert p["argv"] == ["/path with spaces/my agent"]
        assert p["args"] == ["--flag", "value"]

    def test_command_with_spaces_runs_end_to_end(self, tmp_path):
        """A profile whose executable path contains spaces must actually spawn,
        not be split into bogus argv. Uses the explicit form (args non-empty)
        so command is treated as a single argv token per spec."""
        nested = tmp_path / "has space"
        nested.mkdir()
        script = nested / "agent.sh"
        script.write_text("#!/usr/bin/env bash\necho \"ok: $AGENT_MESSAGE\"\n")
        script.chmod(script.stat().st_mode | 0o111)
        r = run(
            {"command": str(script), "args": ["{{MESSAGE}}"]},
            RunOptions(message="payload"),
        )
        assert r.reply.strip() == "ok: payload"
        assert r.exit_code == 0

    def test_command_kept_whole_with_empty_args_list(self):
        """`args: []` (explicit empty list) tells the bridge: do not split
        command. This is the escape hatch for a whitespace-bearing executable
        path that takes no extra argv tokens. Distinct from `args` absent."""
        p = normalize_profile({
            "command": "/path with spaces/my agent",
            "args": [],
        })
        assert p["argv"] == ["/path with spaces/my agent"]
        assert p["args"] == []

    def test_command_split_when_args_absent(self):
        """When `args` is absent entirely, the legacy shorthand applies and
        command is split on whitespace."""
        p = normalize_profile({"command": "python3 ./bridge.py"})
        assert p["argv"] == ["python3", "./bridge.py"]

    def test_args_none_treated_as_absent(self):
        """If someone explicitly sets `args: null`, treat as absent (shorthand
        applies). This protects hand-written YAML where `args:` with no value
        parses as None."""
        p = normalize_profile({"command": "python3 ./bridge.py", "args": None})
        assert p["argv"] == ["python3", "./bridge.py"]

    def test_env_allowlist_absent_is_none(self):
        p = normalize_profile({"command": "x", "env": {"A": "1"}})
        assert p["env_allowlist"] is None

    def test_env_allowlist_parsed_to_set(self):
        p = normalize_profile({"command": "x", "env_allowlist": ["A", "B"]})
        assert p["env_allowlist"] == {"A", "B"}

    def test_env_allowlist_non_list_raises(self):
        with pytest.raises(ValueError, match="env_allowlist"):
            normalize_profile({"command": "x", "env_allowlist": "A"})


def test_protocol_version_is_0_1():
    assert PROTOCOL_VERSION == "0.2"


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

    def test_env_allowlist_end_to_end(self, agent_script, monkeypatch):
        """Allowlist permits listed var, blocks unlisted var, warns via on_stderr."""
        monkeypatch.setenv("ALLOWED_KEY", "ok-val")
        monkeypatch.setenv("SECRET_KEY", "top-secret")
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo "ALLOWED=$ALLOWED_KEY"\n'
            'echo "SECRET=$SECRET_KEY"\n'
        )
        warnings: List[str] = []
        r = run(
            {
                "command": str(agent),
                "env": {
                    "ALLOWED_KEY": "${ALLOWED_KEY}",
                    "SECRET_KEY": "${SECRET_KEY}",
                },
                "env_allowlist": ["ALLOWED_KEY"],
            },
            RunOptions(message="hi", on_stderr=warnings.append),
        )
        assert "ALLOWED=ok-val" in r.reply
        # SECRET_KEY was blocked → expands to empty → agent sees empty value.
        assert "SECRET=" in r.reply
        assert "top-secret" not in r.reply
        # Bridge logged a warning about the blocked reference.
        assert any("SECRET_KEY" in w and "allowlist" in w for w in warnings)

    def test_env_allowlist_stops_undeclared_secrets_leaking(self, agent_script, monkeypatch):
        """A secret in the bridge env that the profile never declares must not
        reach the agent. Pre-0.5.2 the child inherited os.environ wholesale, so
        this leaked. With env_allowlist set, the child env is infra +
        profile.env + AGENT_* only."""
        monkeypatch.setenv("BRIDGE_AWS_SECRET", "aws-top-secret")
        monkeypatch.setenv("BRIDGE_DB_PASSWORD", "db-top-secret")
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo "AWS=${BRIDGE_AWS_SECRET:-unset}"\n'
            'echo "DB=${BRIDGE_DB_PASSWORD:-unset}"\n'
            'echo "PATH_SET=${PATH:+yes}"\n'
        )
        r = run(
            {
                "command": str(agent),
                # Profile declares AWS only; DB is left undeclared.
                "env": {"BRIDGE_AWS_SECRET": "${BRIDGE_AWS_SECRET}"},
                "env_allowlist": ["BRIDGE_AWS_SECRET"],
            },
            RunOptions(message="hi"),
        )
        assert "AWS=aws-top-secret" in r.reply  # declared + allowlisted → reaches agent
        assert "db-top-secret" not in r.reply   # undeclared secret must not leak
        assert "DB=unset" in r.reply
        assert "PATH_SET=yes" in r.reply        # infra (PATH) still present

    def test_env_allowlist_absent_child_inherits_full_env(self, agent_script, monkeypatch):
        """Back-compat: no allowlist → full os.environ inheritance, so an
        undeclared var leaks by design. Pins the documented back-compat
        boundary so the tightening above is clearly opt-in."""
        monkeypatch.setenv("AGENTPROC_BACKCOMPAT_LEAK", "leaked-by-design")
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo "LEAK=${AGENTPROC_BACKCOMPAT_LEAK:-unset}"\n'
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert "LEAK=leaked-by-design" in r.reply

    def test_attachment_passthrough_reaches_agent(self, agent_script):
        """RunOptions.image_url / file_url are injected as AGENT_IMAGE_URL /
        AGENT_FILE_URL on the spawned agent's environment."""
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo "IMG=$AGENT_IMAGE_URL"\n'
            'echo "FILE=$AGENT_FILE_URL"\n'
        )
        r = run(
            {"command": str(agent)},
            RunOptions(
                message="hi",
                image_url="https://example.com/a.png",
                file_url="https://example.com/b.pdf",
            ),
        )
        assert "IMG=https://example.com/a.png" in r.reply
        assert "FILE=https://example.com/b.pdf" in r.reply

    def test_attachment_unset_when_options_empty(self, agent_script):
        """When image_url/file_url are empty, the runner must NOT inject the
        env vars at all — an agent can then distinguish "no image" from
        "image URL is the empty string"."""
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo "IMG=<${AGENT_IMAGE_URL:-unset}>"\n'
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert "IMG=<unset>" in r.reply

    def test_invalid_session_id_ignored_preserves_previous(self, agent_script):
        """A malformed AGENT_SESSION line is ignored; the previous valid id wins."""
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "AGENT_SESSION:valid-id-1"
            echo "AGENT_SESSION:bad:with:colons"
            echo "done"
        """))
        warnings: List[str] = []
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", on_stderr=warnings.append),
        )
        # The invalid line did not overwrite the valid one.
        assert r.session_id == "valid-id-1"
        assert r.reply.strip() == "done"
        # Bridge warned about the invalid value.
        assert any("invalid" in w and "AGENT_SESSION" in w for w in warnings)

    def test_invalid_session_id_when_no_previous(self, agent_script):
        """If the first session line is invalid, session stays empty."""
        agent = agent_script(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "AGENT_SESSION:has space"
            echo "done"
        """))
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi"),
        )
        assert r.session_id == ""
        assert r.reply.strip() == "done"

    def test_stderr_diagnosis_survives_noisy_stderr(self, agent_script):
        """The friendly hint pattern lands at the START of stderr; the rest is
        >2 MB of noise. The head-capped ``stderr_full`` (1 MB) must still
        contain the startup error so diagnosis fires — and the buffer must not
        grow unbounded with the noise."""
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            "echo \"python3: can't open file '/tmp/missing.py': [Errno 2] No such file or directory\" >&2\n"
            'head -c 2097152 /dev/zero | tr "\\0" "x" >&2\n'
            "exit 1\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.exit_code == 1
        assert r.error == (
            "agent script not found: /tmp/missing.py. Check the profile's "
            "command path (likely a {{PROFILE_DIR}} issue or a typo)."
        )

    def test_permission_allow_via_on_permission(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo "perm=$AGENT_PERMISSION"\n'
            "echo 'AGENT_PERMISSION_REQUEST:"
            '{"request_id":"r1","tool_name":"Bash","input":{"command":"true"}}\'\n'
            "IFS= read -r resp\n"
            'echo "got:$resp"\n'
            'echo "AGENT_SESSION:sess-perm-1"\n'
        )
        seen: List[dict] = []

        def on_permission(req):
            seen.append(req)
            return {"behavior": "allow", "updated_input": req["input"]}

        r = run(
            {"command": str(agent), "permission": True},
            RunOptions(message="hi", on_permission=on_permission),
        )
        assert len(seen) == 1
        assert seen[0]["request_id"] == "r1"
        assert "perm=1" in r.reply
        assert "got:AGENT_PERMISSION_RESPONSE:" in r.reply
        assert '"behavior": "allow"' in r.reply or '"behavior":"allow"' in r.reply
        assert r.session_id == "sess-perm-1"
        assert r.exit_code == 0

    def test_permission_false_ignores_request(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo "perm=<${AGENT_PERMISSION-unset}>"\n'
            "echo 'AGENT_PERMISSION_REQUEST:"
            '{"request_id":"r1","tool_name":"Bash","input":{}}\'\n'
            'echo "done"\n'
        )
        stderr: List[str] = []
        called = False

        def on_permission(_req):
            nonlocal called
            called = True
            return {"behavior": "allow"}

        r = run(
            {"command": str(agent)},
            RunOptions(
                message="hi",
                on_permission=on_permission,
                on_stderr=stderr.append,
            ),
        )
        assert called is False
        assert "perm=<unset>" in r.reply
        assert "done" in r.reply
        assert any("ignoring AGENT_PERMISSION_REQUEST" in s for s in stderr)

    def test_permission_deny(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            "echo 'AGENT_PERMISSION_REQUEST:"
            '{"request_id":"r2","tool_name":"Bash","input":{}}\'\n'
            "IFS= read -r resp\n"
            'echo "got:$resp"\n'
        )
        r = run(
            {"command": str(agent), "permission": True},
            RunOptions(
                message="hi",
                on_permission=lambda _r: {
                    "behavior": "deny",
                    "message": "not allowed",
                },
            ),
        )
        assert '"behavior": "deny"' in r.reply or '"behavior":"deny"' in r.reply
        assert "not allowed" in r.reply
