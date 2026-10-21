"""Entity values against the real Home Assistant API.

Skipped unless Home Assistant is importable (it is not a project dependency). Run with:

    uv run --isolated --no-project --python 3.14 --with homeassistant==2026.10.2 \
        --with pymodbus==3.13.1 --with pytest pytest tests
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

pytest.importorskip("homeassistant")

from conftest import COMPONENT_DIR  # noqa: E402

import nexblue_client as nb  # noqa: E402


@pytest.fixture(scope="module")
def component():
    """Import the integration as a real package (relative imports need one)."""
    parent = types.ModuleType("custom_components")
    parent.__path__ = [str(COMPONENT_DIR.parent)]
    sys.modules.setdefault("custom_components", parent)
    modules = {name: importlib.import_module(f"custom_components.nexblue_modbus.{name}")
               for name in ("config_flow", "coordinator", "sensor", "binary_sensor", "number")}
    importlib.import_module("custom_components.nexblue_modbus")
    return types.SimpleNamespace(**modules)


class _Coordinator:
    """Just enough of NexBlueCoordinator for the entity properties."""

    def __init__(self, data, identity):
        self.data = data
        self.identity = identity
        self.device_info = {"identifiers": {("nexblue_modbus", identity.serial)}}
        self.last_update_success = True
        self.written = []

    def async_add_listener(self, *_args, **_kwargs):
        return lambda: None

    async def async_set_current_limit(self, amps):
        self.written.append(amps)


@pytest.fixture
def coordinator(idle_dump):
    data = nb.parse_data(idle_dump["status"], idle_dump["alarms"], idle_dump["holding"])
    return _Coordinator(data, nb.parse_identity(idle_dump["identity"]))


def test_sensor_values(component, coordinator):
    values = {}
    for description in component.sensor.SENSORS:
        entity = component.sensor.NexBlueSensor(coordinator, description)
        values[description.key] = entity.native_value
        assert entity.unique_id == f"E2TEST0001_{description.key}"
        if description.options is not None and entity.native_value is not None:
            assert entity.native_value in description.options
    assert values["state"] == "available"
    assert values["voltage_l1"] == 232.67  # rounded to 2 decimals
    assert values["power"] == 0.0
    assert values["lifetime_energy"] == 1_040_975
    assert values["session_energy"] == 18.034
    assert values["max_current"] == 20.0
    assert values["fallback_current"] == 6.0
    assert values["phase_mode"] == "adaptive"
    assert values["waiting_code"] == 0x40
    assert values["modbus_fallback_limit"] is None  # -1 = not set
    assert values["modbus_fallback_timeout"] == 300


def test_energy_sensors_are_statistics_ready(component):
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    by_key = {d.key: d for d in component.sensor.SENSORS}
    lifetime = by_key["lifetime_energy"]
    assert lifetime.device_class is SensorDeviceClass.ENERGY
    assert lifetime.state_class is SensorStateClass.TOTAL_INCREASING


def test_bitset_attributes(component, coordinator):
    by_key = {d.key: d for d in component.sensor.SENSORS}
    entity = component.sensor.NexBlueSensor(coordinator, by_key["waiting_code"])
    assert entity.extra_state_attributes == {"active": ["Fallback triggered by Modbus"]}


def test_binary_sensors(component, coordinator):
    import dataclasses

    states = {d.key: component.binary_sensor.NexBlueBinarySensor(coordinator, d).is_on
              for d in component.binary_sensor.BINARY_SENSORS}
    assert states == {"error": False, "warning": False}
    coordinator.data = dataclasses.replace(coordinator.data, error_bits=0b100000)
    error = component.binary_sensor.NexBlueBinarySensor(
        coordinator, component.binary_sensor.BINARY_SENSORS[0])
    assert error.is_on
    assert error.extra_state_attributes == {"active": ["Relay stuck"]}


def test_current_limit_number(component, coordinator):
    import asyncio

    number = component.number.NexBlueCurrentLimit(coordinator)
    assert number.native_value == 16.0
    assert (number.native_min_value, number.native_max_value) == (6.0, 20.0)  # app max 20 A
    asyncio.run(number.async_set_native_value(10.0))
    assert coordinator.written == [10.0]


def test_scan_interval_has_a_floor(component):
    entry = types.SimpleNamespace(data={"scan_interval": 10}, options={"scan_interval": 3})
    assert component.coordinator.scan_interval(entry) == 10
    entry = types.SimpleNamespace(data={"scan_interval": 10}, options={"scan_interval": 30})
    assert component.coordinator.scan_interval(entry) == 30
    entry = types.SimpleNamespace(data={}, options={})
    assert component.coordinator.scan_interval(entry) == 10


def test_config_flow_schema_defaults(component):
    import voluptuous as vol

    schema = component.config_flow._user_schema({})
    assert schema({"host": "192.0.2.1"}) == {
        "host": "192.0.2.1", "port": 502, "unit_id": 200, "scan_interval": 10}
    with pytest.raises(vol.Invalid):
        schema({"host": "192.0.2.1", "scan_interval": 5})
    with pytest.raises(vol.Invalid):
        schema({"host": "192.0.2.1", "unit_id": 248})


def test_values_are_rounded(component, coordinator):
    by_key = {d.key: d for d in component.sensor.SENSORS}
    current = component.sensor.NexBlueSensor(coordinator, by_key["current_l1"]).native_value
    assert current == 0.04 and current == round(coordinator.data.current_l1, 2)
    assert by_key["max_current"].state_class is None


def test_lifetime_energy_ignores_false_zero_and_drops(component, coordinator):
    import dataclasses

    by_key = {d.key: d for d in component.sensor.SENSORS}
    sensor = component.sensor.NexBlueSensor(coordinator, by_key["lifetime_energy"])
    assert sensor.native_value == 1_040_975
    for bad in (0, 1_000_000):
        coordinator.data = dataclasses.replace(coordinator.data, lifetime_energy=bad)
        assert sensor.native_value == 1_040_975
    coordinator.data = dataclasses.replace(coordinator.data, lifetime_energy=1_041_500)
    assert sensor.native_value == 1_041_500
    # Session energy resets every session, so it must not be guarded.
    session = component.sensor.NexBlueSensor(coordinator, by_key["session_energy"])
    coordinator.data = dataclasses.replace(coordinator.data, session_energy=0.0)
    assert session.native_value == 0.0


def test_lifetime_energy_restores_last_value(component, coordinator):
    import asyncio
    import dataclasses

    from homeassistant.components.sensor import SensorExtraStoredData

    by_key = {d.key: d for d in component.sensor.SENSORS}
    sensor = component.sensor.NexBlueSensor(coordinator, by_key["lifetime_energy"])

    async def last_data():
        return SensorExtraStoredData(native_value=1_040_975, native_unit_of_measurement="Wh")

    async def noop():
        return None

    sensor.async_get_last_sensor_data = last_data
    # Skip the CoordinatorEntity/RestoreEntity hass plumbing; only the seeding is tested.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(component.sensor.RestoreSensor, "async_added_to_hass", lambda self: noop())
        asyncio.run(sensor.async_added_to_hass())
    coordinator.data = dataclasses.replace(coordinator.data, lifetime_energy=0)
    assert sensor.native_value == 1_040_975


@pytest.mark.parametrize("app_max, expected", [(20.0, 20.0), (32.0, 32.0), (40.0, 32.0),
                                               (0.0, 32.0), (6.0, 6.0)])
def test_current_limit_max_follows_app_max(component, coordinator, app_max, expected):
    import dataclasses

    coordinator.data = dataclasses.replace(coordinator.data, max_current=app_max)
    assert component.number.NexBlueCurrentLimit(coordinator).native_max_value == expected
