"""Polling coordinator for one NexBlue charger."""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_MANUFACTURER, DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL
from .nexblue_client import (
    ChargerData,
    CurrentLimitKeepalive,
    NexBlueClient,
    NexBlueError,
    keepalive_interval,
)
from .nexblue_client import DeviceInfo as ChargerIdentity

_LOGGER = logging.getLogger(__name__)

type NexBlueConfigEntry = ConfigEntry[NexBlueCoordinator]


def scan_interval(entry: ConfigEntry) -> int:
    """Options override the value chosen in the config flow; never below the minimum."""
    seconds = entry.options.get(CONF_SCAN_INTERVAL,
                                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    return max(int(seconds), MIN_SCAN_INTERVAL)


class NexBlueCoordinator(DataUpdateCoordinator[ChargerData]):
    """Reads status, alarms and holding registers every scan interval.

    DataUpdateCoordinator logs a failed poll once, then stays quiet until the charger
    answers again ("... recovered"), so an unreachable charger doesn't flood the log.
    The client reconnects on the next poll by itself.

    Once HA has set the Current Limit, each poll also re-writes it when the keepalive
    is due (see CurrentLimitKeepalive), and the poll interval is shortened to the
    keepalive interval if the configured scan interval is longer.
    """

    config_entry: NexBlueConfigEntry

    def __init__(self, hass: HomeAssistant, entry: NexBlueConfigEntry,
                 client: NexBlueClient, identity: ChargerIdentity) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=timedelta(seconds=scan_interval(entry)),
        )
        self.client = client
        self.identity = identity
        self.keepalive = CurrentLimitKeepalive()
        self._scan_interval = scan_interval(entry)
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, identity.serial)},
            name=entry.title,
            manufacturer=identity.brand or DEFAULT_MANUFACTURER,
            model=identity.model or None,
            serial_number=identity.serial,
            sw_version=identity.firmware or None,
            hw_version=identity.hardware or None,
        )

    async def _async_update_data(self) -> ChargerData:
        poll_interval = self.update_interval.total_seconds() if self.update_interval else 0.0
        try:
            data = await self.client.poll(self.keepalive, poll_interval)
        except NexBlueError as exc:
            raise UpdateFailed(str(exc)) from exc
        self._apply_interval(data.fallback_timeout)
        return data

    def _apply_interval(self, fallback_timeout: int) -> None:
        seconds = float(self._scan_interval)
        if self.keepalive.setpoint is not None:
            seconds = min(seconds, keepalive_interval(fallback_timeout))
        self.update_interval = timedelta(seconds=seconds)

    async def async_set_current_limit(self, amps: float) -> None:
        readback = await self.client.set_current_limit(amps)
        self.keepalive.wrote(amps)
        if self.data is not None:
            self._apply_interval(self.data.fallback_timeout)
            self.async_set_updated_data(dataclasses.replace(self.data, current_limit=readback))

    async def async_start_charging(self) -> None:
        """IF_START = START (the client refuses unless the charger is preparing), then poll."""
        await self.client.start_charging()
        await self.async_request_refresh()
