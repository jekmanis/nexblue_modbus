# Changelog

## 0.3.0 - 2026-10-22

- **Stop charging** button (IF_START = 0). It only acts while the charger is charging,
  suspended_evse or suspended_ev, and leaves the current limit and keepalive alone.
- **Phase mode** select (holding 1007, shown from input 138): adaptive, single phase or
  three phase. NexBlue's switching limits are enforced (10 min between switches, 2 per
  hour, 6 per session) and kept across restarts; choosing the current mode writes
  nothing. The phase mode itself is not restored after a charger power cycle.
- Write scope: 1000-1001, 1006 (0 or 1) and 1007 (0, 1 or 2). 1002-1005 stay read-only.
- README: comparison with NexBlue's YAML example for the built-in Modbus integration.

## 0.2.0 - 2026-10-22

- **Session energy:** a lower reading is accepted only after 3 consecutive low polls, so
  one false 0 no longer looks like a new session. Rises pass at once, and the last value
  is kept across restarts.
- **Current limit:** the limit HA last set survives HA restarts and integration reloads.
  It is written once on setup and the keepalive resumes.
- **Start charging** button (IF_START, holding 1006). It only acts while the charger is
  `preparing`. The write scope is now 1000-1001 plus 1006.
- **Car connected** binary sensor (device class plug), derived from the charger state.
- Waiting bit 7 is labelled "Phase switching".
- Marketplace / HACS readiness: brand icons, validation workflows.

## 0.1.0 - 2026-10-21

- Initial release: sensors, Current limit control with keepalive, config flow.
