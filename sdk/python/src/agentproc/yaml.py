"""YAML parsing for profile.yaml files.

Thin wrapper around PyYAML (a runtime dependency). The previous hand-rolled
subset parser lived in ``cli.py`` and silently mis-parsed several constructs
that real profiles use — most notably inline ``#`` comments, so
``streaming: false # one-shot mode`` became the string ``"false # one-shot mode"``
and the runner's ``is not False`` check left streaming **on**. That is the same
class of bug that retired the Node SDK's hand-rolled parser in favour of
``js-yaml``; the Python SDK now matches.

``parse_yaml_simple`` is kept as an alias for any caller that imported it by
that name.
"""

from __future__ import annotations

from typing import Any, Dict

import yaml


def parse_yaml(text: str) -> Dict[str, Any]:
    """Parse a YAML document into a Python object.

    Uses ``yaml.safe_load``. JSON input is a YAML subset, so the explicit
    JSON fast-path the hand-rolled parser had is unnecessary.
    """
    return yaml.safe_load(text)


# Backwards-compat alias.
parse_yaml_simple = parse_yaml


__all__ = ["parse_yaml", "parse_yaml_simple"]
