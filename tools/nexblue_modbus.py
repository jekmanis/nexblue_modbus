#!/usr/bin/env python3
"""CLI Modbus TCP probe for the NexBlue Edge 2 wallbox.

Register map per the official NexBlue Modbus technical documentation:
    https://nexblue.com/en-no/pages/nexblue-modbus-technical-doc

Charger side requirements:
  * "Modbus TCP" toggle enabled in the NexBlue app (charger settings)
  * Edge 2 / Edge Max / Point 2 (UK): firmware >= 03.09.88
    Delta / Delta Max / Delta Max (UK): firmware >= 03.08.59
  * TCP port 502, default Unit ID 200 (configurable 1-247)
  * mDNS service: _nexblue_modbus._tcp.local

Byte order (per doc): single-register values are big-endian; multi-register
values are "full little-endian (DCBA)" -- little-endian byte order inside each
16-bit register AND little-endian word order.  Vendor docs are often wrong
about this, so --order lets you pick dcba / abcd / badc / cdab and the
"endianness check" section decodes the Date/Time registers under all four.

The only write is Current Limit (holding 1000-1001, ``write-current``). Fallback,
IF_START and phase mode (1002-1007) are read but never written, because writing
them changes charging behaviour.

Usage examples:
    uv run python tools/nexblue_modbus.py 192.0.2.10 read
    uv run python tools/nexblue_modbus.py 192.0.2.10 raw --type input --start 100 --count 40
    uv run python tools/nexblue_modbus.py 192.0.2.10 write-current 10 --yes
    uv run python tools/nexblue_modbus.py selftest
"""

from __future__ import annotations

import argparse
import struct
import sys

from pymodbus.client import ModbusTcpClient

DEFAULT_PORT = 502
DEFAULT_UNIT = 200
TIMEOUT = 5
RETRIES = 1
UNIT_CANDIDATES = (200, 1, 255)
ORDERS = ("dcba", "abcd", "badc", "cdab")

SEP = "=" * 78


# --------------------------------------------------------------------------
# Register map
# --------------------------------------------------------------------------

STATE_NAMES = {
    0: "Available",
    1: "Preparing",
    2: "Charging",
    3: "SuspendedEVSE",
    4: "SuspendedEV",
    5: "Finishing",
    6: "Reserved",
    7: "Unavailable",
    8: "Faulted",
}

PHASE_NAMES = {
    0: "Adaptive",
    1: "Force single phase (1ph)",
    2: "Force three phase (3ph)",
}

IF_START_NAMES = {0: "STOP", 1: "START"}

ERROR_BITS = {
    0: "CP voltage abnormal",
    1: "Phase line leakage",
    2: "Current abnormal / overcurrent",
    3: "Voltage abnormal (over/under voltage)",
    4: "Temperature abnormal (high/low)",
    5: "Relay stuck",
    6: "RCD self-test failed",
    7: "CP diode short",
    8: "PEN open",
    9: "PE leakage",
    10: "PE open",
    11: "PP abnormal",
}

WARNING_BITS = {
    0: "Meter chip abnormal",
    1: "Inter-board communication abnormal",
    2: "RFID RF abnormal",
    3: "868 RF abnormal",
    4: "4G module abnormal",
    5: "Cover open abnormal",
    6: "Reserved",
    7: "TN system phase-loss abnormal",
    8: "IT neutral overcurrent",
    9: "Grid abnormal",
    10: "MID mode",
}

WAITING_BITS = {
    0: "Fallback due to CT offline",
    1: "Fallback due to main unit offline",
    2: "Paused: low-severity error / OCPP charging profile current is 0",
    3: "Host reduces current mid-charge",
    4: "First request sent but host has not responded",
    5: "Load shedding protection active",
    6: "Fallback triggered by Modbus",
}

# (address, register count, kind, label, unit)
IDENT_FIELDS = [
    (0, 8, "str", "Brand  STRING(16)", ""),
    (8, 8, "str", "Model  STRING(16)", ""),
    (16, 12, "str", "Serial Number  STRING(24)", ""),
    (28, 8, "str", "Firmware Version  STRING(16)", ""),
    (36, 1, "mapver", "Modbus Mapping Version  UINT16", ""),
    (37, 1, "u16", "Connector Count  UINT16", ""),
    (38, 2, "date", "Date  UINT32", "YYYYMMDD"),
    (40, 2, "time", "Time  UINT32", "HHMMSS"),
    (42, 1, "tz", "Timezone  UINT16", "minutes"),
    (44, 8, "str", "Hardware Version  STRING(16)", ""),
]

