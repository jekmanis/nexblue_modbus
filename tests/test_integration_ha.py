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
               for name in ("config_flow", "coordinator", "sensor", "binary_sensor", "number",
                            "button", "select", "nexblue_client")}
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
        self.starts = 0
        self.stops = 0
        self.keepalive = nb.CurrentLimitKeepalive()
        self.fail_writes: Exception | None = None

    def async_add_listener(self, *_args, **_kwargs):
        return lambda: None

    async def async_set_current_limit(self, amps):
        # Mirrors NexBlueCoordinator: write, then arm the keepalive with the setpoint.
        if self.fail_writes is not None:
            raise self.fail_writes
        self.written.append(amps)
        self.keepalive.wrote(amps)

    async def async_start_charging(self):
        if self.fail_writes is not None:
            raise self.fail_writes
        self.starts += 1

    async def async_stop_charging(self):
        if self.fail_writes is not None:
            raise self.fail_writes
        self.stops += 1


@pytest.fixture
def coordinator(idle_dump):
    data = nb.parse_data(idle_dump["status"], idle_dump["alarms"], idle_dump["holding"])
    return _Coordinator(data, nb.parse_identity(idle_dump["identity"]))


def test_sensor_values(component, coordinator):
    values = {}
    for description in component.sensor.SENSORS:
        entity = component.sensor.NexBlueSensor(coordinator, description)
        values[description.key] = _poll(entity)
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

    by_key = {d.key: d for d in component.binary_sensor.BINARY_SENSORS}
    states = {key: component.binary_sensor.NexBlueBinarySensor(coordinator, d).is_on
              for key, d in by_key.items()}
    assert states == {"car_connected": False, "error": False, "warning": False}
    coordinator.data = dataclasses.replace(coordinator.data, error_bits=0b100000)
    error = component.binary_sensor.NexBlueBinarySensor(coordinator, by_key["error"])
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


def _poll(sensor, coordinator=None, **changes):
    """One coordinator update: optionally change the data, then run the per-poll hook."""
    import dataclasses

    if changes:
        sensor.coordinator.data = dataclasses.replace(sensor.coordinator.data, **changes)
    if sensor._guard is not None:
        sensor._refresh_guarded_value()
    return sensor.native_value


def test_lifetime_energy_ignores_false_zero_and_drops(component, coordinator):
    by_key = {d.key: d for d in component.sensor.SENSORS}
    sensor = component.sensor.NexBlueSensor(coordinator, by_key["lifetime_energy"])
    assert _poll(sensor) == 1_040_975
    for bad in (0, 1_000_000, 0, 0):
        assert _poll(sensor, lifetime_energy=bad) == 1_040_975   # never accepted
    assert _poll(sensor, lifetime_energy=1_041_500) == 1_041_500


def test_session_energy_single_poll_dip_is_held(component, coordinator):
    by_key = {d.key: d for d in component.sensor.SENSORS}
    session = component.sensor.NexBlueSensor(coordinator, by_key["session_energy"])
    assert _poll(session) == 18.034
    assert _poll(session, session_energy=0.0) == 18.034      # false 0, poll 1
    assert _poll(session, session_energy=18.034) == 18.034   # back: the dip is forgotten
    assert _poll(session, session_energy=0.0) == 18.034
    assert _poll(session, session_energy=2.0) == 18.034      # 2 low polls, then a rise
    assert _poll(session, session_energy=18.1) == 18.1
    # Reading native_value repeatedly must not count as extra polls.
    _poll(session, session_energy=0.0)
    for _ in range(5):
        assert session.native_value == 18.1


def test_session_energy_three_poll_drop_is_accepted(component, coordinator):
    by_key = {d.key: d for d in component.sensor.SENSORS}
    session = component.sensor.NexBlueSensor(coordinator, by_key["session_energy"])
    assert _poll(session) == 18.034
    assert _poll(session, session_energy=0.0) == 18.034
    assert _poll(session, session_energy=0.0) == 18.034
    assert _poll(session, session_energy=0.01) == 0.01       # 3rd low poll: new session
    assert _poll(session, session_energy=0.5) == 0.5         # rises pass immediately


