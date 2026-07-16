"""Tests for the YAML parser wrapper.

Pins the behaviours that the retired hand-rolled parser got wrong — most
notably inline ``#`` comments, which previously turned
``streaming: false # one-shot`` into the string ``"false # one-shot"`` and
left streaming on.
"""

from __future__ import annotations

from agentproc.yaml import parse_yaml


def test_inline_comment_is_stripped():
    p = parse_yaml("streaming: false  # one-shot mode\n")
    assert p["streaming"] is False


def test_inline_comment_on_string_value():
    p = parse_yaml('command: python3 bridge.py  # the bridge\n')
    assert p["command"] == "python3 bridge.py"


def test_empty_env_value_is_null_not_empty_string():
    # The hand-rolled parser rendered an empty `env:` value as "" instead of
    # None. js-yaml / safe_load give None.
    p = parse_yaml("env:\n  MY_KEY:\n")
    assert p["env"]["MY_KEY"] is None


def test_flow_sequence():
    p = parse_yaml("env_allowlist: [A, B, C]\n")
    assert p["env_allowlist"] == ["A", "B", "C"]


def test_nested_map_and_int():
    p = parse_yaml(
        "agentproc:\n"
        "  command: x\n"
        "  timeout_secs: 600\n"
        "  streaming: true\n"
    )
    block = p["agentproc"]
    assert block["command"] == "x"
    assert block["timeout_secs"] == 600
    assert block["streaming"] is True


def test_json_input_is_yaml_subset():
    p = parse_yaml('{"command": "x", "streaming": false}')
    assert p["command"] == "x"
    assert p["streaming"] is False