STATUS_FIELDS = [
    (100, 1, "state", "Chargepoint State  UINT16", ""),
    (102, 2, "f32", "Current L1", "A"),
    (104, 2, "f32", "Current L2", "A"),
    (106, 2, "f32", "Current L3", "A"),
    (108, 2, "f32", "Voltage L1-N", "V"),
    (110, 2, "f32", "Voltage L2-N", "V"),
    (112, 2, "f32", "Voltage L3-N", "V"),
    (114, 2, "f32", "Phase Angle L1 (ref L1-N)", "deg"),
    (116, 2, "f32", "Phase Angle L2 (ref L1-N)", "deg"),
    (118, 2, "f32", "Phase Angle L3 (ref L1-N)", "deg"),
    (120, 2, "f32", "Active Power L1", "W"),
    (122, 2, "f32", "Active Power L2", "W"),
    (124, 2, "f32", "Active Power L3", "W"),
    (126, 2, "f32", "Active Power Total", "W"),
    (128, 2, "f32", "Session Energy", "kWh"),
    (130, 4, "u64", "Lifetime Energy  UINT64", "Wh"),
    (134, 2, "f32", "Fallback Current (app/cloud)", "A"),
    (136, 2, "f32", "Max Charging Current (app/cloud)", "A"),
    (138, 1, "phase", "Phase Charging  UINT16", ""),
]

HOLDING_FIELDS = [
    (1000, 2, "f32", "Current Limit  RW (0-32, default 16)", "A"),
    (1002, 2, "f32", "Fallback Limit  RW (0-16, <6 => 0)", "A"),
    (1004, 2, "u32", "Fallback Timeout  RW (30-1800, default 300)", "s"),
    (1006, 1, "ifstart", "IF_START  RW (0=STOP, 1=START)", ""),
    (1007, 1, "phase", "Runtime Phase Mode  RW (0/1/2)", ""),
]


# --------------------------------------------------------------------------
# Decoding / encoding helpers
# --------------------------------------------------------------------------

def regs_to_bytes(regs) -> bytes:
    """Registers as seen on the wire (each big-endian) -> raw byte stream."""
    return b"".join(struct.pack(">H", int(r) & 0xFFFF) for r in regs)


def bytes_to_regs(data: bytes):
    return [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data), 2)]


def permute(data: bytes, order: str) -> bytes:
    """Map raw wire bytes <-> canonical big-endian bytes.

    All four permutations are self-inverse, so the same function serves both
    decoding (wire -> value) and encoding (value -> wire).

    abcd  big-endian words, big-endian bytes (no change)
    dcba  little-endian words AND little-endian bytes (full reverse)
    badc  big-endian word order, bytes swapped inside each register
    cdab  word order swapped, big-endian bytes inside each register
    """
    words = [data[i:i + 2] for i in range(0, len(data), 2)]
    if order == "abcd":
        pass
    elif order == "dcba":
        words = [w[::-1] for w in reversed(words)]
    elif order == "badc":
        words = [w[::-1] for w in words]
    elif order == "cdab":
        words = list(reversed(words))
    else:
        raise ValueError("unknown byte order: %r" % (order,))
    return b"".join(words)


def decode_float32(regs, order="dcba") -> float:
    return struct.unpack(">f", permute(regs_to_bytes(regs[:2]), order))[0]


def decode_uint32(regs, order="dcba") -> int:
    return struct.unpack(">I", permute(regs_to_bytes(regs[:2]), order))[0]


def decode_uint64(regs, order="dcba") -> int:
    return struct.unpack(">Q", permute(regs_to_bytes(regs[:4]), order))[0]


def encode_float32(value: float, order="dcba"):
    return bytes_to_regs(permute(struct.pack(">f", float(value)), order))


def encode_uint32(value: int, order="dcba"):
    return bytes_to_regs(permute(struct.pack(">I", int(value)), order))


