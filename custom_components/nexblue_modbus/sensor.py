"""Sensors: state, per-phase metering, energy, app limits and alarm bitsets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .entity import NexBlueEntity
from .nexblue_client import (
    ERROR_BITS,
    PHASE_MODE_NAMES,
    STATE_NAMES,
    WAITING_BITS,
    WARNING_BITS,
    ChargerData,
    ConfirmedDropGuard,
    MonotonicCounterGuard,
    active_bits,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NexBlueSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ChargerData], Any]
    attrs_fn: Callable[[ChargerData], dict[str, Any]] | None = None
    # Guard for total_increasing counters (see nexblue_client): false 0 / low readings
    # must not reach the statistics as a meter reset. Applied once per poll.
    guard_factory: Callable[[], MonotonicCounterGuard | ConfirmedDropGuard] | None = None


def _rounded(attr: str, digits: int = 2) -> Callable[[ChargerData], float]:
    # FLOAT32 readings such as 0.044599998742342 A would otherwise land in the
    # recorder with full float noise; suggested_display_precision only affects the UI.
    return lambda d: round(getattr(d, attr), digits)


def _current(key: str, attr: str, *, measurement: bool = True, **kwargs) -> NexBlueSensorDescription:
    return NexBlueSensorDescription(
        key=key, translation_key=key, device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT if measurement else None,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2, value_fn=_rounded(attr), **kwargs)


def _voltage(key: str, attr: str) -> NexBlueSensorDescription:
    return NexBlueSensorDescription(
        key=key, translation_key=key, device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1, value_fn=_rounded(attr))


def _power(key: str, attr: str) -> NexBlueSensorDescription:
    return NexBlueSensorDescription(
        key=key, translation_key=key, device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0, value_fn=_rounded(attr))


def _bitset(key: str, attr: str, labels: dict[int, str]) -> NexBlueSensorDescription:
    return NexBlueSensorDescription(
        key=key, translation_key=key, entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, a=attr: getattr(d, a),
        attrs_fn=lambda d, a=attr, l=labels: {"active": active_bits(getattr(d, a), l)})


SENSORS: tuple[NexBlueSensorDescription, ...] = (
    NexBlueSensorDescription(
        key="state", translation_key="state", device_class=SensorDeviceClass.ENUM,
        options=list(STATE_NAMES.values()), value_fn=lambda d: d.state_name),
    _current("current_l1", "current_l1"),
    _current("current_l2", "current_l2"),
    _current("current_l3", "current_l3"),
    _voltage("voltage_l1", "voltage_l1"),
    _voltage("voltage_l2", "voltage_l2"),
    _voltage("voltage_l3", "voltage_l3"),
    _power("power_l1", "power_l1"),
    _power("power_l2", "power_l2"),
    _power("power_l3", "power_l3"),
    _power("power", "power_total"),
    NexBlueSensorDescription(
        key="session_energy", translation_key="session_energy",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2,
        value_fn=_rounded("session_energy", 3), guard_factory=ConfirmedDropGuard),
    NexBlueSensorDescription(
        key="lifetime_energy", translation_key="lifetime_energy",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=1,
        value_fn=lambda d: d.lifetime_energy, guard_factory=MonotonicCounterGuard),
    # A configured limit, not a measurement: no state_class, so no long-term statistics.
    _current("max_current", "max_current", measurement=False),
    _current("fallback_current", "fallback_current", entity_category=EntityCategory.DIAGNOSTIC),
    NexBlueSensorDescription(
        key="phase_mode", translation_key="phase_mode", device_class=SensorDeviceClass.ENUM,
        options=list(PHASE_MODE_NAMES.values()), entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.phase_mode_name),
    _bitset("error_code", "error_bits", ERROR_BITS),
    _bitset("warning_code", "warning_bits", WARNING_BITS),
    _bitset("waiting_code", "waiting_bits", WAITING_BITS),
    # Modbus fallback settings (holding 1002-1005), read-only here. -1 means "not set".
    NexBlueSensorDescription(
        key="modbus_fallback_limit", translation_key="modbus_fallback_limit",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        value_fn=lambda d: d.fallback_limit if d.fallback_limit >= 0 else None),
    NexBlueSensorDescription(
        key="modbus_fallback_timeout", translation_key="modbus_fallback_timeout",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        value_fn=lambda d: d.fallback_timeout),
)


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NexBlueSensor(coordinator, description) for description in SENSORS)


class NexBlueSensor(NexBlueEntity, RestoreSensor):
    entity_description: NexBlueSensorDescription

    def __init__(self, coordinator: NexBlueCoordinator,
                 description: NexBlueSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        factory = description.guard_factory
        self._guard = factory() if factory is not None else None
        self._guarded_value: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._guard is None:
            return
        # Seed the guard with the value from before the restart / reload, so a false
        # reading on the first poll afterwards is held back too.
        last = await self.async_get_last_sensor_data()
        if (last is not None and last.native_value is not None
                and last.native_unit_of_measurement == self.native_unit_of_measurement):
            try:
                self._guard.last = float(last.native_value)
            except (TypeError, ValueError):
                pass
        self._refresh_guarded_value()

    def _refresh_guarded_value(self) -> None:
        """Feed the current poll through the guard - exactly once per poll."""
        self._guarded_value = self._guard.filter(
            self.entity_description.value_fn(self.coordinator.data))

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._guard is not None:
            self._refresh_guarded_value()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> Any:
        if self._guard is not None:
            return self._guarded_value
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
