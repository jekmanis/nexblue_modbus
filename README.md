# NexBlue Modbus TCP for Home Assistant

`nexblue_modbus` is a custom Home Assistant integration for the NexBlue Edge 2 EV
charger. It works over local Modbus TCP, with no cloud. It creates a **NexBlue <model>**
device with live sensors, a **Current limit** control, **Start/Stop charging** buttons and
a **Phase mode** select.

Tested with an Edge 2 (NB1620B, FW 03.09.97) on Home Assistant 2026.10.2, which bundles
pymodbus 3.13.1. The test suite also passes against 2026.10.0, the minimum in
`hacs.json`. Older releases aren't tested. The register map is in [FINDINGS.md](FINDINGS.md), and the vendor's
document is <https://nexblue.com/en-no/pages/nexblue-modbus-technical-doc>.

This is an independent community project. It is not affiliated with, endorsed by or
supported by NexBlue. "NexBlue" is used only to name the hardware it works with.

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

## 2. Installation

Home Assistant 2026.10.0 or later.

**HACS / Marketplace (custom repository).** The integration isn't in the default catalog
yet, so add it as a custom repository: in HACS open the menu (⋮) → **Custom
repositories**, enter `https://github.com/jekmanis/nexblue_modbus` with type
**Integration**, then download **NexBlue Modbus TCP** and restart Home Assistant. Updates
then show up like any other. Once it is in the default catalog, searching for it is
enough.

**Manual.** Copy `custom_components/nexblue_modbus/` (including `brand/` and
`translations/`) into the `custom_components/` folder of your Home Assistant config, then
**restart Home Assistant** (a full restart, because a new custom component isn't picked
up by a reload).

**Deploy tool.** Or use the deploy tool from this repo. It backs up, copies over SMB, restarts and adds
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
| `button.<device>_start_charging` | holding 1006 (IF_START) | Starts charging. Only acts in state `preparing` (below) |
| `button.<device>_stop_charging` | holding 1006 (IF_START) | Stops charging. Only acts in charging, suspended_evse, suspended_ev (below) |
| `select.<device>_phase_mode` | input 138 (shown), holding 1007 (written) | adaptive, single_phase, three_phase; limited to NexBlue's switching rules (below) |
| `binary_sensor.<device>_car_connected` | input 100 | Plug: on in preparing, charging, suspended_evse, suspended_ev, finishing; off in available, reserved, unavailable, faulted |
| `binary_sensor.<device>_error` | input 400 | Diagnostic, on when any error bit is set; attribute `active` lists them |
| `binary_sensor.<device>_warning` | input 401 | Diagnostic, as above for warnings |
| `sensor.<device>_error_code` / `_warning_code` / `_waiting_code` | input 400-402 | Diagnostic, raw bitsets with `active` labels |
| `sensor.<device>_fallback_current` | input 134-135 | Diagnostic, the app fallback current |
| `sensor.<device>_phase_mode` | input 138 | Diagnostic: adaptive, single_phase, three_phase |
| `sensor.<device>_modbus_fallback_limit` / `_timeout` | holding 1002-1005 | Diagnostic, disabled by default, read only |

The device info shows the model, serial number, firmware and hardware version.

**Write scope:** the integration writes only the Current limit (holding 1000-1001),
IF_START (holding 1006: 1 START, 0 STOP) from the two buttons, and Runtime Phase Mode
(holding 1007: 0, 1 or 2) from the Phase mode select. Fallback limit/timeout (1002-1005)
are only read, because writing them changes how the charger behaves without HA.

### Start and stop charging

**Start charging** writes IF_START = 1. It only acts while the charger is in state
`preparing`: a car is plugged in but charging hasn't started, for example because you
usually start it in the NexBlue app or by a schedule. **Stop charging** writes IF_START =
0 and only acts while the charger is `charging`, `suspended_evse` or `suspended_ev`. It
doesn't change the current limit or its keepalive. In any other state a press fails with
an error and nothing is sent. The state is checked again on the charger right before the
write.

- IF_START has no effect while the charger is controlled by OCPP (vendor document).
- The NexBlue app, portal and cloud API can also start and stop charging. Avoid sending
  conflicting commands from them and from Modbus at the same time; NexBlue recommends
  one authorization method at a time.

### Phase mode

The select shows Phase charging (input 138) and writes Runtime Phase Mode (holding
1007). Both use the same encoding: 0 adaptive, 1 single phase (forced), 2 three phase
(forced). Choosing the mode the charger already reports writes nothing.

NexBlue limits phase switching to at least 10 minutes between switches, at most 2 in
any 60 minutes and at most 6 per charging session. The integration refuses a change that
would break one of these with an error, and writes nothing:

- A session ends when the charger reports no car (available, reserved, unavailable,
  faulted); the per-session count then starts again.
- The switch times (UTC) and the session count are kept across HA restarts and reloads,
  so a restart doesn't reset the limits.
- Only switches made through this integration count. Switches from the NexBlue app or
  portal aren't seen, so leave enough room if you also use those.
- The phase mode itself is not restored or kept alive. After an AC power cycle the
  charger reverts its writable settings (1000-1007) to their defaults. The keepalive
  writes the current limit again; the phase mode stays at whatever the charger reverted
  to until you choose it again.

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
it again. That clears the stored limit. In a charging session (Edge 2, single phase), a
change from 16 A to 10 A and back took effect within about 10-15 s each way.

## Compared with NexBlue's YAML example

NexBlue publishes a YAML package for Home Assistant's built-in Modbus integration
([examples/modbus](https://github.com/NexBlue-AB/home-assistant-nexblue/tree/main/examples/modbus)).
It is a good reference and covers the same registers. The differences:

| | This integration | NexBlue's YAML example |
|---|---|---|
| Setup | Config flow from the UI, one device per charger with model, serial and firmware | YAML package in `configuration.yaml`, separate entities |
| Polling | One pass over four register blocks per scan interval | 38 sensors and a switch, each polled separately on its own interval (5-300 s) |
| Current limit | Kept alive: re-written before the fallback timeout runs out, and restored after HA restarts | One-shot write; the charger falls back after the timeout |
| Values | Named states and labelled bitsets, false-zero guards on the energy counters, phase-switch limits enforced, unit tests | Raw values; phase-switch limits documented, not enforced |
| Start/stop | Two buttons, each only in the states where it makes sense | One switch that writes 1006 |

What the YAML example has that this integration doesn't:

- Writes to the fallback limit and fallback timeout (1002-1005).
- Model support claims beyond the Edge 2: Edge Max and Point 2 (UK) on firmware 03.09.88
  or later, and Delta, Delta Max and Delta Max (UK) on 03.08.59 or later. This
  integration is only tested on an Edge 2.

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
