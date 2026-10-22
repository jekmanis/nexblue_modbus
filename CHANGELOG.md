# Changelog

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
