# NexBlue Edge 2 – lokālās piekļuves izpēte (2026-09-10)

## Iekārtas LAN
IP adreses, hostname un sērijas numurs publiskajai versijai aizstāti ar piemēra vērtībām (192.0.2.0/24 = dokumentācijas tīkls).

| IP | Hostname | MAC | OUI |
|---|---|---|---|
| 192.0.2.10 (lādētājs) | NEXBLUE_XXXX | (MAC izņemts) | Espressif (ESP32) |
| 192.0.2.11 (slodzes mērītājs) | NEXBLUE_YYYY | (MAC izņemts) | Espressif (ESP32) |

## Rezultāts
- Ping: atbild abas.
- TCP 1–65535 (tools/portscan.py): **0 atvērtu portu** abām. 502 atbild ar RST ("actively refused").
- pymodbus (tools/modbus_probe.py): savienojums neizdodas – Modbus TCP slave nav.
- mDNS: nekā no šīm IP. SSDP: nepārliecinošs (iespējams Windows firewall).
- Tiešsaistē (pirmais meklējums neatrada; lietotājs atrada): oficiāla Modbus TCP dokumentācija EKSISTĒ – skat. sadaļu zemāk. Vecā SV produkta lapa minēja "Modbus",
  jaunā V1.2 lapa vārdu izņēmusi; RS-485 grupēts kopā ar CT/loadshedding => charger ir Modbus *master*
  pret skaitītāju, nevis slave.

## OFICIĀLAIS MODBUS TCP (atrasts vēlāk): https://nexblue.com/en-no/pages/nexblue-modbus-technical-doc
- Modbus TCP, ports 502, Unit ID pēc noklusējuma **200** (1–247). **Jāieslēdz NexBlue App → charger settings → "Modbus TCP"**.
- Edge 2 / Edge Max / Point 2 nepieciešama FW ≥ **03.09.88** (Delta: 03.08.59). mDNS: `_nexblue_modbus._tcp.local`.
- Funkcijas: 0x03, 0x04, 0x06, 0x10. Viena reģistra vērtības big-endian; vairāku reģistru vērtības (FLOAT32/UINT32/UINT64) – "DCBA" (little-endian baiti un vārdi). Pārbaudīt praktiski ar Date reģistru 0038–0039 (YYYYMMDD).
- Input (0x04): 0–7 Brand, 8–15 Model, 16–27 Serial, 28–35 FW, 36 Mapping ver (0x0100/0x0102), 37 ConnectorCount, 38–39 Date, 40–41 Time, 42 TZ min, 44 HW ver;
  100 State (0 Available,1 Preparing,2 Charging,3 SuspEVSE,4 SuspEV,5 Finishing,7 Unavailable,8 Faulted); 102–107 I L1..L3 (FLOAT32 A); 108–113 U L1..L3-N (V);
  114–119 fāžu leņķi; 120–127 P L1,L2,L3,Total (W); 128–129 Session kWh; 130–133 Lifetime Wh (UINT64); 134–135 Fallback A; 136–137 Max A; 138 Phase mode (0 Adaptive,1 1ph,2 3ph);
  400 Error bitset, 401 Warning bitset, 402 Waiting bitset (bit6 = fallback triggered by Modbus).
- Holding RW (0x03/0x06/0x10): 1000–1001 Current Limit FLOAT32 A (0–32, default 16); 1002–1003 Fallback Limit A (0–16, <6→0); 1004–1005 Fallback Timeout s (30–1800, default 300, heartbeat);
  1006 IF_START (0 STOP/1 START; nestrādā, ja charger kontrolē OCPP); 1007 Runtime Phase Mode (0/1/2; max 2 pārslēgšanas/h, 6/sesijā, ≥10 min intervāls).
- Secinājums: mūsu skens rāda 502 aizvērtu, jo Modbus TCP aplikācijā vēl nav ieslēgts (vai FW par vecu). Skripts: tools/nexblue_modbus.py.