def test_session_energy_rise_passes_immediately(component, coordinator):
    by_key = {d.key: d for d in component.sensor.SENSORS}
    session = component.sensor.NexBlueSensor(coordinator, by_key["session_energy"])
    assert _poll(session) == 18.034
    assert _poll(session, session_energy=18.5) == 18.5


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
    assert sensor.native_value == 1_040_975                 # fixture value, first poll
    assert _poll(sensor, lifetime_energy=0) == 1_040_975


@pytest.mark.parametrize("stored, polls, expected", [
    (25.5, [0.0], 25.5),                  # false 0 right after a restart is held
    (25.5, [0.0, 0.0, 0.0], 0.0),         # a real new session still gets through
    (25.5, [26.0], 26.0),
])
def test_session_energy_restores_last_value(component, coordinator, stored, polls, expected):
    import asyncio
    import dataclasses

    from homeassistant.components.sensor import SensorExtraStoredData

    by_key = {d.key: d for d in component.sensor.SENSORS}
    sensor = component.sensor.NexBlueSensor(coordinator, by_key["session_energy"])
    coordinator.data = dataclasses.replace(coordinator.data, session_energy=polls[0])

    async def last_data():
        return SensorExtraStoredData(native_value=stored, native_unit_of_measurement="kWh")

    async def noop():
        return None

    sensor.async_get_last_sensor_data = last_data
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(component.sensor.RestoreSensor, "async_added_to_hass", lambda self: noop())
        asyncio.run(sensor.async_added_to_hass())            # counts as the first poll
    for value in polls[1:]:
        _poll(sensor, session_energy=value)
    assert sensor.native_value == expected


@pytest.mark.parametrize("app_max, expected", [(20.0, 20.0), (32.0, 32.0), (40.0, 32.0),
                                               (0.0, 32.0), (6.0, 6.0)])
def test_current_limit_max_follows_app_max(component, coordinator, app_max, expected):
    import dataclasses

    coordinator.data = dataclasses.replace(coordinator.data, max_current=app_max)
    assert component.number.NexBlueCurrentLimit(coordinator).native_max_value == expected


# --------------------------------------------------------------------------
# Current limit kept across restarts / reloads
# --------------------------------------------------------------------------

def _add_number(component, coordinator, stored):
    """Run the number's async_added_to_hass with ``stored`` as the restored extra data."""
    import asyncio

    number = component.number.NexBlueCurrentLimit(coordinator)

    async def last_extra():
        return stored

    number.async_get_last_extra_data = last_extra
    asyncio.run(number.async_added_to_hass())
    return number


def test_restore_data_is_the_ha_setpoint_not_the_reading(component, coordinator):
    import asyncio

    number = component.number.NexBlueCurrentLimit(coordinator)
    assert number.native_value == 16.0
    assert number.extra_restore_state_data.as_dict() == {"setpoint": None}
    asyncio.run(number.async_set_native_value(10.0))
    assert number.extra_restore_state_data.as_dict() == {"setpoint": 10.0}


def test_restored_setpoint_is_written_once_and_keepalive_resumes(component, coordinator):
    stored = component.number.CurrentLimitStoredData(12.0)
    _add_number(component, coordinator, stored)
    assert coordinator.written == [12.0]
    assert coordinator.keepalive.setpoint == 12.0


@pytest.mark.parametrize("stored", [None, "no setpoint", "garbage"])
def test_nothing_stored_means_no_write_and_no_keepalive(component, coordinator, stored):
    from homeassistant.helpers.restore_state import RestoredExtraData

    data = {None: None,
            "no setpoint": RestoredExtraData({"setpoint": None}),
            "garbage": RestoredExtraData({"setpoint": "x"})}[stored]
    _add_number(component, coordinator, data)
    assert coordinator.written == []
    assert coordinator.keepalive.setpoint is None


