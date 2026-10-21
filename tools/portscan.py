"""Fast multi-threaded TCP connect port scanner.

Usage: uv run python tools/portscan.py <host>

Scans all TCP ports 1-65535 against the given host using a thread pool,
attempts a trivial banner grab on any open port, and prints the final
list of open ports (with banners, if any) at the end.
"""
import sys
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_WORKERS = 200
CONNECT_TIMEOUT = 0.5
BANNER_TIMEOUT = 0.75

open_ports = []
open_lock = threading.Lock()

scanned_count = 0
scanned_lock = threading.Lock()
TOTAL_PORTS = 65535


def scan_port(host, port):
    global scanned_count
    banner = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(CONNECT_TIMEOUT)
            result = s.connect_ex((host, port))
            if result == 0:
                # Port is open, try a trivial banner grab
                try:
                    s.settimeout(BANNER_TIMEOUT)
                    data = s.recv(256)
                    if data:
                        banner = data.decode(errors="replace").strip()
                except Exception:
                    banner = None
                with open_lock:
                    open_ports.append((port, banner))
    except Exception:
        pass
    finally:
        with scanned_lock:
            scanned_count += 1
            if scanned_count % 10000 == 0:
                print(f"  ...scanned {scanned_count}/{TOTAL_PORTS} ports", file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        print("Usage: python portscan.py <host>")
        sys.exit(1)
    host = sys.argv[1]
    print(f"Scanning {host} ports 1-65535 with {MAX_WORKERS} threads (timeout {CONNECT_TIMEOUT}s)...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(scan_port, host, port) for port in range(1, TOTAL_PORTS + 1)]
        for _ in as_completed(futures):
            pass

    open_ports.sort(key=lambda x: x[0])
    print(f"\nScan complete for {host}. Open ports: {len(open_ports)}")
    if not open_ports:
        print("No open TCP ports found.")
    else:
        for port, banner in open_ports:
            if banner:
                print(f"{port}/tcp open [{banner}]")
            else:
                print(f"{port}/tcp open")


if __name__ == "__main__":
    main()