## Citi API ceļi
1. **Mākoņa REST API** (OpenAPI: https://prod-management.nexblue.com/swagger/dist/openapi_gen.json,
   bāze https://api.nexblue.com/third_party/openapi). Login user/pass -> access_token (Authorization header bez "Bearer").
   GET /chargers/{id}/cmd/status, POST .../start_charging, .../stop_charging, .../set_current_limit {"current_limit": 6..32},
   GET /measurement/chargers/{id}, GET /sessions/charger/{id}. Komandas prasa charger online; set_current_limit tikai owner/installer.
   Klienti: github NexBlue-AB/nexblue-api, NexBlue-AB/home-assistant-nexblue, AndrewBarber/nexblue_hass, kimdv/aionexblue, evcc template `nexblue`.
2. **Lokāls OCPP 1.6-J / 2.0.1** – Partner app -> Locations -> Settings -> Operator -> Customized OCPP platform (ws:// / wss://), FW >= 1.1.2.
   Pārslēdzot uz custom OCPP, mākoņa API komandas šim charger vairs nestrādā. 1<->3 fāžu pārslēgšana caur OCPP nestrādā.
3. BLE 4.2 – proprietārs, nedokumentēts.

## Avoti
- https://cdn.shopify.com/s/files/1/0686/5276/1363/files/NexBlue_NexBlue_Edge_2_Product_Sheet_V1.2.pdf
- https://cdn.shopify.com/s/files/1/0686/5276/1363/files/NexBlue_NexBlue_Edge_2_Product_sheet_SV.pdf
- https://nexblue.com/blogs/integration-guide/install-ocpp-on-the-nexblue-charger
- https://docs.evcc.io/en/chargers/nexblue-edge-2/
- Manuālis (201 lpp., tikai attēli): https://drive.google.com/file/d/14b_6vH6Hz8y19gpx9ypEIzjiepvJ1x-t/view

## 2026-10-21 – Modbus TCP ieslēgts, savienojamības pārbaude
- **192.0.2.10 (lādētājs) – STRĀDĀ.** 502 atvērts, Unit ID 200, mapping 0x0100 (v1.0). Modelis NB1620B, S/N E2TEST0001 (aizstāts),
  FW 03.09.97, HW 02.05.01. mDNS `NexBlue EV Charger Modbus Server._nexblue_modbus._tcp.local.` -> 192.0.2.10:502.
- Baitu secība **DCBA apstiprināta** (Date 20261021, Time 13:20:28, TZ +120 min).
- Reģistrs **139 neeksistē** (exception 2) -> statusa bloks jālasa 100–138 (39 reģ.); tools/nexblue_modbus.py izlabots.
- Momentuzņēmums: State 0 Available, U ≈ 234.7/233.4/236.1 V, leņķi 0/120/240°, P = 0 W, Session 18.03 kWh,
  Lifetime 1 040 975 Wh, Max 20 A, Fallback (app) 6 A; holding: Current Limit 16 A, Fallback Limit -1.0 (nav iestatīts),
  Fallback Timeout 300 s, IF_START 0 (STOP), Phase mode 0 Adaptive. Kļūdu/brīdinājumu biti = 0.
- **192.0.2.11 (slodzes mērītājs) – Modbus TCP NAV.** Ping atbild, 502 "actively refused", mDNS nereklamē.
  Dokumentācija aptver tikai lādētājus; par mērītāju vienīgais ir Waiting bit0 "Load balancer disconnected" (reģ. 402).
- **write-current pārbaudīts (2026-10-21):** 0x10 uz 1000–1001, 10 A (raw [0x0000 0x2041]) -> WRITE OK, nolasīts 10.000 A;
  kļūdu/brīdinājumu/gaidīšanas biti 0; lietotnes Max (136–137, 20 A) un Fallback (134–135, 6 A) nemainās. Atjaunots 16 A, nolasīts 16.000 A.
  Ietekme uz reālu lādēšanu nav pārbaudīta (auto nebija pieslēgts, State Available).

## 2026-10-21 – HA integrācijas darba atklājumi
- **Lādētājs apkalpo tikai VIENU Modbus TCP klientu vienlaikus.** Kamēr Home Assistant tur savienojumu, otrs klients
  (tools/nexblue_modbus.py) saņem `ConnectionResetError [WinError 10054]` visiem unit ID; esošais klients turpina strādāt.
  Rīka lietošanai vispirms jāatslēdz HA config entry (tools/live_current_limit_test.py to dara automātiski).
- Input 402 Waiting bit6 ("Fallback triggered by Modbus") bija iestatīts jau pirms integrācijas (Fallback Timeout 300 s,
  Fallback Limit -1 = nav iestatīts) – lasīšana neskaitās Modbus heartbeat. Tāpēc integrācija pēc Current Limit iestatīšanas
  to periodiski pārraksta (keepalive, skat. README). Pārbaudīts dzīvē: pēc 16 A ieraksta no HA bit6 nodzisa
  uzreiz un palika 0 ~380 s (ilgāk par 300 s timeout) – ieraksts skaitās heartbeat, keepalive strādā. Ietekme uz reālu lādēšanas sesiju nav pārbaudīta (auto nebija pieslēgts).