def test_restored_extra_data_from_storage_round_trips(component, coordinator):
    from homeassistant.helpers.restore_state import RestoredExtraData

    _add_number(component, coordinator, RestoredExtraData({"setpoint": 16.0}))
    assert coordinator.written == [16.0]


@pytest.mark.parametrize("setpoint, expected", [(25.0, 20.0), (3.0, 6.0)])
def test_out_of_range_setpoint_is_clamped_with_a_warning(component, coordinator, caplog,
                                                         setpoint, expected):
    import logging

    with caplog.at_level(logging.WARNING):
        _add_number(component, coordinator, component.number.CurrentLimitStoredData(setpoint))
    assert coordinator.written == [expected]          # app max is 20 A, UI min 6 A
    assert coordinator.keepalive.setpoint == expected
    assert any("outside" in r.getMessage() for r in caplog.records)


def test_restore_write_failure_is_a_warning_not_a_crash(component, coordinator, caplog):
    import logging

    # The package's own exception class: tests import nexblue_client by path, which is a
    # different module object from custom_components.nexblue_modbus.nexblue_client.
    coordinator.fail_writes = component.number.NexBlueError("charger unreachable")
    with caplog.at_level(logging.WARNING):
        _add_number(component, coordinator, component.number.CurrentLimitStoredData(12.0))
    assert coordinator.keepalive.setpoint is None
    assert any("keepalive not resumed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Start charging button
# --------------------------------------------------------------------------

def _button(component, coordinator, key):
    by_key = {d.key: d for d in component.button.BUTTONS}
    return component.button.NexBlueChargeButton(coordinator, by_key[key])


def _press(component, coordinator, state=None, key="start_charging"):
    import asyncio
    import dataclasses

    if state is not None:
        code = next(c for c, n in nb.STATE_NAMES.items() if n == state)
        coordinator.data = dataclasses.replace(coordinator.data, state=code)
    asyncio.run(_button(component, coordinator, key).async_press())


def test_start_button_starts_when_preparing(component, coordinator):
    button = _button(component, coordinator, "start_charging")
    assert button.unique_id == "E2TEST0001_start_charging"
    _press(component, coordinator, "preparing")
    assert (coordinator.starts, coordinator.stops) == (1, 0)


@pytest.mark.parametrize("state", sorted(set(nb.STATE_NAMES.values()) - {"preparing"}))
def test_start_button_refused_in_any_other_state(component, coordinator, state):
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match=f"preparing.*{state}"):
        _press(component, coordinator, state)
    assert coordinator.starts == 0


@pytest.mark.parametrize("key", ["start_charging", "stop_charging"])
def test_buttons_refused_without_data(component, coordinator, key):
    from homeassistant.exceptions import HomeAssistantError

    coordinator.data = None
    with pytest.raises(HomeAssistantError):
        _press(component, coordinator, key=key)
    assert (coordinator.starts, coordinator.stops) == (0, 0)


@pytest.mark.parametrize("key, state", [("start_charging", "preparing"),
                                        ("stop_charging", "charging")])
def test_button_write_failure_is_a_ha_error(component, coordinator, key, state):
    from homeassistant.exceptions import HomeAssistantError

    coordinator.fail_writes = component.button.NexBlueError("charger unreachable")
    with pytest.raises(HomeAssistantError, match="charger unreachable"):
        _press(component, coordinator, state, key=key)


@pytest.mark.parametrize("state", sorted(nb.STATE_NAMES.values()))
def test_stop_button_in_every_state(component, coordinator, state):
    from homeassistant.exceptions import HomeAssistantError

    assert _button(component, coordinator, "stop_charging").unique_id == "E2TEST0001_stop_charging"
    if state in {"charging", "suspended_evse", "suspended_ev"}:
        _press(component, coordinator, state, key="stop_charging")
        assert coordinator.stops == 1
    else:
        with pytest.raises(HomeAssistantError, match=f"suspended_ev.*{state}. Nothing was sent"):
            _press(component, coordinator, state, key="stop_charging")
        assert coordinator.stops == 0
    assert coordinator.starts == 0
    assert coordinator.written == [] and coordinator.keepalive.setpoint is None  # limit untouched


