# NexBlue Modbus TCP for Home Assistant

`nexblue_modbus` is a custom Home Assistant integration for the NexBlue Edge 2 EV
charger. It works over local Modbus TCP, with no cloud. It creates a **NexBlue <model>**
device with live sensors and a **Current limit** control.

Tested with an Edge 2 (NB1620B, FW 03.09.97) on Home Assistant 2026.10.2, which bundles
pymodbus 3.13.1. The test suite also passes against 2026.10.0, the minimum in
`hacs.json`. Older releases aren't tested. The register map is in [FINDINGS.md](FINDINGS.md), and the vendor's
document is <https://nexblue.com/en-no/pages/nexblue-modbus-technical-doc>.

## 1. Enable Modbus TCP on the charger

1. Update the charger to firmware **03.09.88 or later** (Delta: 03.08.59).
2. In the NexBlue app, open charger settings and switch on **Modbus TCP**. Note the
   **Unit ID** (default 200).
3. Give the charger a fixed IP address (DHCP reservation) on your router.
4. Check from a PC: `uv sync`, then `uv run python tools/smoke_client.py --host <charger-ip>`.
   It should print the model, the serial number and live voltages.

**The charger serves only one Modbus TCP client at a time.** While Home Assistant is
connected, other Modbus tools (evcc, `tools/nexblue_modbus.py`, ...) are refused. Disable
the integration's config entry before you use another client.

## 2. Install

Copy `custom_components/nexblue_modbus/` into the `custom_components/` folder of your Home
Assistant config, then **restart Home Assistant** (a full restart, because a new custom
component isn't picked up by a reload).

Or use the deploy tool from this repo. It backs up, copies over SMB, restarts and adds
the config entry. It needs:

- [uv](https://docs.astral.sh/uv/)
- a Home Assistant long-lived access token (HA profile → Security), saved in a file
  outside the repo. The tool never prints it.
- the SMB share `\\<ha-host>\config` (Samba add-on). Mount it with `net use` without an
  explicit user, because an explicit user gives error 1219.

```powershell
uv sync
uv run --with websockets python tools/deploy_to_ha.py --host <ha-host> --token-file <path-to-ha-token> --dry-run --deploy --restart --setup-entry <charger-ip>
uv run --with websockets python tools/deploy_to_ha.py --host <ha-host> --token-file <path-to-ha-token> --deploy --restart --setup-entry <charger-ip>
```

`--host` is required (or set `HA_HOST`). The HA port defaults to 8123 and the share to
`\\<ha-host>\config`; change them with `--port` and `--share-root`. Backups go to
`<config>/nexblue_modbus_backups/backup-<timestamp>`. Roll back with
`--restore backup-<timestamp> --restart`.

## 3. Add the integration

Go to **Settings → Devices & services → Add integration → NexBlue Modbus TCP** and enter:

| Field | Default | Notes |
|---|---|---|
| Host | | Charger IP |
| Port | 502 | |
| Modbus unit ID | 200 | As set in the NexBlue app (1-247) |
| Scan interval | 10 s | 10-3600 s. The charger's ESP32 handles few requests. |

The flow reads the charger's identification registers before it saves anything. The
serial number becomes the unique ID. You can change the scan interval later under
**Configure**.

## 4. Entities

Entity IDs start with the device name, which defaults to `NexBlue <model>`. For an Edge 2 (model code NB1620B) that gives `<device>` = `nexblue_nb1620b`, e.g. `sensor.nexblue_nb1620b_state`.

| Entity | Source (function code, register) | Notes |
|---|---|---|
| `sensor.<device>_state` | input 100 | available, preparing, charging, suspended_evse, suspended_ev, finishing, unavailable, faulted |
| `sensor.<device>_current_l1` / `_l2` / `_l3` | input 102-107 | A |
| `sensor.<device>_voltage_l1` / `_l2` / `_l3` | input 108-113 | V, line to neutral |
| `sensor.<device>_power_l1` / `_l2` / `_l3` | input 120-125 | W |
| `sensor.<device>_power` | input 126-127 | W, total |
| `sensor.<device>_session_energy` | input 128-129 | kWh, resets each session |
| `sensor.<device>_lifetime_energy` | input 130-133 | Energy, `total_increasing`, usable in the Energy dashboard |
| `sensor.<device>_max_current` | input 136-137 | A, the maximum set in the app |
| `number.<device>_current_limit` | holding 1000-1001 | 6 A up to the app max current (input 136-137, at most 32 A); read and **write**, kept alive (below) |
| `binary_sensor.<device>_error` | input 400 | Diagnostic, on when any error bit is set; attribute `active` lists them |
| `binary_sensor.<device>_warning` | input 401 | Diagnostic, as above for warnings |
| `sensor.<device>_error_code` / `_warning_code` / `_waiting_code` | input 400-402 | Diagnostic, raw bitsets with `active` labels |
| `sensor.<device>_fallback_current` | input 134-135 | Diagnostic, the app fallback current |
| `sensor.<device>_phase_mode` | input 138 | Diagnostic: adaptive, single_phase, three_phase |
| `sensor.<device>_modbus_fallback_limit` / `_timeout` | holding 1002-1005 | Diagnostic, disabled by default, read only |

The device info shows the model, serial number, firmware and hardware version.

**Write scope:** the integration writes only the Current limit (holding 1000-1001).
Fallback limit/timeout, IF_START (start/stop) and phase mode are read or ignored, because
writing them changes charging behaviour.

### Current limit keepalive

The charger has a Modbus heartbeat: the Fallback Timeout (holding 1004-1005, default
300 s). If no Modbus **write** arrives within it, the charger falls back to its fallback
limit, and Waiting bit 6 "Fallback triggered by Modbus" is set. Reads don't count. So a
limit written once would quietly revert after five minutes.

Once you (or an automation) have set `Current limit` from Home Assistant, the
integration writes **the same value** to 1000-1001 again every third of the fallback
timeout. The interval is clamped to 10-120 s, so 100 s at the default 300 s timeout. The
keepalive:

- uses the same single connection as the polling, and writes nothing but 1000-1001.
- doesn't start until HA has set a limit. Until then the integration only reads, and the
  charger's own fallback behaves as without HA.
- shortens polling to the keepalive interval if your scan interval is longer.
- re-applies HA's value if the charger restarts and resets 1000 to its 16 A default.
- survives HA restarts and integration reloads. The limit HA last set is kept as restore
  state; on setup it is written once more and the keepalive resumes. Only a limit HA set
  itself is kept. The register value is never restored as one, so a charger HA never
  controlled stays read-only. If the stored value is outside today's 6 A to max-current
  range (for example, the app maximum was lowered), it's clamped and a warning is logged.

To hand control back to the charger's own fallback, delete the integration entry and add
it again. That clears the stored limit. Its effect during a real charging session hasn't
been verified yet.

## Development

```powershell
uv sync
uv run pytest tests                       # client, decoding, static checks
uv run --isolated --no-project --python 3.14 --with homeassistant==2026.10.2 --with pymodbus==3.13.1 --with pytest pytest tests   # also the HA-backed tests
uv run python -m compileall custom_components
uv run --with websockets python tools/live_current_limit_test.py --ha-host <ha-host> --charger <charger-ip> --token-file <path-to-ha-token>   # live: set 10 A, verify, restore
```

`custom_components/nexblue_modbus/nexblue_client.py` is plain Python with no Home
Assistant imports. It holds the register map, the decoding and the single-connection
client.
