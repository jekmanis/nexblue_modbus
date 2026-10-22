"""Phase mode: Runtime Phase Mode (holding 1007), shown from Phase charging (input 138).

Both registers use one encoding: 0 adaptive, 1 single phase, 2 three phase. Choosing the
mode the charger already reports writes nothing. A real change is refused (nothing
written) if it would break NexBlue's limits: 10 min between switches, 2 per rolling
hour, 6 per charging session. The limiter state (switch times as UTC timestamps and the
session count) is kept across HA restarts as restore data; the phase mode itself is not
restored or re-written, so after a charger power cycle it is whatever the charger
reverts to. Switches made from the NexBlue app or portal aren't counted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .entity import NexBlueEntity
from .nexblue_client import PHASE_MODE_NAMES, NexBlueError, PhaseSwitchLimitError

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

MODES_BY_NAME = {name: code for code, name in PHASE_MODE_NAMES.items()}


@dataclass
class PhaseLimiterStoredData(ExtraStoredData):
    """The phase-switch limiter state, not the phase mode."""

    limiter: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"limiter": self.limiter}


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([NexBluePhaseMode(entry.runtime_data)])


class NexBluePhaseMode(NexBlueEntity, SelectEntity, RestoreEntity):
    _attr_translation_key = "phase_mode"
    _attr_options = list(PHASE_MODE_NAMES.values())

    def __init__(self, coordinator: NexBlueCoordinator) -> None:
        super().__init__(coordinator, "phase_mode_select")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.phase_mode_name

    @property
    def extra_restore_state_data(self) -> PhaseLimiterStoredData:
        return PhaseLimiterStoredData(self.coordinator.phase_limiter.as_dict())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is None:
            return
        stored = extra.as_dict().get("limiter")
        if not isinstance(stored, dict) or not self.coordinator.phase_limiter.restore(stored):
            _LOGGER.warning("Ignoring unreadable stored phase-switch limiter state")

    async def async_select_option(self, option: str) -> None:
        if option == self.current_option:
            _LOGGER.debug("Phase mode is already %s; nothing written", option)
            return
        try:
            written = await self.coordinator.async_set_phase_mode(MODES_BY_NAME[option])
        except PhaseSwitchLimitError as exc:
            raise HomeAssistantError(f"Phase switch to {option} refused: {exc}. "
                                     "Nothing was sent.") from exc
        except NexBlueError as exc:
            raise HomeAssistantError(f"Setting phase mode {option} failed: {exc}") from exc
        if written:
            _LOGGER.info("Phase mode set to %s", option)
