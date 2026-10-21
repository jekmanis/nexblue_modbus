"""SSDP M-SEARCH probe using only the Python standard library.

Sends an SSDP M-SEARCH multicast request (ST: ssdp:all) to
239.255.255.250:1900, then listens for ~3 seconds collecting responses.
Prints all responders for context, and clearly flags any response coming
from the target IPs given on the command line.

    uv run python tools/ssdp_probe.py <charger-ip> [<other-ip> ...]
"""
import socket
import sys
import time

MCAST_ADDR = "239.255.255.250"
MCAST_PORT = 1900
LISTEN_SECONDS = 3
TARGET_IPS = set(sys.argv[1:])

MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
).encode("utf-8")


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.bind(("", 0))
    sock.settimeout(1)

    print(f"Sending SSDP M-SEARCH to {MCAST_ADDR}:{MCAST_PORT} ...")
    sock.sendto(MSEARCH, (MCAST_ADDR, MCAST_PORT))

    responses = []
    end_time = time.time() + LISTEN_SECONDS
    while time.time() < end_time:
        try:
            data, addr = sock.recvfrom(65535)
            responses.append((addr[0], data.decode(errors="replace")))
        except socket.timeout:
            continue
        except Exception:
            continue
    sock.close()

    print(f"\nTotal SSDP responses received: {len(responses)}")
    target_matches = []
    for ip, text in responses:
        marker = " <-- TARGET CHARGER" if ip in TARGET_IPS else ""
        print(f"\n--- Response from {ip}{marker} ---")
        print(text.strip())
        if ip in TARGET_IPS:
            target_matches.append((ip, text))

    print("\n=== Summary ===")
    if not target_matches:
        print("No SSDP responses from %s." % ", ".join(sorted(TARGET_IPS)))
    else:
        for ip, text in target_matches:
            print(f"TARGET MATCH: {ip}")
            print(text.strip())


if __name__ == "__main__":
    main()
