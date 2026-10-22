"""Test setup.

``nexblue_client.py`` has no Home Assistant imports and no relative imports, so it is
imported straight from the component directory. Importing it through the package would
run ``__init__.py``, which needs Home Assistant.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "nexblue_modbus"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Loaded by file path, not by putting COMPONENT_DIR on sys.path: the component's
# select.py would shadow the standard library's select module.
_spec = importlib.util.spec_from_file_location("nexblue_client", COMPONENT_DIR / "nexblue_client.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules["nexblue_client"] = _module
_spec.loader.exec_module(_module)


@pytest.fixture
def idle_dump() -> dict[str, list[int]]:
    """Raw blocks read from an Edge 2 on 2026-10-21, no car connected; serial number replaced."""
    return json.loads((FIXTURES / "edge2_idle.json").read_text(encoding="utf-8"))
