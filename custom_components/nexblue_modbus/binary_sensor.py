"""Problem binary sensors for the error and warning bitsets (input 400, 401)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .entity import NexBlueEntity
from .nexblue_client import ERROR_BITS, WARNING_BITS, ChargerData, active_bits

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NexBlueBinaryDescription(BinarySensorEntityDescription):
    bits_fn: Callable[[ChargerData], int]
    labels: dict[int, str]


BINARY_SENSORS: tuple[NexBlueBinaryDescription, ...] = (
    NexBlueBinaryDescription(
        key="error", translation_key="error", device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC, bits_fn=lambda d: d.error_bits,
        labels=ERROR_BITS),
    NexBlueBinaryDescription(
        key="warning", translation_key="warning", device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC, bits_fn=lambda d: d.warning_bits,
        labels=WARNING_BITS),
)


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NexBlueBinarySensor(coordinator, d) for d in BINARY_SENSORS)


class NexBlueBinarySensor(NexBlueEntity, BinarySensorEntity):
    entity_description: NexBlueBinaryDescription

    def __init__(self, coordinator: NexBlueCoordinator,
                 description: NexBlueBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.bits_fn(self.coordinator.data) != 0

    @property
    def extra_state_attributes(self) -> dict[str, list[str]]:
        bits = self.entity_description.bits_fn(self.coordinator.data)
        return {"active": active_bits(bits, self.entity_description.labels)}
