#!/usr/bin/env python3
"""Browse the local network for NexBlue Modbus TCP services over mDNS.

NexBlue chargers advertise _nexblue_modbus._tcp.local. once "Modbus TCP" is
enabled in the NexBlue app (Edge 2 firmware >= 03.09.88).

    uv run python tools/mdns_nexblue.py
    uv run python tools/mdns_nexblue.py --seconds 15 --all
"""

from __future__ import annotations

import argparse
import socket
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

SERVICE = "_nexblue_modbus._tcp.local."
EXTRA_SERVICES = ["_modbus._tcp.local.", "_http._tcp.local."]


class Listener(ServiceListener):
    def __init__(self) -> None:
        self.found = {}

    def _describe(self, zc: Zeroconf, type_: str, name: str, event: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        print("[%s] %s" % (event, name))
        if info is None:
            print("    (no service info returned)")
            return
        addrs = []
        for raw in info.addresses:
            try:
                family = socket.AF_INET if len(raw) == 4 else socket.AF_INET6
                addrs.append(socket.inet_ntop(family, raw))
            except Exception:  # noqa: BLE001
                addrs.append(repr(raw))
        print("    server   : %s" % info.server)
        print("    addresses: %s" % (", ".join(addrs) if addrs else "(none)"))
        print("    port     : %s" % info.port)
        if info.properties:
            for key, value in info.properties.items():
                key_s = key.decode("utf-8", "replace") if isinstance(key, bytes) else key
                val_s = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
                print("    prop     : %s = %s" % (key_s, val_s))
        self.found[name] = (addrs, info.port)
        for addr in addrs:
            if ":" not in addr:
                print("    probe    : uv run python tools/nexblue_modbus.py %s read" % addr)

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._describe(zc, type_, name, "ADD")

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._describe(zc, type_, name, "UPDATE")

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        print("[REMOVE] %s" % name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=8.0, help="browse duration (default 8)")
    parser.add_argument("--all", action="store_true",
                        help="also browse _modbus._tcp and _http._tcp")
    args = parser.parse_args()

    types = [SERVICE] + (EXTRA_SERVICES if args.all else [])
    print("Browsing %s for %.0f s ..." % (", ".join(types), args.seconds))

    zc = Zeroconf()
    listener = Listener()
    browsers = [ServiceBrowser(zc, t, listener) for t in types]
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        for browser in browsers:
            browser.cancel()
        zc.close()

    print("\n--- SUMMARY ---")
    if not listener.found:
        print("No services found.")
        print("If the charger is on this LAN, make sure 'Modbus TCP' is enabled in the "
              "NexBlue app (charger settings) and firmware is >= 03.09.88; mDNS also "
              "needs UDP 5353 to be un-blocked (Windows Firewall / VLAN / Wi-Fi client "
              "isolation can hide it).")
        return 1
    for name, (addrs, port) in sorted(listener.found.items()):
        print("%s -> %s port %s" % (name, ", ".join(addrs) or "(no address)", port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
