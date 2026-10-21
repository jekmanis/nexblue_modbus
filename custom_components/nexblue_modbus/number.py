"""Current Limit control (holding 1000-1001), the only register this integration writes.

Range: 6 A (the IEC 61851 minimum charging current) up to the maximum current set in
the NexBlue app (input 136-137), never above the register's 32 A.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CURRENT_LIMIT_MIN_UI
from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .entity import NexBlueEntity
from .nexblue_client import CURRENT_LIMIT_MAX, NexBlueError

PARALLEL_UPDATES = 1


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([NexBlueCurrentLimit(entry.runtime_data)])


class NexBlueCurrentLimit(NexBlueEntity, NumberEntity):
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

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set_current_limit(value)
        except NexBlueError as exc:
            raise HomeAssistantError(f"Setting the current limit to {value:g} A failed: {exc}") from exc