# --------------------------------------------------------------------------
# Car connected
# --------------------------------------------------------------------------

def _car_connected(component, coordinator):
    by_key = {d.key: d for d in component.binary_sensor.BINARY_SENSORS}
    return component.binary_sensor.NexBlueBinarySensor(coordinator, by_key["car_connected"])


@pytest.mark.parametrize("code, name", sorted(nb.STATE_NAMES.items()))
def test_car_connected_for_every_state(component, coordinator, code, name):
    import dataclasses

    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

    expected = name in {"preparing", "charging", "suspended_evse", "suspended_ev", "finishing"}
    assert expected or name in {"available", "reserved", "unavailable", "faulted"}
    coordinator.data = dataclasses.replace(coordinator.data, state=code)
    sensor = _car_connected(component, coordinator)
    assert sensor.device_class is BinarySensorDeviceClass.PLUG
    assert sensor.unique_id == "E2TEST0001_car_connected"
    assert sensor.available
    assert sensor.is_on is expected
    assert sensor.extra_state_attributes is None


def test_car_connected_unknown_for_undocumented_state(component, coordinator):
    import dataclasses

    coordinator.data = dataclasses.replace(coordinator.data, state=42)
    assert _car_connected(component, coordinator).is_on is None


def test_binary_sensors_unavailable_without_data(component, coordinator):
    coordinator.data = None
    for description in component.binary_sensor.BINARY_SENSORS:
        assert not component.binary_sensor.NexBlueBinarySensor(coordinator, description).available



# --------------------------------------------------------------------------
# Phase mode select and its limiter
# --------------------------------------------------------------------------

