#!/usr/bin/env python3
"""Read a NexBlue charger once through the integration's own client (read-only).

Prints the same fields as ``tools/nexblue_modbus.py read`` so the two can be compared,
and can save the raw register blocks as a test fixture.

    uv run python tools/smoke_client.py --host 192.0.2.10
    uv run python tools/smoke_client.py --host 192.0.2.10 --dump tests/fixtures/edge2_idle.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "nexblue_modbus"
sys.path.insert(0, str(COMPONENT_DIR))

import nexblue_client as nb  # noqa: E402 - path set up above


def show(info: nb.DeviceInfo, data: nb.ChargerData) -> None:
    rows = [
        ("Brand", info.brand), ("Model", info.model), ("Serial Number", info.serial),
        ("Firmware Version", info.firmware), ("Hardware Version", info.hardware),
        ("Modbus Mapping Version", "0x%04X" % info.mapping_version),
        ("Connector Count", info.connector_count),
        ("Chargepoint State", "%d = %s" % (data.state, data.state_name)),
        ("Current L1", "%.3f A" % data.current_l1),
        ("Current L2", "%.3f A" % data.current_l2),
        ("Current L3", "%.3f A" % data.current_l3),
        ("Voltage L1-N", "%.3f V" % data.voltage_l1),
        ("Voltage L2-N", "%.3f V" % data.voltage_l2),
        ("Voltage L3-N", "%.3f V" % data.voltage_l3),
        ("Active Power L1", "%.3f W" % data.power_l1),
        ("Active Power L2", "%.3f W" % data.power_l2),
        ("Active Power L3", "%.3f W" % data.power_l3),
        ("Active Power Total", "%.3f W" % data.power_total),
        ("Session Energy", "%.3f kWh" % data.session_energy),
        ("Lifetime Energy", "%d Wh" % data.lifetime_energy),
        ("Fallback Current (app/cloud)", "%.3f A" % data.fallback_current),
        ("Max Charging Current (app/cloud)", "%.3f A" % data.max_current),
        ("Phase Charging", "%d = %s" % (data.phase_mode, data.phase_mode_name)),
        ("Error bitset", "0x%04X %s" % (data.error_bits, nb.active_bits(data.error_bits, nb.ERROR_BITS))),
        ("Warning bitset", "0x%04X %s" % (data.warning_bits, nb.active_bits(data.warning_bits, nb.WARNING_BITS))),
        ("Waiting bitset", "0x%04X %s" % (data.waiting_bits, nb.active_bits(data.waiting_bits, nb.WAITING_BITS))),
        ("Current Limit", "%.3f A" % data.current_limit),
        ("Fallback Limit", "%.3f A" % data.fallback_limit),
        ("Fallback Timeout", "%d s" % data.fallback_timeout),
        ("IF_START", data.if_start),
        ("Runtime Phase Mode", data.runtime_phase_mode),
    ]
    for label, value in rows:
        print("  %-34s %s" % (label, value))


async def run(args) -> int:
    client = nb.NexBlueClient(args.host, port=args.port, unit_id=args.unit)
    try:
        raw = await client.read_raw()
    except nb.NexBlueError as exc:
        print("ERROR: %s" % exc)
        return 1
    finally:
        await client.close()

    info = nb.parse_identity(raw["identity"])
    data = nb.parse_data(raw["status"], raw["alarms"], raw["holding"])
    print("NexBlue %s:%d unit %d" % (args.host, args.port, args.unit))
    show(info, data)

    if args.dump:
        path = Path(args.dump)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=1) + "\n", encoding="utf-8")
        print("raw blocks written to %s" % path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=nb.DEFAULT_PORT)
    parser.add_argument("--unit", type=int, default=nb.DEFAULT_UNIT_ID)
    parser.add_argument("--dump", metavar="PATH", help="save the raw register blocks as JSON")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
