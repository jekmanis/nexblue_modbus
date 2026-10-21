"""Direct Modbus TCP probe of a NexBlue Edge 2.

    uv run python tools/modbus_probe.py <charger-ip> [port]
"""
import sys
from pymodbus.client import ModbusTcpClient

if len(sys.argv) < 2:
    sys.exit("usage: modbus_probe.py <charger-ip> [port]")
HOST = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 502

c = ModbusTcpClient(HOST, port=PORT, timeout=3)
if not c.connect():
    print(f"{HOST}:{PORT} - cannot connect (TCP port closed / filtered)")
    sys.exit(1)
print(f"Connected to {HOST}:{PORT}")
for uid in (1, 0, 255):
    for fn, name in ((c.read_holding_registers, "holding"), (c.read_input_registers, "input")):
        try:
            r = fn(address=0, count=10, device_id=uid)
            print(f"  unit {uid} {name}[0..9]: {'ERROR ' + str(r) if r.isError() else r.registers}")
        except Exception as e:
            print(f"  unit {uid} {name}: EXCEPTION {e}")
c.close()
