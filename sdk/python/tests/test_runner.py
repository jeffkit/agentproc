"""Tests for agentproc.runner — the canonical bridge-side implementation (wire 0.4)."""

from __future__ import annotations

import json
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
    def test_legacy_session_is_malformed(self):
        assert classify_line('{"type":"session","id":"abc-123"}') == {
            "kind": "malformed", "value": '{"type":"session","id":"abc-123"}'
        }

    def test_legacy_text_is_malformed(self):
        assert classify_line('{"type":"text","text":"hello world"}') == {
            "kind": "malformed", "value": '{"type":"text","text":"hello world"}'
        }

    def test_partial(self):
        assert classify_line('{"type":"partial","text":"hello"}') == {"kind": "partial", "value": "hello"}

    def test_partial_with_newline(self):
        assert classify_line('{"type":"partial","text":"line1\\nline2"}') == {"kind": "partial", "value": "line1\nline2"}

    def test_partial_empty(self):
        assert classify_line('{"type":"partial","text":""}') == {"kind": "partial", "value": ""}

    def test_partial_with_role(self):
        assert classify_line('{"type":"partial","text":"x","role":"thinking"}') == {
            "kind": "partial", "value": "x", "role": "thinking"
        }

    def test_partial_non_string_role_dropped(self):
        assert classify_line('{"type":"partial","text":"y","role":42}') == {"kind": "partial", "value": "y"}

    def test_partial_with_session_id(self):
        assert classify_line('{"type":"partial","text":"x","session_id":"s1"}') == {
            "kind": "partial", "value": "x", "session_id": "s1"
        }

    def test_result(self):
        assert classify_line('{"type":"result","text":"hello world"}') == {
            "kind": "result", "value": "hello world"
        }

    def test_result_with_session_id(self):
        assert classify_line('{"type":"result","text":"ok","session_id":"abc"}') == {
            "kind": "result", "value": "ok", "session_id": "abc"
        }

    def test_result_missing_text(self):
        assert classify_line('{"type":"result"}') == {"kind": "result", "value": ""}

    def test_error(self):
        assert classify_line('{"type":"error","message":"rate limited"}') == {"kind": "error", "value": "rate limited"}

    def test_error_with_session_id(self):
        assert classify_line('{"type":"error","message":"boom","session_id":"s1"}') == {
            "kind": "error", "value": "boom", "session_id": "s1"
        }

    def test_permission_request(self):
        c = classify_line('{"type":"permission_request","request_id":"1","tool_name":"Bash","input":{}}')
        assert c["kind"] == "permission_request"
        assert c["value"] == {"type": "permission_request", "request_id": "1", "tool_name": "Bash", "input": {}}

    def test_plain_text_is_malformed(self):
        assert classify_line("hello world") == {"kind": "malformed", "value": "hello world"}

    def test_empty_line_is_malformed(self):
        assert classify_line("") == {"kind": "malformed", "value": ""}

    def test_non_object_json_is_malformed(self):
        assert classify_line("42") == {"kind": "malformed", "value": "42"}
        assert classify_line("[1,2,3]") == {"kind": "malformed", "value": "[1,2,3]"}

    def test_object_without_type_is_malformed(self):
        assert classify_line('{"foo":"bar"}') == {"kind": "malformed", "value": '{"foo":"bar"}'}

    def test_unknown_type_is_malformed(self):
        assert classify_line('{"type":"unknown"}') == {"kind": "malformed", "value": '{"type":"unknown"}'}


class TestPermissionHelpers:
    def test_format_allow(self):
        assert format_permission_response(
            {"request_id": "1", "behavior": "allow", "updated_input": {"c": "x"}}
        ) == '{"type":"permission_response","request_id":"1","behavior":"allow","updated_input":{"c":"x"}}'

    def test_format_deny(self):
        assert format_permission_response(
            {"request_id": "2", "behavior": "deny", "message": "nope"}
        ) == '{"type":"permission_response","request_id":"2","behavior":"deny","message":"nope"}'

    def test_format_allow_without_updated_input_omits_field(self):
        # allow without updated_input MUST omit the field — the agent/CLI is
        # responsible for falling back to the request's original input. The
        # runner must not pre-fill it (would erase the "user accepted
        # unchanged" vs "user never touched it" distinction downstream).
        assert format_permission_response(
            {"request_id": "3", "behavior": "allow"}
        ) == '{"type":"permission_response","request_id":"3","behavior":"allow"}'

    def test_is_valid(self):
        assert is_valid_permission_request({"request_id": "1", "tool_name": "Bash", "input": {}})
        assert not is_valid_permission_request({"request_id": "1", "tool_name": "Bash"})
        assert not is_valid_permission_request({"request_id": "a b", "tool_name": "Bash", "input": {}})
        assert not is_valid_permission_request(None)


