#!/usr/bin/env python3
"""Live check of the Current limit number: HA -> register 1000 -> HA, then restore.

The charger serves ONE Modbus TCP client at a time (a second one is reset), so the
HA config entry is disabled - releasing its connection - while tools/nexblue_modbus.py
reads register 1000, and enabled again afterwards. The original limit is read first
and restored in a ``finally`` block.

    uv run --with websockets python tools/live_current_limit_test.py \
        --ha-host 192.0.2.20 --charger 192.0.2.10 --token-file ~/ha_token.txt

``--ha-host`` / ``--charger`` may also come from ``HA_HOST`` / ``NEXBLUE_HOST``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

DOMAIN = "nexblue_modbus"
TOOL = Path(__file__).resolve().parent / "nexblue_modbus.py"


class Live:
    def __init__(self, args) -> None:
        self.base = "http://{0}:{1}".format(args.ha_host, args.ha_port)
        self.charger = args.charger
        self.test_value = args.value
        self._token = Path(args.token_file).expanduser().read_text(encoding="utf-8").split()[0]
        self.entry_id = self.entries()[0]["entry_id"]
        registry = self.ws({"type": "config/entity_registry/list"})
        self.entity = next(e["entity_id"] for e in registry
                           if e.get("platform") == DOMAIN and e["unique_id"].endswith("_current_limit"))

    def api(self, path, body=None):
        req = urllib.request.Request(
            self.base + path, method="POST" if body is not None else "GET",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")

    def ws(self, message):
        async def call():
            async with websockets.connect(self.base.replace("http", "ws", 1) + "/api/websocket",
                                          max_size=64 * 1024 * 1024) as conn:
                await conn.recv()
                await conn.send(json.dumps({"type": "auth", "access_token": self._token}))
                await conn.recv()
                await conn.send(json.dumps(dict(message, id=1)))
                while True:
                    reply = json.loads(await conn.recv())
                    if reply.get("id") == 1:
                        if not reply.get("success"):
                            raise RuntimeError("%s failed: %s" % (message["type"], reply))
                        return reply.get("result")

        return asyncio.run(call())

    def entries(self):
        return self.api("/api/config/config_entries/entry?domain=" + DOMAIN)

    def entry_state(self) -> str:
        return self.entries()[0]["state"]

    def ha_value(self) -> float:
        return float(self.api("/api/states/" + self.entity)["state"])

    def set_ha(self, value: float) -> None:
        self.api("/api/services/number/set_value", {"entity_id": self.entity, "value": value})
        time.sleep(2)

    def set_disabled(self, disabled: bool) -> None:
        self.ws({"type": "config_entries/disable", "entry_id": self.entry_id,
                 "disabled_by": "user" if disabled else None})
        want = "not_loaded" if disabled else "loaded"
        for _ in range(30):
            if self.entry_state() == want:
                break
            time.sleep(1)
        time.sleep(2 if disabled else 3)
        print("    entry %s: state=%s" % ("disabled" if disabled else "enabled", self.entry_state()))

    def register_1000(self) -> float:
        self.set_disabled(True)
        try:
            out = subprocess.run([sys.executable, str(TOOL), self.charger, "read"],
                                 capture_output=True, text=True, timeout=60).stdout
        finally:
            self.set_disabled(False)
        line = next((l.strip() for l in out.splitlines() if "[1000-1001]" in l), None)
        if line is None:
            raise RuntimeError("nexblue_modbus.py printed no register 1000:\n" + out)
        print("    tool: " + line)
        return float(re.search(r"(-?\d+\.\d+) A$", line).group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--charger", default=os.environ.get("NEXBLUE_HOST"),
                        help="charger IP (or env NEXBLUE_HOST)")
    parser.add_argument("--ha-host", default=os.environ.get("HA_HOST"),
                        help="Home Assistant host (or env HA_HOST)")
    parser.add_argument("--ha-port", type=int, default=8123)
    parser.add_argument("--value", type=float, default=10.0)
    args = parser.parse_args()
    if not (args.charger and args.ha_host):
        parser.error("--charger and --ha-host are required (or NEXBLUE_HOST / HA_HOST)")
    live = Live(args)
    print("entity: " + live.entity)

    original = live.register_1000()
    print("1. original: register 1000 = %g A, HA = %g A" % (original, live.ha_value()))
    ok = False
    try:
        live.set_ha(live.test_value)
        print("2. HA number.set_value %g -> HA = %g A" % (live.test_value, live.ha_value()))
        register = live.register_1000()
        ha_after = live.ha_value()
        print("   register 1000 = %g A, HA after reload = %g A" % (register, ha_after))
        ok = register == live.test_value == ha_after
        print("   %s" % ("PASS" if ok else "FAIL"))
    finally:
        live.set_ha(original)
        register = live.register_1000()
        ha_after = live.ha_value()
        restored = register == original == ha_after
        print("3. restored: register 1000 = %g A, HA = %g A, entry %s -> %s"
              % (register, ha_after, live.entry_state(), "PASS" if restored else "RESTORE FAILED"))
    return 0 if ok and restored else 1


if __name__ == "__main__":
    sys.exit(main())
