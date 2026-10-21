"""mDNS/DNS-SD probe using zeroconf.

Browses _services._dns-sd._udp.local. (the meta-service list) plus a
handful of common service types for ~5 seconds, collects discovered
service info, and prints any entries whose resolved IP address matches
one of the target IPs given on the command line.

    uv run python tools/mdns_probe.py <charger-ip> [<other-ip> ...]
"""
import socket
import sys
import time
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

TARGET_IPS = set(sys.argv[1:])

COMMON_SERVICE_TYPES = [
    "_services._dns-sd._udp.local.",
    "_http._tcp.local.",
    "_https._tcp.local.",
    "_modbus._tcp.local.",
    "_workstation._tcp.local.",
    "_device-info._tcp.local.",
    "_mqtt._tcp.local.",
]

discovered = []  # list of (service_type, name, addresses, info)


class Listener(ServiceListener):
    def add_service(self, zc, type_, name):
        # The meta-service browser (_services._dns-sd._udp.local.) yields
        # service *type* strings via `name`, not service instance names -
        # get_service_info() only works for concrete service instances, so
        # just record the discovered type string in that case.
        if type_ == "_services._dns-sd._udp.local.":
            discovered.append((type_, name, [], None))
            return
        try:
            info = zc.get_service_info(type_, name, timeout=1000)
        except Exception:
            info = None
        addrs = []
        if info:
            try:
                addrs = [socket.inet_ntoa(a) for a in info.addresses]
            except Exception:
                addrs = []
        discovered.append((type_, name, addrs, info))

    def update_service(self, zc, type_, name):
        pass

    def remove_service(self, zc, type_, name):
        pass


def main():
    zc = Zeroconf()
    listeners = []
    browsers = []
    try:
        for st in COMMON_SERVICE_TYPES:
            try:
                browsers.append(ServiceBrowser(zc, st, Listener()))
            except Exception as e:
                print(f"Could not browse {st}: {e}")
        print("Browsing mDNS for ~5 seconds...")
        time.sleep(5)
    finally:
        zc.close()

    print(f"\nTotal service entries discovered: {len(discovered)}")
    matches = []
    for type_, name, addrs, info in discovered:
        print(f"  {type_} {name} -> addresses={addrs}")
        if any(a in TARGET_IPS for a in addrs):
            matches.append((type_, name, addrs, info))

    print("\n--- Matches for target chargers (%s)" % ", ".join(sorted(TARGET_IPS)) + " ---")
    if not matches:
        print("No mDNS entries matched the target IPs.")
    else:
        for type_, name, addrs, info in matches:
            print(f"MATCH: {type_} {name} addrs={addrs}")
            if info:
                print(f"  port={info.port} server={info.server} properties={info.properties}")


if __name__ == "__main__":
    main()