class _PhaseClient:
    """Mirrors NexBlueClient.set_phase_mode against the fake coordinator's data."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.writes = []

    async def set_phase_mode(self, mode, before_write=None):
        if self.coordinator.data.phase_mode == mode:
            return False
        if before_write is not None:
            before_write()
        self.writes.append(mode)
        return True


class _WallClock:
    def __init__(self, now=1_760_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def phased(component, coordinator):
    """The fake coordinator with the real async_set_phase_mode and a real limiter."""
    coordinator.clock = _WallClock()
    coordinator.phase_limiter = component.nexblue_client.PhaseSwitchLimiter(coordinator.clock)
    coordinator.client = _PhaseClient(coordinator)

    async def no_refresh():
        return None

    coordinator.async_request_refresh = no_refresh
    coordinator.async_set_phase_mode = types.MethodType(
        component.coordinator.NexBlueCoordinator.async_set_phase_mode, coordinator)
    return coordinator


def _select(component, coordinator, option):
    import asyncio

    entity = component.select.NexBluePhaseMode(coordinator)
    asyncio.run(entity.async_select_option(option))
    return entity


def _set_phase(coordinator, code):
    import dataclasses

    coordinator.data = dataclasses.replace(coordinator.data, phase_mode=code)


@pytest.mark.parametrize("code, option", [(0, "adaptive"), (1, "single_phase"),
                                          (2, "three_phase"), (9, None)])
def test_phase_select_option_mapping(component, coordinator, code, option):
    _set_phase(coordinator, code)
    entity = component.select.NexBluePhaseMode(coordinator)
    assert entity.options == ["adaptive", "single_phase", "three_phase"]
    assert entity.current_option == option
    assert entity.unique_id == "E2TEST0001_phase_mode_select"
    assert component.select.MODES_BY_NAME == {"adaptive": 0, "single_phase": 1, "three_phase": 2}


def test_phase_select_current_mode_is_a_no_op(component, phased):
    _select(component, phased, "adaptive")              # fixture reports adaptive
    assert phased.client.writes == []
    assert phased.phase_limiter.as_dict() == {"switches": [], "session_switches": 0}


def test_phase_select_writes_a_change_and_counts_it(component, phased):
    _select(component, phased, "single_phase")
    assert phased.client.writes == [1]
    assert phased.phase_limiter.as_dict() == {"switches": [phased.clock.now], "session_switches": 1}


def test_phase_select_refuses_within_10_min_and_writes_nothing(component, phased):
    from homeassistant.exceptions import HomeAssistantError

    _select(component, phased, "single_phase")
    _set_phase(phased, 1)
    phased.clock.now += 300
    with pytest.raises(HomeAssistantError, match="10 min.*Nothing was sent"):
        _select(component, phased, "three_phase")
    assert phased.client.writes == [1]
    phased.clock.now += 300
    _select(component, phased, "three_phase")
    assert phased.client.writes == [1, 2]


def test_phase_select_refuses_a_third_switch_within_an_hour(component, phased):
    from homeassistant.exceptions import HomeAssistantError

    for code, option in ((1, "single_phase"), (2, "three_phase")):
        _select(component, phased, option)
        _set_phase(phased, code)
        phased.clock.now += 700
    with pytest.raises(HomeAssistantError, match="per hour"):
        _select(component, phased, "adaptive")
    assert phased.client.writes == [1, 2]


def test_phase_select_refuses_a_seventh_switch_in_a_session(component, phased):
    from homeassistant.exceptions import HomeAssistantError

    phased.phase_limiter.session_switches = 6
    with pytest.raises(HomeAssistantError, match="session"):
        _select(component, phased, "single_phase")
    assert phased.client.writes == []


def test_phase_limiter_survives_a_restart(component, phased):
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.restore_state import RestoredExtraData
    import asyncio

    before = _select(component, phased, "single_phase")
    stored = before.extra_restore_state_data.as_dict()
    assert stored == {"limiter": {"switches": [phased.clock.now], "session_switches": 1}}

    # "Restart": a fresh limiter, restored from storage, a few minutes later.
    phased.phase_limiter = component.nexblue_client.PhaseSwitchLimiter(phased.clock)
    after = component.select.NexBluePhaseMode(phased)

    async def last_extra():
        return RestoredExtraData(stored)

    async def noop():
        return None

    after.async_get_last_extra_data = last_extra
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(component.select.RestoreEntity, "async_added_to_hass", lambda self: noop())
        asyncio.run(after.async_added_to_hass())
    assert phased.phase_limiter.session_switches == 1
    _set_phase(phased, 1)
    phased.clock.now += 120
    with pytest.raises(HomeAssistantError, match="10 min"):
        asyncio.run(after.async_select_option("three_phase"))
    assert phased.client.writes == [1]


@pytest.mark.parametrize("stored", [{"limiter": "x"}, {"limiter": {"switches": 1}}, {}])
def test_phase_limiter_unreadable_storage_is_ignored(component, phased, caplog, stored):
    from homeassistant.helpers.restore_state import RestoredExtraData
    import asyncio
    import logging

    entity = component.select.NexBluePhaseMode(phased)

    async def last_extra():
        return RestoredExtraData(stored)

    async def noop():
        return None

    entity.async_get_last_extra_data = last_extra
    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as mp:
        mp.setattr(component.select.RestoreEntity, "async_added_to_hass", lambda self: noop())
        asyncio.run(entity.async_added_to_hass())
    assert phased.phase_limiter.as_dict() == {"switches": [], "session_switches": 0}
    assert any("phase-switch limiter" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("state, expected", [("charging", 5), ("available", 0),
                                             ("faulted", 0), ("finishing", 5)])
def test_every_poll_lets_the_limiter_see_the_session(component, phased, state, expected):
    import asyncio
    import dataclasses

    code = next(c for c, n in nb.STATE_NAMES.items() if n == state)
    data = dataclasses.replace(phased.data, state=code)

    async def poll(keepalive, interval):
        return data

    phased.client.poll = poll
    phased.update_interval = None
    phased._apply_interval = lambda timeout: None
    phased.phase_limiter.session_switches = 5
    asyncio.run(component.coordinator.NexBlueCoordinator._async_update_data(phased))
    assert phased.phase_limiter.session_switches == expected
