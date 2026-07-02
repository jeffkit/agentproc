"""Cross-language parity test for hub/recursive/bridge.py.

Drives the shared ``hub/recursive/tests/parity.json`` fixture through the
Python bridge's pure helpers (``provider_args``, ``_global_args``,
``extract_session_dir``, ``_last_assistant_text``) and asserts they match the
fixture's expectations. The Node bridge runs the same fixture through its
``providerArgs`` / ``globalArgs`` / ``extractSessionDir`` / ``lastAssistantText``
in ``hub/recursive/tests/test_bridge_parity.js`` — together they guard the
hub/README.md claim that both bridges produce identical observable behaviour.

The full NDJSON event classification (``handleLine`` inside ``main``) is not
covered — it is nested in ``main()`` and not refactored to an importable
helper; that remains future parity work.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BRIDGE_PY = HERE.parent / "bridge.py"
PARITY_JSON = HERE / "parity.json"

# Import hub/recursive/bridge.py as a module (it has an __main__ guard, so
# importing it does not run the bridge).
_spec = importlib.util.spec_from_file_location("recursive_bridge_py", BRIDGE_PY)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

data = json.loads(PARITY_JSON.read_text(encoding="utf-8"))

_RECURSIVE_VARS = [
    "RECURSIVE_API_KEY", "RECURSIVE_PROVIDER", "RECURSIVE_API_BASE",
    "RECURSIVE_MODEL", "RECURSIVE_WORKSPACE", "RECURSIVE_MAX_STEPS",
    "RECURSIVE_PERMISSION_MODE", "RECURSIVE_STATE_DIR", "AGENT_STREAMING",
]


@pytest.fixture
def clean_env(monkeypatch):
    for v in _RECURSIVE_VARS:
        monkeypatch.delenv(v, raising=False)
    yield monkeypatch


@pytest.mark.parametrize("case", data["arg_cases"], ids=[c["name"] for c in data["arg_cases"]])
def test_arg_building(case, clean_env):
    for k, val in case["env"].items():
        clean_env.setenv(k, val)
    assert bridge.provider_args() == case["expect_provider_args"]
    assert bridge._global_args() == case["expect_global_args"]


@pytest.mark.parametrize("case", data["extract_session_dir_cases"],
                         ids=[f"extract-{i}" for i in range(len(data["extract_session_dir_cases"]))])
def test_extract_session_dir(case):
    assert bridge.extract_session_dir(case["stderr"]) == case["expect"]


@pytest.mark.parametrize("case", data["last_assistant_text_cases"],
                         ids=[f"last-{i}" for i in range(len(data["last_assistant_text_cases"]))])
def test_last_assistant_text(case, tmp_path):
    if case["transcript_lines"]:
        (tmp_path / "transcript.jsonl").write_text(
            "\n".join(json.dumps(line) for line in case["transcript_lines"]) + "\n",
            encoding="utf-8",
        )
    assert bridge._last_assistant_text(str(tmp_path)) == case["expect"]


def test_last_assistant_text_none_session_dir():
    assert bridge._last_assistant_text(None) is None
