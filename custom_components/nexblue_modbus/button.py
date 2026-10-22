"""Start and Stop charging: IF_START (holding 1006), 1 = START, 0 = STOP.

Start only acts while the charger is ``preparing`` (car plugged in, charging not
started); Stop only while it is charging, suspended_evse or suspended_ev. In any other
state the press raises an error and nothing is written; the client checks the state
again on the charger itself right before the write. Stop doesn't touch the current
limit or its keepalive. IF_START has no effect while the charger is controlled by OCPP.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .entity import NexBlueEntity
from .nexblue_client import (
    START_ALLOWED_STATES,
    STATE_NAMES,
    STOP_ALLOWED_STATES,
    NexBlueError,
)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class NexBlueButtonDescription(ButtonEntityDescription):
    allowed_states: frozenset[str]
    requirement: str
    press_fn: Callable[[NexBlueCoordinator], Awaitable[None]]


BUTTONS: tuple[NexBlueButtonDescription, ...] = (
    NexBlueButtonDescription(
        key="start_charging", translation_key="start_charging",
        allowed_states=START_ALLOWED_STATES,
        requirement="when a car is plugged in and waiting (state preparing)",
        press_fn=lambda c: c.async_start_charging()),
    NexBlueButtonDescription(
        key="stop_charging", translation_key="stop_charging",
        allowed_states=STOP_ALLOWED_STATES,
        requirement="while charging or suspended (state charging, suspended_evse or "
                    "suspended_ev)",
        press_fn=lambda c: c.async_stop_charging()),
)


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(NexBlueChargeButton(entry.runtime_data, d) for d in BUTTONS)


class NexBlueChargeButton(NexBlueEntity, ButtonEntity):
    entity_description: NexBlueButtonDescription

    def __init__(self, coordinator: NexBlueCoordinator,
                 description: NexBlueButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        description = self.entity_description
        data = self.coordinator.data
        state = None if data is None else STATE_NAMES.get(data.state, data.state)
        if state not in description.allowed_states:
            what = description.key.replace("_", " ").capitalize()
            raise HomeAssistantError(
                f"{what} only works {description.requirement}; the charger is "
                f"{'unknown' if state is None else state}. Nothing was sent.")
        try:
            await description.press_fn(self.coordinator)
        except NexBlueError as exc:
            raise HomeAssistantError(
                f"{description.key.replace('_', ' ').capitalize()} failed: {exc}") from exc