def decode_string(regs) -> str:
    """STRING(n): 2 bytes per register, big-endian (high byte first)."""
    raw = regs_to_bytes(regs)
    return raw.split(b"\x00")[0].decode("utf-8", errors="replace").strip()


def decode_string_swapped(regs) -> str:
    """Same string with the two bytes of every register swapped."""
    raw = permute(regs_to_bytes(regs), "badc")
    return raw.split(b"\x00")[0].decode("utf-8", errors="replace").strip()


def looks_printable(text: str) -> bool:
    return bool(text) and all(32 <= ord(ch) < 127 for ch in text)


def signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


def fmt_regs(regs) -> str:
    return "[" + " ".join("0x%04X" % (int(r) & 0xFFFF) for r in regs) + "]"


def fmt_date(value: int) -> str:
    return "%04d-%02d-%02d" % (value // 10000, (value // 100) % 100, value % 100)


def date_plausible(value: int) -> bool:
    y, m, d = value // 10000, (value // 100) % 100, value % 100
    return 2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31


def fmt_time(value: int) -> str:
    return "%02d:%02d:%02d" % (value // 10000, (value // 100) % 100, value % 100)


def time_plausible(value: int) -> bool:
    h, m, s = value // 10000, (value // 100) % 100, value % 100
    return h <= 23 and m <= 59 and s <= 59


def decode_bits(value: int, labels) -> list:
    out = []
    for bit in range(16):
        if value & (1 << bit):
            out.append("bit%-2d %s" % (bit, labels.get(bit, "reserved/undocumented")))
    return out


def decode_field(regs, kind: str, order: str) -> str:
    """Human-readable decoding of one mapped field."""
    if kind == "str":
        text = decode_string(regs)
        if not looks_printable(text):
            swapped = decode_string_swapped(regs)
            if looks_printable(swapped):
                return "%r  (byte-swapped: %r)" % (text, swapped)
        return repr(text)
    if kind == "u16":
        return str(regs[0])
    if kind == "mapver":
        v = regs[0]
        note = "" if v in (0x0100, 0x0102) else "  <-- expected 0x0100 or 0x0102"
        return "0x%04X  (v%d.%d)%s" % (v, v >> 8, v & 0xFF, note)
    if kind == "state":
        return "%d = %s" % (regs[0], STATE_NAMES.get(regs[0], "unknown"))
    if kind == "phase":
        return "%d = %s" % (regs[0], PHASE_NAMES.get(regs[0], "unknown"))
    if kind == "ifstart":
        return "%d = %s" % (regs[0], IF_START_NAMES.get(regs[0], "unknown"))
    if kind == "tz":
        s = signed16(regs[0])
        return "%d unsigned / %+d signed  (UTC%+.1fh)" % (regs[0], s, s / 60.0)
    if kind == "date":
        v = decode_uint32(regs, order)
        note = "" if date_plausible(v) else "  <-- implausible, try another --order"
        return "%d -> %s%s" % (v, fmt_date(v), note)
    if kind == "time":
        v = decode_uint32(regs, order)
        note = "" if time_plausible(v) else "  <-- implausible, try another --order"
        return "%d -> %s%s" % (v, fmt_time(v), note)
    if kind == "f32":
        return "%.3f" % decode_float32(regs, order)
    if kind == "u32":
        return str(decode_uint32(regs, order))
    if kind == "u64":
        return str(decode_uint64(regs, order))
    raise ValueError("unknown field kind %r" % (kind,))


def print_fields(fields, base_addr: int, regs, order: str) -> None:
    for addr, count, kind, label, unit in fields:
        off = addr - base_addr
        span = "%04d" % addr if count == 1 else "%04d-%04d" % (addr, addr + count - 1)
        if off < 0 or off + count > len(regs):
            print("  [%-9s] %-44s <not in read block>" % (span, label))
            continue
        chunk = regs[off:off + count]
        print("  [%-9s] %-44s raw=%-28s %s %s"
              % (span, label, fmt_regs(chunk), decode_field(chunk, kind, order), unit))


# --------------------------------------------------------------------------
# Modbus plumbing
# --------------------------------------------------------------------------

class ProbeError(Exception):
    pass


def connect(args) -> ModbusTcpClient:
    client = ModbusTcpClient(args.host, port=args.port, timeout=TIMEOUT, retries=RETRIES)
    if not client.connect():
        raise ProbeError(
            "cannot reach %s:%d -- port %d closed -- enable 'Modbus TCP' in the "
            "NexBlue app charger settings and check firmware >= 03.09.88"
            % (args.host, args.port, args.port)
        )
    return client


def read_block(client, kind: str, start: int, count: int, unit: int):
    """Read one register block; never raises. Returns a list of ints or None."""
    fn = client.read_input_registers if kind == "input" else client.read_holding_registers
    try:
        rr = fn(start, count=count, device_id=unit)
    except Exception as exc:  # noqa: BLE001 - a probe must never crash
        print("  !! %s %d..%d: EXCEPTION %s: %s"
              % (kind, start, start + count - 1, type(exc).__name__, exc))
        return None
    if rr.isError():
        print("  !! %s %d..%d: MODBUS ERROR: %s" % (kind, start, start + count - 1, rr))
        return None
    return list(rr.registers)


def probe_unit(client, preferred: int):
    """Find a unit id that answers, trying the preferred one first, then 200/1/255."""
    candidates = [preferred] + [u for u in UNIT_CANDIDATES if u != preferred]
    for unit in candidates:
        try:
            rr = client.read_input_registers(36, count=1, device_id=unit)
        except Exception as exc:  # noqa: BLE001
            print("  unit %-3d -> EXCEPTION %s: %s" % (unit, type(exc).__name__, exc))
            continue
        if rr.isError():
            print("  unit %-3d -> error: %s" % (unit, rr))
            continue
        print("  unit %-3d -> OK, mapping version register 0036 = 0x%04X"
              % (unit, rr.registers[0]))
        return unit
    return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_read(client, args) -> int:
    print(SEP)
    print("NexBlue Modbus TCP probe -- %s:%d  (multi-register order: %s)"
          % (args.host, args.port, args.order))
    print(SEP)

    print("\n--- UNIT ID DETECTION (preferred %d, fallbacks %s) ---"
          % (args.unit, ", ".join(str(u) for u in UNIT_CANDIDATES)))
    unit = probe_unit(client, args.unit)
    if unit is None:
        print("\nTCP port %d is open on %s but no unit id answered." % (args.port, args.host))
        print("Check that 'Modbus TCP' is enabled in the NexBlue app, that the Unit ID "
              "matches (default 200, configurable 1-247) and firmware >= 03.09.88.")
        return 2
    print("  ==> using unit id %d" % unit)

    print("\n--- IDENTIFICATION (input registers 0x04, 0000-0051) ---")
    ident = read_block(client, "input", 0, 52, unit)
    if ident:
        print_fields(IDENT_FIELDS, 0, ident, args.order)
    else:
        print("  (identification block unavailable)")

    print("\n--- ENDIANNESS CHECK ---")
    if ident and len(ident) >= 42:
        print("  Mapping version 0036 raw = 0x%04X  (single register, big-endian; "
              "expected 0x0100 or 0x0102)" % ident[36])
        print("  Date 0038-0039 raw = %s    Time 0040-0041 raw = %s"
              % (fmt_regs(ident[38:40]), fmt_regs(ident[40:42])))
        print("  %-6s %-12s %-12s %-10s %-10s" % ("order", "date", "formatted", "time", "formatted"))
        for order in ORDERS:
            dval = decode_uint32(ident[38:40], order)
            tval = decode_uint32(ident[40:42], order)
            mark = ""
            if date_plausible(dval):
                mark += "  <-- plausible date"
            if time_plausible(tval):
                mark += "  <-- plausible time"
            print("  %-6s %-12d %-12s %-10d %-10s%s"
                  % (order, dval, fmt_date(dval), tval, fmt_time(tval), mark))
        print("  (doc claims dcba = full little-endian; override with --order)")
    else:
        print("  (needs input registers 0036-0041, not available)")

    print("\n--- STATUS / METERING (input registers 0x04, 0100-0138) ---")
    status = read_block(client, "input", 100, 39, unit)
    if status:
        print_fields(STATUS_FIELDS, 100, status, args.order)
    else:
        print("  (status block unavailable)")

    print("\n--- ALARMS (input registers 0x04, 0400-0402) ---")
    alarms = read_block(client, "input", 400, 3, unit)
    if alarms:
        for addr, labels, name in ((400, ERROR_BITS, "Error"),
                                   (401, WARNING_BITS, "Warning"),
                                   (402, WAITING_BITS, "Waiting")):
            value = alarms[addr - 400]
            print("  [%04d] %-7s bitset  raw=0x%04X  0b%s"
                  % (addr, name, value, format(value, "016b")))
            bits = decode_bits(value, labels)
            if not bits:
                print("           (no bits set)")
            for text in bits:
                print("           %s" % text)
    else:
        print("  (alarm block unavailable)")

    print("\n--- HOLDING REGISTERS (0x03, 1000-1007) ---")
    holding = read_block(client, "holding", 1000, 8, unit)
    if holding:
        print_fields(HOLDING_FIELDS, 1000, holding, args.order)
    else:
        print("  (holding block unavailable)")

    print("\n" + SEP)
    return 0


def cmd_raw(client, args) -> int:
    print("--- RAW %s REGISTERS %d..%d (unit %d, order %s) ---"
          % (args.type.upper(), args.start, args.start + args.count - 1,
             args.unit, args.order))
    unit = args.unit
    regs = read_block(client, args.type, args.start, args.count, unit)
    if regs is None:
        detected = probe_unit(client, args.unit)
        if detected is None or detected == unit:
            return 2
        unit = detected
        print("  retrying with unit id %d" % unit)
        regs = read_block(client, args.type, args.start, args.count, unit)
        if regs is None:
            return 2
    for i, value in enumerate(regs):
        print("  [%04d] 0x%04X  %6d  (signed %7d)"
              % (args.start + i, value, value, signed16(value)))
    print("\n  register pairs decoded with order %s:" % args.order)
    for i in range(0, len(regs) - 1, 2):
        addr = args.start + i
        pair = regs[i:i + 2]
        print("  [%04d-%04d] %s  f32=%-18g u32=%d"
              % (addr, addr + 1, fmt_regs(pair),
                 decode_float32(pair, args.order), decode_uint32(pair, args.order)))
    return 0


def do_write(client, args, addr: int, values, what: str, readback) -> int:
    """Shared write path: announce the plan, require --yes, write 0x10, read back."""
    span = "%04d" % addr if len(values) == 1 else "%04d-%04d" % (addr, addr + len(values) - 1)
    print("PLAN: write holding register(s) %s on %s:%d unit %d"
          % (span, args.host, args.port, args.unit))
    print("      %s" % what)
    print("      function 0x10 (write multiple), order %s, raw values %s"
          % (args.order, fmt_regs(values)))
    if not args.yes:
        print("\nDry run only -- nothing written. Pass --yes to perform this write.")
        return 0

    unit = args.unit
    try:
        rr = client.write_registers(addr, values, device_id=unit)
    except Exception as exc:  # noqa: BLE001
        print("WRITE EXCEPTION %s: %s" % (type(exc).__name__, exc))
        return 2
    if rr.isError():
        print("WRITE ERROR: %s" % rr)
        return 2
    print("WRITE OK")

    print("\n--- READ BACK (0x03) ---")
    start, count, fields = readback
    regs = read_block(client, "holding", start, count, unit)
    if regs is None:
        return 2
    print_fields(fields, start, regs, args.order)
    return 0


def cmd_write_current(client, args) -> int:
    if not 0.0 <= args.value <= 32.0:
        print("refusing: Current Limit must be 0-32 A (got %g)" % args.value)
        return 2
    return do_write(client, args, 1000, encode_float32(args.value, args.order),
                    "Current Limit = %g A  (FLOAT32, range 0-32, default 16)" % args.value,
                    (1000, 2, [HOLDING_FIELDS[0]]))


# --------------------------------------------------------------------------
# Offline self test of the decoding helpers
# --------------------------------------------------------------------------

def cmd_selftest(_client, _args) -> int:
    failures = []

    def check(name, got, want):
        ok = got == want
        if not ok:
            failures.append(name)
        print("  %-50s %s  got=%r want=%r" % (name, "PASS" if ok else "FAIL", got, want))

    print("--- decoding / encoding self test ---")
    # FLOAT32 16.0 == 0x41800000 -> canonical bytes 41 80 00 00
    check("encode_float32(16.0,'abcd')", encode_float32(16.0, "abcd"), [0x4180, 0x0000])
    check("encode_float32(16.0,'dcba')", encode_float32(16.0, "dcba"), [0x0000, 0x8041])
    check("encode_float32(16.0,'dcba') wire bytes",
          regs_to_bytes(encode_float32(16.0, "dcba")), b"\x00\x00\x80\x41")
    check("encode_float32(16.0,'badc')", encode_float32(16.0, "badc"), [0x8041, 0x0000])
    check("encode_float32(16.0,'cdab')", encode_float32(16.0, "cdab"), [0x0000, 0x4180])
    check("decode_float32([0x0000,0x8041],'dcba')", decode_float32([0x0000, 0x8041], "dcba"), 16.0)
    check("decode_float32([0x4180,0x0000],'abcd')", decode_float32([0x4180, 0x0000], "abcd"), 16.0)
    for order in ORDERS:
        for value in (0.0, 6.0, 16.0, 32.0, 10.5, 230.25):
            check("round-trip f32 %-7g order %s" % (value, order),
                  decode_float32(encode_float32(value, order), order), value)
    print("  cross-order sanity: dcba-encoded 16.0 read as abcd -> %r"
          % decode_float32(encode_float32(16.0, "dcba"), "abcd"))

    # UINT32 date 20260910 == 0x0135282E -> canonical bytes 01 35 28 2E
    check("encode_uint32(20260910,'abcd')", encode_uint32(20260910, "abcd"), [0x0135, 0x282E])
    check("encode_uint32(20260910,'dcba')", encode_uint32(20260910, "dcba"), [0x2E28, 0x3501])
    check("decode_uint32 abcd 20260910", decode_uint32([0x0135, 0x282E], "abcd"), 20260910)
    check("decode_uint32 dcba 20260910", decode_uint32([0x2E28, 0x3501], "dcba"), 20260910)
    check("decode_uint32 badc 20260910", decode_uint32([0x3501, 0x2E28], "badc"), 20260910)
    check("decode_uint32 cdab 20260910", decode_uint32([0x282E, 0x0135], "cdab"), 20260910)
    check("fmt_date(20260910)", fmt_date(20260910), "2026-09-10")
    check("date_plausible(20260910)", date_plausible(20260910), True)
    check("date_plausible(3329426689)", date_plausible(3329426689), False)
    check("fmt_time(155630)", fmt_time(155630), "15:56:30")
    check("time_plausible(155630)", time_plausible(155630), True)
    for order in ORDERS:
        check("round-trip u32 20260910 order %s" % order,
              decode_uint32(encode_uint32(20260910, order), order), 20260910)
        check("round-trip u32 300 order %s" % order,
              decode_uint32(encode_uint32(300, order), order), 300)

    # UINT64 lifetime energy
    want64 = 0x0000000102030405
    check("decode_uint64 abcd", decode_uint64([0x0000, 0x0001, 0x0203, 0x0405], "abcd"), want64)
    check("decode_uint64 dcba", decode_uint64([0x0504, 0x0302, 0x0100, 0x0000], "dcba"), want64)
    check("decode_uint64 badc", decode_uint64([0x0000, 0x0100, 0x0302, 0x0504], "badc"), want64)
    check("decode_uint64 cdab", decode_uint64([0x0405, 0x0203, 0x0001, 0x0000], "cdab"), want64)
    check("decode_uint64 12345678 Wh abcd",
          decode_uint64(bytes_to_regs(struct.pack(">Q", 12345678)), "abcd"), 12345678)
    check("decode_uint64 12345678 Wh dcba",
          decode_uint64(bytes_to_regs(struct.pack("<Q", 12345678)), "dcba"), 12345678)

    # strings / misc
    check("decode_string 'NexBlue'",
          decode_string([0x4E65, 0x7842, 0x6C75, 0x6500, 0, 0, 0, 0]), "NexBlue")
    check("decode_string_swapped 'NexBlue'",
          decode_string_swapped([0x654E, 0x4278, 0x756C, 0x0065, 0, 0, 0, 0]), "NexBlue")
    check("signed16(0xFF88) timezone", signed16(0xFF88), -120)
    check("signed16(120) timezone", signed16(120), 120)
    check("permute involution dcba",
          permute(permute(b"\x01\x02\x03\x04", "dcba"), "dcba"), b"\x01\x02\x03\x04")
    check("permute involution cdab",
          permute(permute(b"\x01\x02\x03\x04", "cdab"), "cdab"), b"\x01\x02\x03\x04")
    check("permute involution badc",
          permute(permute(b"\x01\x02\x03\x04", "badc"), "badc"), b"\x01\x02\x03\x04")

    print("\n%d failure(s)%s" % (len(failures), (": " + ", ".join(failures)) if failures else ""))
    return 1 if failures else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Global options repeated on every subcommand so they can appear either
    # before or after the subcommand name (SUPPRESS keeps the main-parser
    # value when the option is not repeated).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=argparse.SUPPRESS)
    common.add_argument("--unit", type=int, default=argparse.SUPPRESS)
    common.add_argument("--order", choices=ORDERS, default=argparse.SUPPRESS)
    common.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                        help="actually perform the write (otherwise only the plan is printed)")

    parser = argparse.ArgumentParser(
        prog="nexblue_modbus.py",
        description="Modbus TCP probe for the NexBlue Edge 2 wallbox (port 502, default "
                    "unit id 200). Requires 'Modbus TCP' enabled in the NexBlue app and "
                    "firmware >= 03.09.88.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  nexblue_modbus.py 192.0.2.10\n"
               "  nexblue_modbus.py 192.0.2.10 read --order abcd\n"
               "  nexblue_modbus.py 192.0.2.10 raw --type input --start 0 --count 48\n"
               "  nexblue_modbus.py 192.0.2.10 write-current 10 --yes\n"
               "  nexblue_modbus.py selftest\n",
    )
    parser.add_argument("host", nargs="?", help="charger IP or hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default 502)")
    parser.add_argument("--unit", type=int, default=DEFAULT_UNIT,
                        help="Modbus unit id (default 200; 200/1/255 are tried on failure)")
    parser.add_argument("--order", choices=ORDERS, default="dcba",
                        help="multi-register byte/word order (default dcba = full "
                             "little-endian, as the vendor doc claims)")
    parser.add_argument("--yes", action="store_true", help="confirm write commands")

    sub = parser.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("read", parents=[common], help="full labelled dump (default command)")

    p_raw = sub.add_parser("raw", parents=[common], help="raw register dump")
    p_raw.add_argument("--type", choices=("input", "holding"), default="input")
    p_raw.add_argument("--start", type=int, required=True)
    p_raw.add_argument("--count", type=int, required=True)

    p_cur = sub.add_parser("write-current", parents=[common],
                           help="write Current Limit FLOAT32 to 1000-1001 (0-32 A)")
    p_cur.add_argument("value", type=float)

    sub.add_parser("selftest", parents=[common],
                   help="unit-test the decoding helpers offline (no host needed)")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd or "read"

    if cmd == "selftest":
        return cmd_selftest(None, args)

    if not args.host:
        parser.error("the host argument is required for the %r command" % cmd)
    if not 1 <= args.unit <= 255:
        parser.error("--unit must be 1-255 (NexBlue allows 1-247; 255 is only a fallback)")

    handlers = {
        "read": cmd_read,
        "raw": cmd_raw,
        "write-current": cmd_write_current,
    }

    # Range checks happen before any network access.
    limits = {
        "write-current": (0.0, 32.0, "Current Limit", "A"),
    }
    if cmd in limits:
        low, high, name, unit = limits[cmd]
        if not low <= args.value <= high:
            print("refusing: %s must be %g-%g %s (got %g)" % (name, low, high, unit, args.value))
            return 2

    # A write dry run (no --yes) only prints its plan, so it needs no connection.
    dry_run = cmd not in ("read", "raw") and not args.yes

    client = None
    try:
        if not dry_run:
            client = connect(args)
        return handlers[cmd](client, args)
    except ProbeError as exc:
        print("ERROR: %s" % exc)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:  # noqa: BLE001
        print("UNEXPECTED ERROR %s: %s" % (type(exc).__name__, exc))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
