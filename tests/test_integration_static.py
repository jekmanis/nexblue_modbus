"""Checks on the integration files that need no Home Assistant install."""

from __future__ import annotations

import ast
import json
import re

import pytest

import nexblue_client as nb
from conftest import COMPONENT_DIR, REPO_ROOT

SOURCES = sorted(COMPONENT_DIR.glob("*.py"))
JSON_FILES = [COMPONENT_DIR / "manifest.json", COMPONENT_DIR / "strings.json",
              COMPONENT_DIR / "translations" / "en.json"]


@pytest.mark.parametrize("path", JSON_FILES + SOURCES, ids=lambda p: p.name)
def test_files_are_utf8_without_bom(path):
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM breaks manifest/strings parsing"
    text = raw.decode("utf-8")
    assert "â€" not in text and "Ã¢" not in text, "mojibake"


def test_manifest():
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "nexblue_modbus" == COMPONENT_DIR.name
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    # pymodbus is the only runtime dependency, and HA 2026.10.2 pins pymodbus==3.13.1.
    (requirement,) = manifest["requirements"]
    name, minimum = requirement.split(">=")
    assert name == "pymodbus"
    assert tuple(map(int, minimum.split("."))) <= (3, 13, 1)


def test_manifest_is_marketplace_ready():
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:]), "hassfest wants domain, name, then alphabetical"
    for key in ("codeowners", "documentation", "issue_tracker", "version", "integration_type"):
        assert manifest[key], key
    assert manifest["integration_type"] == "device"
    assert 1 <= len(manifest["categories"]) <= 3
    assert len(set(manifest["categories"])) == len(manifest["categories"])


def test_hacs_json():
    """Only keys from the Marketplace hacs.json reference, with the documented types."""
    hacs = json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
    types = {"name": str, "content_in_root": bool, "filename": str, "zip_release": bool,
             "persistent_directory": str, "homeassistant": str, "hide_default_branch": bool,
             "hacs": str, "country": (str, list)}
    assert hacs["name"]
    for key, value in hacs.items():
        assert key in types, f"undocumented hacs.json key {key}"
        assert isinstance(value, types[key]), key
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", hacs["homeassistant"])


def test_translations_match_strings():
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text(encoding="utf-8"))
    assert strings == english


def _translation_keys(platform: str) -> set[str]:
    """Translation keys used by a platform module, found by parsing its source."""
    tree = ast.parse((COMPONENT_DIR / f"{platform}.py").read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "translation_key":
            if isinstance(node.value, ast.Constant):
                keys.add(node.value.value)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_attr_translation_key" for t in node.targets):
            keys.add(node.value.value)
        # Helper factories (_current, _voltage, ...) take the key as first argument.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id.startswith("_") and node.args
                and isinstance(node.args[0], ast.Constant)):
            keys.add(node.args[0].value)
    return keys


@pytest.mark.parametrize("platform", ["sensor", "binary_sensor", "number", "button"])
def test_every_entity_has_a_name(platform):
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
    used = _translation_keys(platform)
    assert used, f"no translation keys found in {platform}.py"
    defined = set(strings["entity"][platform])
    assert used == defined
    for key in used:
        assert strings["entity"][platform][key]["name"]


def test_enum_state_translations_cover_options():
    sensor = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))["entity"]["sensor"]
    assert set(sensor["state"]["state"]) == set(nb.STATE_NAMES.values())
    assert set(sensor["phase_mode"]["state"]) == set(nb.PHASE_MODE_NAMES.values())


def test_config_flow_fields_have_labels():
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
    assert set(strings["config"]["step"]["user"]["data"]) == {"host", "port", "unit_id",
                                                              "scan_interval"}
    source = (COMPONENT_DIR / "config_flow.py").read_text(encoding="utf-8")
    for error in re.findall(r'errors\["base"\] = "(\w+)"', source):
        assert error in strings["config"]["error"]
    aborts = re.findall(r'async_abort\(reason="(\w+)"', source)
    assert "no_serial" in aborts
    for reason in aborts:
        assert reason in strings["config"]["abort"]


def test_only_the_client_talks_modbus_and_only_writes_1000_and_1006():
    """Current Limit (1000-1001) and IF_START (1006, START only) are the whole write scope.

    Fallback limit/timeout (1002-1005) and phase mode (1007) change charging behaviour
    and are out of scope.
    """
    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        if path.name == "nexblue_client.py":
            calls = re.findall(r'_call\("write_\w+", (\w+)', source)
            assert calls == ["REG_CURRENT_LIMIT", "REG_IF_START"]
            assert re.findall(r"values=\[(\w+)\]", source) == ["IF_START_START"]
            assert (nb.REG_CURRENT_LIMIT, nb.REG_IF_START, nb.IF_START_START) == (1000, 1006, 1)
            assert not {1002, 1003, 1004, 1005, 1007} & {nb.REG_CURRENT_LIMIT,
                                                         nb.REG_CURRENT_LIMIT + 1,
                                                         nb.REG_IF_START}
        else:
            assert "pymodbus" not in source, f"{path.name} must go through nexblue_client"
            assert "write_register" not in source, path.name


def test_probe_tool_only_writes_current_limit():
    source = (REPO_ROOT / "tools" / "nexblue_modbus.py").read_text(encoding="utf-8")
    assert source.count("client.write_registers(") == 1  # inside do_write()
    assert "write_register(" not in source
    assert re.findall(r"do_write\(client, args, (\d+)", source) == ["1000"]

    import argparse
    import importlib.util

    spec = importlib.util.spec_from_file_location("probe_tool", REPO_ROOT / "tools" / "nexblue_modbus.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    subparsers = next(a for a in tool.build_parser()._actions
                      if isinstance(a, argparse._SubParsersAction))
    assert set(subparsers.choices) == {"read", "raw", "write-current", "selftest"}


@pytest.mark.parametrize("name, size", [("icon.png", 256), ("icon@2x.png", 512),
                                        ("dark_icon.png", 256), ("dark_icon@2x.png", 512)])
def test_brand_icons(name, size):
    """brand/ icons (HA 2026.3+): square PNG, 256 px and 512 px for @2x, with alpha."""
    import struct

    raw = (COMPONENT_DIR / "brand" / name).read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR"
    width, height, _depth, color_type = struct.unpack(">IIBB", raw[16:26])
    assert (width, height) == (size, size)
    assert color_type == 6  # RGBA: transparent background
