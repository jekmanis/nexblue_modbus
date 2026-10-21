"""NexBlue EV charger over local Modbus TCP."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_UNIT_ID
from .coordinator import NexBlueConfigEntry, NexBlueCoordinator
from .nexblue_client import NexBlueClient, NexBlueError

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER,
                             Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: NexBlueConfigEntry) -> bool:
    client = NexBlueClient(entry.data[CONF_HOST], port=entry.data[CONF_PORT],
                           unit_id=entry.data[CONF_UNIT_ID])
    try:
        identity = await client.read_device_info()
        coordinator = NexBlueCoordinator(hass, entry, client, identity)
        await coordinator.async_config_entry_first_refresh()
    except NexBlueError as exc:
        await client.close()
        raise ConfigEntryNotReady(str(exc)) from exc
    except ConfigEntryNotReady:
        await client.close()
        raise

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NexBlueConfigEntry) -> bool:
    coordinator = entry.runtime_data
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await coordinator.client.close()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: NexBlueConfigEntry) -> None:
    """Options changed (scan interval): reload so the coordinator picks them up."""
    await hass.config_entries.async_reload(entry.entry_id)
