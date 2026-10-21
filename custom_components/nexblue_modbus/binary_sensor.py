"""Binary sensors: car connected (from State, input 100) and the error/warning bitsets
(input 400, 401)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
from .nexblue_client import (
    CAR_CONNECTED_STATES,
    CAR_DISCONNECTED_STATES,
    ERROR_BITS,
    WARNING_BITS,
    ChargerData,
    active_bits,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NexBlueBinaryDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[ChargerData], bool | None]
    attrs_fn: Callable[[ChargerData], dict[str, Any]] | None = None


def car_connected(data: ChargerData) -> bool | None:
    """On while a car is plugged in; None (unknown) for an undocumented state."""
    if data.state_name in CAR_CONNECTED_STATES:
        return True
    if data.state_name in CAR_DISCONNECTED_STATES:
        return False
    return None


def _bitset(key: str, bits_fn: Callable[[ChargerData], int],
            labels: dict[int, str]) -> NexBlueBinaryDescription:
    return NexBlueBinaryDescription(
        key=key, translation_key=key, device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC, is_on_fn=lambda d: bits_fn(d) != 0,
        attrs_fn=lambda d: {"active": active_bits(bits_fn(d), labels)})


BINARY_SENSORS: tuple[NexBlueBinaryDescription, ...] = (
    NexBlueBinaryDescription(
        key="car_connected", translation_key="car_connected",
        device_class=BinarySensorDeviceClass.PLUG, is_on_fn=car_connected),
    _bitset("error", lambda d: d.error_bits, ERROR_BITS),
    _bitset("warning", lambda d: d.warning_bits, WARNING_BITS),
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
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
