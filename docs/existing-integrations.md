# Existing NexBlue integrations (checked 2026-10-21)

Question: is there a maintained Home Assistant integration that talks to the NexBlue
Edge 2 over **local Modbus TCP** (port 502, unit 200)?

**Verdict: no.** Every NexBlue HA integration found uses the NexBlue cloud. So
`nexblue_modbus` is built here.

| Source | URL | What it is | Verdict |
|---|---|---|---|
| HACS default list | https://raw.githubusercontent.com/hacs/default/master/integration | Full list grepped for "nexblue" | No entry |
| GitHub repo search "nexblue modbus", "_nexblue_modbus" | https://github.com/search?q=nexblue+modbus&type=repositories | 0 repositories | None |
| GitHub repo search "nexblue home assistant" | https://github.com/search?q=nexblue+home+assistant&type=repositories | 4 repositories, all cloud (below) | No Modbus |
| HA core `nexblue` (since 2026.9) | https://www.home-assistant.io/integrations/nexblue | `cloud_polling`, account login, `nexblue-api` | Cloud, doesn't count |
| NexBlue-AB/home-assistant-nexblue | https://github.com/NexBlue-AB/home-assistant-nexblue | Official cloud integration | Cloud, doesn't count |
| AndrewBarber/nexblue_hass | https://github.com/AndrewBarber/nexblue_hass | Unofficial cloud integration | Cloud, doesn't count |
| VaabenDK/HomeAssistant_NexBlueEVCloud | https://github.com/VaabenDK/HomeAssistant_NexBlueEVCloud | Cloud | Cloud, doesn't count |
| svemonix/Nexblue-Zen-HASS | https://github.com/svemonix/Nexblue-Zen-HASS | Cloud, Zen model | Cloud, doesn't count |
| kimdv/aionexblue | https://github.com/kimdv/aionexblue | Cloud API library (from FINDINGS.md, not re-checked) | Cloud, doesn't count |
| HA community forum | https://community.home-assistant.io/search?q=nexblue%20modbus | No thread found via web search (forum search itself not reachable from the agent) | None found |
| evcc template | https://github.com/evcc-io/evcc/blob/master/templates/definition/charger/nexblue.yaml | Cloud: user/password/serial, no Modbus parameters | Cloud, doesn't count |
| chloepriceless/dvhub | https://github.com/chloepriceless/dvhub | Node.js energy manager (not HA) with a Modbus TCP NexBlue driver: unit 200, DCBA, IR 100/126/130, HR 1000-1004 | Not HA; register map agrees with ours |
| timlaing/modbus_local_gateway | https://github.com/timlaing/modbus_local_gateway | Generic YAML-profile Modbus gateway | No NexBlue profile |

HA's built-in `modbus` YAML integration could read these registers too (`swap: word_byte`).
It has no config flow, no device info and no decoded state/bitset entities, so this
doesn't change the decision.
