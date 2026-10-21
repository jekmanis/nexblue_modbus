"""Test setup.

``nexblue_client.py`` has no Home Assistant imports and no relative imports, so it is
imported straight from the component directory. Importing it through the package would
run ``__init__.py``, which needs Home Assistant.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "nexblue_modbus"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(COMPONENT_DIR))


@pytest.fixture
def idle_dump() -> dict[str, list[int]]:
    """Raw blocks read from an Edge 2 on 2026-10-21, no car connected; serial number replaced."""
    return json.loads((FIXTURES / "edge2_idle.json").read_text(encoding="utf-8"))
