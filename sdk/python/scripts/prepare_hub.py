#!/usr/bin/env python3
"""Copy the canonical hub/ directory (repo root) into this package as
``agentproc/hub_data/``, so ``agentproc hub run`` / ``hub list`` can read
profiles with zero network. Run before ``python -m build`` / ``twine upload``.

Excludes Python bytecode (__pycache__, *.pyc). Zero dependencies.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent.parent / "hub"          # repo root / hub
DEST = HERE.parent / "src" / "agentproc" / "hub_data"   # sdk/python/src/agentproc/hub_data


def _ignore(_, names):
    return [n for n in names if n == "__pycache__" or n.endswith(".pyc")]


def main() -> int:
    if not SRC.exists():
        print(f"prepare-hub: source hub/ not found at {SRC}", file=sys.stderr)
        return 1
    if DEST.exists():
        shutil.rmtree(DEST)
    shutil.copytree(SRC, DEST, ignore=_ignore)
    print(f"prepare-hub: copied {SRC} -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
