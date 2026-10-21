"""Start charging: IF_START = START (holding 1006).

Only acts while the charger is ``preparing`` (car plugged in, charging not started). In
any other state the press raises an error and nothing is written; the client checks the
state again on the charger itself right before the write. There is no stop button.
IF_START has no effect while the charger is controlled by OCPP.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .entity import NexBlueEntity
from .nexblue_client import STATE_NAMES, STATE_PREPARING, NexBlueError

PARALLEL_UPDATES = 1


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([NexBlueStartCharging(entry.runtime_data)])


class NexBlueStartCharging(NexBlueEntity, ButtonEntity):
    _attr_translation_key = "start_charging"

    def __init__(self, coordinator: NexBlueCoordinator) -> None:
        super().__init__(coordinator, "start_charging")

    async def async_press(self) -> None:
        data = self.coordinator.data
        if data is None or data.state != STATE_PREPARING:
            state = "unknown" if data is None else STATE_NAMES.get(data.state, data.state)
            raise HomeAssistantError(
                f"Start charging only works when a car is plugged in and waiting (state "
                f"preparing); the charger is {state}. Nothing was sent.")
        try:
            await self.coordinator.async_start_charging()
        except NexBlueError as exc:
            raise HomeAssistantError(f"Start charging failed: {exc}") from exc
