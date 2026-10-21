"""Constants for the NexBlue Modbus TCP integration."""

from __future__ import annotations

DOMAIN = "nexblue_modbus"

CONF_UNIT_ID = "unit_id"

DEFAULT_NAME = "NexBlue Edge 2"
DEFAULT_MANUFACTURER = "NexBlue"

# The charger is an ESP32 with a single Modbus TCP client slot; polling faster than this
# is refused by the config flow.
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 3600
DEFAULT_SCAN_INTERVAL = 10

# Lowest Current limit offered in the UI (IEC 61851 minimum); the register accepts 0-32 A.
CURRENT_LIMIT_MIN_UI = 6.0
