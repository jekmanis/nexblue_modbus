"""Base entity: one device per charger, keyed by serial number."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import NexBlueCoordinator


class NexBlueEntity(CoordinatorEntity[NexBlueCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: NexBlueCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.identity.serial}_{key}"
        self._attr_device_info = coordinator.device_info