class TestIsValidSessionId:
    def test_valid_uuid(self):
        assert is_valid_session_id("f47ac10b-58cc-4372-a567-0e02b2c3d479")

    def test_valid_cli_handle(self):
        assert is_valid_session_id("cli-sess-9f3a2c1e")

    def test_valid_short_token(self):
        assert is_valid_session_id("abc123")

    def test_empty_rejected(self):
        assert not is_valid_session_id("")

    def test_wire_spaces_allowed(self):
        assert is_valid_session_id("has space")

    def test_wire_colons_allowed(self):
        assert is_valid_session_id("thread:abc")

    def test_wire_plus_allowed(self):
        assert is_valid_session_id("a+b")

    def test_control_chars_rejected(self):
        assert not is_valid_session_id("ctrl\x07char")
        assert not is_valid_session_id("tab\there")

    def test_slash_rejected(self):
        assert not is_valid_session_id("a/b")
        assert not is_valid_session_id("..\\..\\tmp")

    def test_dot_dotdot_rejected(self):
        assert not is_valid_session_id(".")
        assert not is_valid_session_id("..")

    def test_a_dot_dot_b_allowed(self):
        assert is_valid_session_id("a..b")


class TestSubstitute:
    def test_message(self):
        assert substitute("You said: {{MESSAGE}}", {"message": "hi"}) == "You said: hi"

    def test_session_id(self):
        assert substitute("s={{SESSION_ID}}", {"session_id": "abc"}) == "s=abc"

    def test_session_name(self):
        assert substitute("n={{SESSION_NAME}}", {"session_name": "work"}) == "n=work"

    def test_profile_dir(self):
        assert substitute("d={{PROFILE_DIR}}", {"profile_dir": "/p"}) == "d=/p"

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
        assert expand_env_ref(
            "${AWS_SECRET_ACCESS_KEY}", {"AWS_SECRET_ACCESS_KEY": "s3cr3t"}, allowlist={"HOME"}
        ) == ""

    def test_allowlist_blocked_callback(self):
        blocked: List[str] = []
        out = expand_env_ref("${A} ${B}", {"A": "1", "B": "2"}, allowlist={"A"}, on_blocked=blocked.append)
        assert out == "1 "
        assert blocked == ["B"]

    def test_allowlist_none_means_all_permitted(self):
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
    def test_minimal_valid_command_is_argv0_never_split(self):
        p = normalize_profile({"command": "bash"})
        assert p["argv"] == ["bash"]
        assert p["args"] == []
        assert p["streaming"] is True

    def test_hub_form(self):
        p = normalize_profile({"agentproc": {"command": "node"}})
        assert p["argv"] == ["node"]

    def test_rejects_missing_command(self):
        with pytest.raises(ValueError, match="command must be a non-empty string"):
            normalize_profile({})

    def test_rejects_empty_command(self):
        with pytest.raises(ValueError, match="command must be a non-empty string"):
            normalize_profile({"command": "   "})

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            normalize_profile(None)  # type: ignore

    def test_command_with_whitespace_kept_whole(self):
        p = normalize_profile({"command": "python3 ./bridge.py"})
        assert p["argv"] == ["python3 ./bridge.py"]
        assert p["args"] == []

    def test_args_cast_to_str(self):
        p = normalize_profile({"command": "x", "args": ["--foo", 42]})
        assert p["args"] == ["--foo", "42"]

    def test_args_none_treated_as_empty_not_split(self):
        p = normalize_profile({"command": "python3 ./bridge.py", "args": None})
        assert p["argv"] == ["python3 ./bridge.py"]
        assert p["args"] == []

    def test_cwd_tilde_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        p = normalize_profile({"command": "x", "cwd": "~/proj"})
        assert p["cwd"] == str(tmp_path / "proj")

    def test_no_stdin_field(self):
        p = normalize_profile({"command": "x"})
        assert "stdin" not in p

    def test_no_env_inherit_field_and_legacy_ignored(self):
        p = normalize_profile({"command": "x"})
        assert "env_inherit" not in p
        p2 = normalize_profile({"command": "x", "env_inherit": "all"})
        assert "env_inherit" not in p2

    def test_truncation_suffix_defaults_to_ellipsis(self):
        p = normalize_profile({"command": "x"})
        assert p["truncation_suffix"] == "\n\n…(truncated)"

    def test_truncation_suffix_custom_value_is_honoured(self):
        # A custom cap no longer silently strips the truncation notice —
        # users get the default suffix unless they explicitly override.
        p = normalize_profile({"command": "x", "max_reply_chars": 100})
        assert p["truncation_suffix"] == "\n\n…(truncated)"
        p2 = normalize_profile({"command": "x", "truncation_suffix": " [more]"})
        assert p2["truncation_suffix"] == " [more]"

    def test_truncation_suffix_empty_string_disables_notice(self):
        p = normalize_profile({"command": "x", "truncation_suffix": ""})
        assert p["truncation_suffix"] == ""

    def test_permission_defaults_false(self):
        assert normalize_profile({"command": "x"})["permission"] is False
        assert normalize_profile({"command": "x", "permission": True})["permission"] is True
        assert normalize_profile({"command": "x", "permission": False})["permission"] is False
        assert normalize_profile({"command": "x", "permission": "true"})["permission"] is False

    def test_streaming_false_honored(self):
        assert normalize_profile({"command": "x", "streaming": False})["streaming"] is False
        assert normalize_profile({"command": "x", "streaming": True})["streaming"] is True
        assert normalize_profile({"command": "x"})["streaming"] is True

    def test_env_allowlist_absent_is_none(self):
        p = normalize_profile({"command": "x", "env": {"A": "1"}})
        assert p["env_allowlist"] is None

    def test_env_allowlist_parsed_to_set(self):
        p = normalize_profile({"command": "x", "env_allowlist": ["A", "B"]})
        assert p["env_allowlist"] == {"A", "B"}

    def test_env_allowlist_non_list_raises(self):
        with pytest.raises(ValueError, match="env_allowlist"):
            normalize_profile({"command": "x", "env_allowlist": "A"})

    def test_command_with_spaces_runs_end_to_end(self, tmp_path):
        """A profile whose executable path contains spaces must actually spawn,
        not be split into bogus argv. `command` is a single argv token; the
        message is passed via `args` placeholder and read by the agent."""
        nested = tmp_path / "has space"
        nested.mkdir()
        script = nested / "agent.sh"
        script.write_text(
            '#!/usr/bin/env bash\n'
            'echo "{\\"type\\":\\"result\\",\\"text\\":\\"ok: $1\\"}"\n'
        )
        script.chmod(script.stat().st_mode | 0o111)
        r = run(
            {"command": str(script), "args": ["{{MESSAGE}}"]},
            RunOptions(message="payload"),
        )
        assert r.reply == "ok: payload"
        assert r.exit_code == 0


