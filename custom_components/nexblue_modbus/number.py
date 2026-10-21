"""Current Limit control (holding 1000-1001).

Besides IF_START (1006, the Start charging button) the only register this integration
writes.

Range: 6 A (the IEC 61851 minimum charging current) up to the maximum current set in
the NexBlue app (input 136-137), never above the register's 32 A.

The limit HA last set is kept across HA restarts and entry reloads (restore state). On
setup it is written once more and the keepalive resumes, so the charger doesn't drop to
its fallback limit after a restart. A limit HA never set is never written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import CURRENT_LIMIT_MIN_UI
from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .entity import NexBlueEntity
from .nexblue_client import CURRENT_LIMIT_MAX, NexBlueError

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass
class CurrentLimitStoredData(ExtraStoredData):
    """The limit HA set (the keepalive setpoint), not the register reading.

    Restoring the reading would start the keepalive for chargers HA never controlled.
    """

    setpoint: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"setpoint": self.setpoint}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CurrentLimitStoredData | None:
        try:
            value = data["setpoint"]
            return cls(None if value is None else float(value))
        except (KeyError, TypeError, ValueError):
            return None


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([NexBlueCurrentLimit(entry.runtime_data)])


class NexBlueCurrentLimit(NexBlueEntity, NumberEntity, RestoreEntity):
    _attr_translation_key = "current_limit"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = CURRENT_LIMIT_MIN_UI
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: NexBlueCoordinator) -> None:
        super().__init__(coordinator, "current_limit")

    @property
    def native_max_value(self) -> float:
        app_max = self.coordinator.data.max_current
        if CURRENT_LIMIT_MIN_UI <= app_max <= CURRENT_LIMIT_MAX:
            return app_max
        return CURRENT_LIMIT_MAX

    @property
    def native_value(self) -> float:
        return self.coordinator.data.current_limit

    @property
    def extra_restore_state_data(self) -> CurrentLimitStoredData:
        return CurrentLimitStoredData(self.coordinator.keepalive.setpoint)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        stored = CurrentLimitStoredData.from_dict(extra.as_dict()) if extra is not None else None
        if stored is not None and stored.setpoint is not None:
            await self.async_resume_setpoint(stored.setpoint)

    async def async_resume_setpoint(self, setpoint: float) -> None:
        """Write the restored limit once (clamped to today's range) and resume the keepalive."""
        low, high = self.native_min_value, self.native_max_value
        amps = min(max(setpoint, low), high)
        if amps != setpoint:
            _LOGGER.warning("Stored current limit %g A is outside %g-%g A; using %g A",
                            setpoint, low, high, amps)
        try:
            await self.coordinator.async_set_current_limit(amps)
        except NexBlueError as exc:
            _LOGGER.warning("Could not restore the current limit %g A, keepalive not "
                            "resumed: %s", amps, exc)
            return
        _LOGGER.info("Restored current limit %g A; keepalive resumed", amps)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set_current_limit(value)
        except NexBlueError as exc:
            raise HomeAssistantError(f"Setting the current limit to {value:g} A failed: {exc}") from exc
