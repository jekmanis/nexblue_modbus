"""Config flow: host, port, unit id and scan interval, validated by reading the charger."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback

from .const import (
    CONF_UNIT_ID,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import NexBlueConfigEntry, scan_interval
from .nexblue_client import (
    DEFAULT_PORT,
    DEFAULT_UNIT_ID,
    NexBlueClient,
    NexBlueConnectionError,
    NexBlueError,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL,
                                                             max=MAX_SCAN_INTERVAL))


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)):
            vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Required(CONF_UNIT_ID, default=defaults.get(CONF_UNIT_ID, DEFAULT_UNIT_ID)):
            vol.All(vol.Coerce(int), vol.Range(min=1, max=247)),
        vol.Required(CONF_SCAN_INTERVAL,
                     default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)):
            SCAN_INTERVAL_VALIDATOR,
    })


class NexBlueConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Checked before connecting: a running entry already holds the charger's
            # connection, and a probe would open a second one.
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            client = NexBlueClient(user_input[CONF_HOST], port=user_input[CONF_PORT],
                                   unit_id=user_input[CONF_UNIT_ID])
            try:
                identity = await client.read_device_info()
            except NexBlueConnectionError as exc:
                _LOGGER.debug("validation failed: %s", exc)
                errors["base"] = "cannot_connect"
            except NexBlueError as exc:
                _LOGGER.debug("validation failed: %s", exc)
                errors["base"] = "read_failed"
            else:
                if not identity.serial:
                    # The serial number is the unique id; without it two chargers could
                    # not be told apart.
                    return self.async_abort(reason="no_serial")
                await self.async_set_unique_id(identity.serial)
                # The entry's update listener reloads it; letting the flow reload too
                # would set the integration up twice in a row.
                self._abort_if_unique_id_configured(updates={
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_UNIT_ID: user_input[CONF_UNIT_ID],
                }, reload_on_update=False)
                title = f"NexBlue {identity.model}" if identity.model else DEFAULT_NAME
                return self.async_create_entry(title=title, data=user_input)
            finally:
                await client.close()

        return self.async_show_form(step_id="user", data_schema=_user_schema(user_input or {}),
                                    errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: NexBlueConfigEntry) -> OptionsFlow:
        return NexBlueOptionsFlow()


class NexBlueOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=scan_interval(self.config_entry)):
                SCAN_INTERVAL_VALIDATOR,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