def test_protocol_version_is_0_4():
    assert PROTOCOL_VERSION == "0.4"


# ---------------------------------------------------------------------------
# 2. run() end-to-end tests with tiny agent scripts
# ---------------------------------------------------------------------------

def _evt(obj: dict) -> str:
    """A bash `echo` of a single-quoted NDJSON event line (no shell expansion)."""
    return "echo '" + json.dumps(obj, ensure_ascii=False) + "'"


def write_script(content: str, tmp_path: Path) -> Path:
    f = tmp_path / "agent.sh"
    f.write_text(content)
    f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return f


def write_py_agent(src: str, tmp_path: Path) -> Path:
    f = tmp_path / "agent.py"
    f.write_text(src)
    return f


@pytest.fixture
def agent_script(tmp_path):
    def _make(content: str) -> Path:
        return write_script(content, tmp_path)
    return _make


@pytest.fixture
def py_agent(tmp_path):
    def _make(src: str) -> Path:
        return write_py_agent(src, tmp_path)
    return _make


class TestRunEndToEnd:
    def test_simple_result_event(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\n" + _evt({"type": "result", "text": "hello"}) + "\n")
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.reply == "hello"
        assert r.session_id == ""
        assert r.error == ""
        assert r.exit_code == 0

    def test_session_id_first_non_empty_wins(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "result", "text": "done", "session_id": "first"}) + "\n"
            + _evt({"type": "result", "text": "ignored", "session_id": "second"}) + "\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.session_id == "first"
        assert r.reply == "done"

    def test_partial_triggers_callback(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "partial", "text": "chunk1"}) + "\n"
            + _evt({"type": "partial", "text": "chunk2"}) + "\n"
            + _evt({"type": "result", "text": "final"}) + "\n"
        )
        partials: List[str] = []
        r = run({"command": str(agent)}, RunOptions(message="hi", on_partial=partials.append))
        assert partials == ["chunk1", "chunk2"]
        # Partials already forwarded → result.text must not become reply.
        assert r.reply == ""

    def test_partial_with_role_streams_text(self, agent_script):
        # Python's on_partial receives only the text (role is not forwarded);
        # this pins that a role-bearing partial still streams its text.
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "partial", "text": "thinking...", "role": "thinking"}) + "\n"
            + _evt({"type": "result", "text": ""}) + "\n"
        )
        partials: List[str] = []
        r = run({"command": str(agent)}, RunOptions(message="hi", on_partial=partials.append))
        assert partials == ["thinking..."]
        assert r.reply == ""

    def test_partial_skipped_when_streaming_false(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "partial", "text": "chunk1"}) + "\n"
            + _evt({"type": "result", "text": "final"}) + "\n"
        )
        partials: List[str] = []
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", streaming=False, on_partial=partials.append),
        )
        assert partials == []
        assert r.reply == "final"

    def test_error_surfaces(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "partial", "text": "thinking..."}) + "\n"
            + _evt({"type": "error", "message": "rate limited"}) + "\n"
            "exit 1\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.error == "rate limited"
        assert r.exit_code == 1

    def test_error_marks_exit_1_even_if_process_exits_0(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "error", "message": "soft fail"}) + "\n"
            "exit 0\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.error == "soft fail"
        assert r.exit_code == 1

    def test_second_result_ignored(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "result", "text": "a"}) + "\n"
            + _evt({"type": "result", "text": "b"}) + "\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.reply == "a"

    def test_result_preserves_newlines(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "result", "text": "line 1\nline 2\nline 3"}) + "\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.reply == "line 1\nline 2\nline 3"

    def test_malformed_lines_ignored(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            'echo " AGENT_SESSION:foo"\n'   # plain text → malformed
            'echo "not json"\n'             # malformed
            + _evt({"type": "result", "text": "real reply"}) + "\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.session_id == ""
        assert r.reply == "real reply"

    def test_exit_code_propagates(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\nexit 3\n")
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.exit_code == 3

    def test_agent_env_vars_not_injected(self, agent_script):
        # Wire 0.4: the per-turn request travels on stdin, not env.
        agent = agent_script(
            '#!/usr/bin/env bash\n'
            'echo "{\\"type\\":\\"result\\",\\"text\\":\\"m=<${AGENT_MESSAGE:-unset}>\\"}"\n'
        )
        r = run({"command": str(agent)}, RunOptions(message="payload"))
        assert r.reply == "m=<unset>"

    def test_turn_written_to_stdin(self, py_agent):
        agent = py_agent(
            "import json, sys\n"
            "line = sys.stdin.readline()\n"
            "t = json.loads(line)\n"
            "out = {'type':'result','text':'|'.join([\n"
            "  'msg='+t.get('message',''),\n"
            "  'sid='+t.get('session_id',''),\n"
            "  'sname='+t.get('session_name',''),\n"
            "  'from='+t.get('from_user',''),\n"
            "  'pv='+t.get('protocol_version',''),\n"
            "])}\n"
            "sys.stdout.write(json.dumps(out)+'\\n')\n"
        )
        r = run(
            {"command": sys.executable, "args": [str(agent)]},
            RunOptions(message="hello", session_id="prev-123", session_name="work", from_user="u123"),
        )
        assert r.reply == f"msg=hello|sid=prev-123|sname=work|from=u123|pv={PROTOCOL_VERSION}"

    def test_attachments_travel_on_stdin(self, py_agent):
        agent = py_agent(
            "import json, sys\n"
            "line = sys.stdin.readline()\n"
            "t = json.loads(line)\n"
            "atts = ','.join(a['kind']+':'+a['url'] for a in (t.get('attachments') or []))\n"
            "sys.stdout.write(json.dumps({'type':'result','text':'atts='+atts})+'\\n')\n"
        )
        r = run(
            {"command": sys.executable, "args": [str(agent)]},
            RunOptions(
                message="hi",
                attachments=[
                    {"kind": "image", "url": "https://example.com/a.png"},
                    {"kind": "file", "url": "https://example.com/b.pdf"},
                ],
            ),
        )
        assert r.reply == "atts=image:https://example.com/a.png,file:https://example.com/b.pdf"

    def test_message_placeholder_in_args(self, agent_script):
        agent = agent_script(
            '#!/usr/bin/env bash\n'
            'echo "{\\"type\\":\\"result\\",\\"text\\":\\"args: $1\\"}"\n'
        )
        r = run(
            {"command": str(agent), "args": ["{{MESSAGE}}"]},
            RunOptions(message="hello"),
        )
        assert r.reply == "args: hello"

    def test_profile_env_with_var_ref(self, agent_script, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "/some/path")
        agent = agent_script(
            '#!/usr/bin/env bash\n'
            'echo "{\\"type\\":\\"result\\",\\"text\\":\\"v=$MY_KEY\\"}"\n'
        )
        r = run(
            {"command": str(agent), "env": {"MY_KEY": "${MY_TEST_VAR}"}},
            RunOptions(message="hi"),
        )
        assert r.reply == "v=/some/path"

    def test_extra_env_applied(self, agent_script):
        agent = agent_script(
            '#!/usr/bin/env bash\n'
            'echo "{\\"type\\":\\"result\\",\\"text\\":\\"x=$X\\"}"\n'
        )
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", extra_env={"X": "extra"}),
        )
        assert r.reply == "x=extra"

    def test_timeout_kills_long_agent(self, agent_script):
        agent = agent_script("#!/usr/bin/env bash\nsleep 30\necho 'should not reach'\n")
        r = run(
            {"command": str(agent), "kill_grace_secs": 1},
            RunOptions(message="hi", timeout_secs=1),
        )
        assert r.timed_out is True
        assert r.exit_code == 124

    def test_spawn_error_command_not_found(self):
        r = run({"command": "/nonexistent/command/xyz"}, RunOptions(message="hi"))
        assert r.exit_code == 1

    def test_env_allowlist_end_to_end(self, agent_script, monkeypatch):
        monkeypatch.setenv("ALLOWED_KEY", "ok-val")
        monkeypatch.setenv("SECRET_KEY", "top-secret")
        agent = agent_script(
            '#!/usr/bin/env bash\n'
            'echo "{\\"type\\":\\"result\\",\\"text\\":\\"ALLOWED=$ALLOWED_KEY SECRET=$SECRET_KEY\\"}"\n'
        )
        warnings: List[str] = []
        r = run(
            {
                "command": str(agent),
                "env": {"ALLOWED_KEY": "${ALLOWED_KEY}", "SECRET_KEY": "${SECRET_KEY}"},
                "env_allowlist": ["ALLOWED_KEY"],
            },
            RunOptions(message="hi", on_stderr=warnings.append),
        )
        assert "ALLOWED=ok-val" in r.reply
        assert "SECRET=" in r.reply
        assert "top-secret" not in r.reply
        assert any("SECRET_KEY" in w and "allowlist" in w for w in warnings)

    def test_undeclared_secrets_do_not_leak(self, agent_script, monkeypatch):
        monkeypatch.setenv("BRIDGE_DB_PASSWORD", "db-top-secret")
        monkeypatch.setenv("AGENTPROC_SECURE_DEFAULT_LEAK", "should-not-leak")
        agent = agent_script(
            '#!/usr/bin/env bash\n'
            'echo "{\\"type\\":\\"result\\",\\"text\\":\\"'
            'DB=${BRIDGE_DB_PASSWORD:-unset} '
            'LEAK=${AGENTPROC_SECURE_DEFAULT_LEAK:-unset} '
            'PATH_SET=${PATH:+yes}\\"}"\n'
        )
        r = run(
            {"command": str(agent), "env": {"BRIDGE_DB_PASSWORD": "${BRIDGE_DB_PASSWORD}"}},
            RunOptions(message="hi"),
        )
        assert "DB=db-top-secret" in r.reply
        assert "LEAK=unset" in r.reply
        assert "should-not-leak" not in r.reply
        assert "PATH_SET=yes" in r.reply

    def test_invalid_session_id_ignored_preserves_previous(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "partial", "text": "x", "session_id": "valid-id-1"}) + "\n"
            + _evt({"type": "result", "text": "done", "session_id": "bad/path"}) + "\n"
        )
        warnings: List[str] = []
        partials: List[str] = []
        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", on_stderr=warnings.append, on_partial=partials.append),
        )
        assert r.session_id == "valid-id-1"
        assert partials == ["x"]
        assert r.reply == ""  # partials forwarded → empty reply
        assert any("invalid" in w and "session id" in w for w in warnings)

    def test_invalid_session_id_when_no_previous(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            + _evt({"type": "result", "text": "done", "session_id": "bad/path"}) + "\n"
        )
        r = run({"command": str(agent)}, RunOptions(message="hi"))
        assert r.session_id == ""
        assert r.reply == "done"

    def test_stderr_diagnosis_survives_noisy_stderr(self, agent_script):
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
        # The agent consumes the turn line, emits a permission_request, reads
        # the response line, then reports what it saw (grep — the response
        # contains quotes, so we must not embed it raw into JSON).
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            "read -r turn\n"
            + _evt({"type": "permission_request", "request_id": "r1", "tool_name": "Bash", "input": {"command": "true"}}) + "\n"
            'IFS= read -r resp\n'
            'if echo "$resp" | grep -q \'"behavior":"allow"\'; then '
            + _evt({"type": "result", "text": "ALLOWED", "session_id": "sess-perm-1"}) + '; fi\n'
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
        assert r.reply == "ALLOWED"
        assert r.session_id == "sess-perm-1"
        assert r.exit_code == 0

    def test_permission_false_ignores_request(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            "read -r turn\n"
            + _evt({"type": "permission_request", "request_id": "r1", "tool_name": "Bash", "input": {}}) + "\n"
            + _evt({"type": "result", "text": "done"}) + "\n"
        )
        stderr: List[str] = []
        called = False

        def on_permission(_req):
            nonlocal called
            called = True
            return {"behavior": "allow"}

        r = run(
            {"command": str(agent)},
            RunOptions(message="hi", on_permission=on_permission, on_stderr=stderr.append),
        )
        assert called is False
        assert r.reply == "done"
        assert any('ignoring {"type":"permission_request"}' in s or "permission_request" in s for s in stderr)

    def test_permission_deny(self, agent_script):
        agent = agent_script(
            "#!/usr/bin/env bash\n"
            "read -r turn\n"
            + _evt({"type": "permission_request", "request_id": "r2", "tool_name": "Bash", "input": {}}) + "\n"
            'IFS= read -r resp\n'
            'if echo "$resp" | grep -q \'"behavior":"deny"\'; then '
            + _evt({"type": "result", "text": "DENIED"}) + '; fi\n'
            'if echo "$resp" | grep -q \'not allowed\'; then '
            + _evt({"type": "result", "text": "HASMSG"}) + '; fi\n'
        )
        r = run(
            {"command": str(agent), "permission": True},
            RunOptions(
                message="hi",
                on_permission=lambda _r: {"behavior": "deny", "message": "not allowed"},
            ),
        )
        # First result wins; second is ignored — DENIED is emitted first.
        assert "DENIED" in r.reply
