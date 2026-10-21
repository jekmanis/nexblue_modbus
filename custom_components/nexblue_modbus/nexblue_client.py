"""NexBlue Modbus TCP client and register decoding.

Pure Python - no Home Assistant imports - so it is unit-tested on its own and
shared with tools/smoke_client.py.

Register map: https://nexblue.com/en-no/pages/nexblue-modbus-technical-doc,
verified on an Edge 2 (NB1620B, FW 03.09.97), see FINDINGS.md:

* input 0-51      identification (strings big-endian, read once)
* input 100-138   status / metering (139 does not exist -> exception 2)
* input 400-402   error / warning / waiting bitsets
* holding 1000-1007  current limit, fallback limit/timeout, IF_START, phase mode

Single-register values are big-endian. Multi-register values are "DCBA": little-endian
bytes inside each register and little-endian word order (confirmed with the Date
register 38-39).

Only one TCP connection is ever open and every request goes through one lock: the
charger (an ESP32) serves one Modbus TCP client at a time and resets any second
connection. The Current Limit keepalive and the IF_START write use this same connection. After a transport error the socket is
closed and the next call reconnects; pymodbus' own reconnect loop is disabled so
nothing retries in the background.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import struct
import time
from dataclasses import dataclass

from pymodbus.client import AsyncModbusTcpClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 200
DEFAULT_TIMEOUT = 5.0

IDENT_START, IDENT_COUNT = 0, 52
STATUS_START, STATUS_COUNT = 100, 39
ALARM_START, ALARM_COUNT = 400, 3
HOLDING_START, HOLDING_COUNT = 1000, 8

REG_CURRENT_LIMIT = 1000
CURRENT_LIMIT_MIN = 0.0
CURRENT_LIMIT_MAX = 32.0

# IF_START, holding 1006, UINT16: 0 STOP, 1 START. Has no effect while the charger is
# controlled by OCPP. Only START is ever written.
REG_IF_START = 1006
IF_START_START = 1

STATE_NAMES = {
    0: "available",
    1: "preparing",
    2: "charging",
    3: "suspended_evse",
    4: "suspended_ev",
    5: "finishing",
    6: "reserved",
    7: "unavailable",
    8: "faulted",
}

STATE_PREPARING = 1

# Charger states in which a car is plugged in (OCPP-style connector states).
CAR_CONNECTED_STATES = frozenset({"preparing", "charging", "suspended_evse", "suspended_ev",
                                  "finishing"})
CAR_DISCONNECTED_STATES = frozenset({"available", "reserved", "unavailable", "faulted"})

PHASE_MODE_NAMES = {
    0: "adaptive",
    1: "single_phase",
    2: "three_phase",
}

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


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def _dcba_bytes(regs) -> bytes:
    """Wire registers -> canonical big-endian bytes (full byte reversal)."""
    return b"".join(struct.pack(">H", int(r) & 0xFFFF) for r in regs)[::-1]


def decode_float32(regs) -> float:
    return struct.unpack(">f", _dcba_bytes(regs[:2]))[0]


def decode_uint32(regs) -> int:
    return struct.unpack(">I", _dcba_bytes(regs[:2]))[0]


def decode_uint64(regs) -> int:
    return struct.unpack(">Q", _dcba_bytes(regs[:4]))[0]


def encode_float32(value: float) -> list[int]:
    data = struct.pack(">f", float(value))[::-1]
    return [struct.unpack(">H", data[i:i + 2])[0] for i in (0, 2)]


def decode_string(regs) -> str:
    """STRING(n): two bytes per register, high byte first, NUL-terminated."""
    raw = b"".join(struct.pack(">H", int(r) & 0xFFFF) for r in regs)
    return raw.split(b"\x00")[0].decode("utf-8", errors="replace").strip()


def active_bits(value: int, labels: dict[int, str]) -> list[str]:
    """Labels of the set bits, undocumented bits reported by number."""
    return [labels.get(bit, f"bit {bit}") for bit in range(16) if value & (1 << bit)]


FALLBACK_TIMEOUT_DEFAULT = 300   # s, holding 1004-1005 (documented range 30-1800)
KEEPALIVE_MIN = 10.0             # s, never faster than the minimum scan interval
KEEPALIVE_MAX = 120.0


def keepalive_interval(fallback_timeout: int) -> float:
    """Seconds between Current Limit re-writes: a third of the fallback timeout, 10-120 s.

    The floor is 10 s rather than 30 s so that the shortest allowed timeout (30 s) is
    still beaten. An out-of-range timeout reading falls back to the 300 s default.
    """
    timeout = fallback_timeout if 30 <= fallback_timeout <= 1800 else FALLBACK_TIMEOUT_DEFAULT
    return min(max(timeout / 3, KEEPALIVE_MIN), KEEPALIVE_MAX)


class CurrentLimitKeepalive:
    """Remembers the limit HA set, and when it is due to be written again.

    Reads don't count as the charger's Modbus heartbeat: without a write within the
    fallback timeout the charger drops to its fallback limit (Waiting bit 6). So once HA
    has set a limit, the same value is re-written before the timeout runs out. Nothing
    is written until HA has set a limit itself.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.setpoint: float | None = None
        self._last_write: float | None = None

    def wrote(self, amps: float) -> None:
        self.setpoint = float(amps)
        self._last_write = self._clock()

    def due(self, fallback_timeout: int, poll_interval: float = 0.0) -> bool:
        """True if waiting for the next poll would leave the interval exceeded."""
        if self.setpoint is None or self._last_write is None:
            return False
        elapsed = self._clock() - self._last_write
        return elapsed + poll_interval >= keepalive_interval(fallback_timeout)


class MonotonicCounterGuard:
    """Holds a lifetime energy counter steady through false readings.

    Home Assistant reads any drop of a ``total_increasing`` sensor as a meter reset, so
    one transient 0 (or lower) reading makes the statistics count the whole climb back
    a second time. A reading of 0 or below the last good value is ignored: the last good
    value is returned instead, or None (unknown) when there is none yet.
    """

    def __init__(self, last: float | None = None) -> None:
        self.last = last

    def filter(self, value: float | None) -> float | None:
        if value is None or value <= 0 or (self.last is not None and value < self.last):
            if value is not None and self.last is not None:
                _LOGGER.debug("ignoring counter reading %s below last good %s", value, self.last)
            return self.last
        self.last = value
        return value


class ConfirmedDropGuard:
    """Session energy: rises pass at once, a drop only once it has persisted.

    The session counter drops to 0 when a new session starts, so a lower reading can't
    simply be rejected. But one false 0 mid-session would look like a meter reset to Home
    Assistant's ``total_increasing`` statistics and double-count the session. So a reading
    below the last accepted value is accepted only after ``confirm_polls`` consecutive
    polls have all been below it; until then the last accepted value is returned.

    ``filter`` must be called exactly once per poll.
    """

    def __init__(self, last: float | None = None, confirm_polls: int = 3) -> None:
        self.last = last
        self._confirm = confirm_polls
        self._low_polls = 0

    def filter(self, value: float | None) -> float | None:
        if value is None:
            return self.last
        if self.last is None or value >= self.last:
            self.last = value
            self._low_polls = 0
            return value
        self._low_polls += 1
        if self._low_polls >= self._confirm:
            _LOGGER.debug("counter drop %s -> %s confirmed after %d polls",
                          self.last, value, self._low_polls)
            self.last = value
            self._low_polls = 0
            return value
        return self.last


@dataclass(frozen=True)
class DeviceInfo:
    brand: str
    model: str
    serial: str
    firmware: str
    hardware: str
    mapping_version: int
    connector_count: int


@dataclass(frozen=True)
class ChargerData:
    state: int
    current_l1: float
    current_l2: float
    current_l3: float
    voltage_l1: float
    voltage_l2: float
    voltage_l3: float
    power_l1: float
    power_l2: float
    power_l3: float
    power_total: float
    session_energy: float      # kWh
    lifetime_energy: int       # Wh
    fallback_current: float    # A, set in the app/cloud
    max_current: float         # A, set in the app/cloud
    phase_mode: int
    error_bits: int
    warning_bits: int
    waiting_bits: int
    current_limit: float       # A, holding 1000-1001
    fallback_limit: float      # A, holding 1002-1003 (-1 = not set)
    fallback_timeout: int      # s, holding 1004-1005
    if_start: int
    runtime_phase_mode: int

    @property
    def state_name(self) -> str | None:
        return STATE_NAMES.get(self.state)

    @property
    def phase_mode_name(self) -> str | None:
        return PHASE_MODE_NAMES.get(self.phase_mode)


def parse_identity(regs) -> DeviceInfo:
    if len(regs) < IDENT_COUNT:
        raise ValueError(f"identification block needs {IDENT_COUNT} registers, got {len(regs)}")
    return DeviceInfo(
        brand=decode_string(regs[0:8]),
        model=decode_string(regs[8:16]),
        serial=decode_string(regs[16:28]),
        firmware=decode_string(regs[28:36]),
        hardware=decode_string(regs[44:52]),
        mapping_version=regs[36],
        connector_count=regs[37],
    )


def parse_data(status, alarms, holding) -> ChargerData:
    for name, regs, count in (("status", status, STATUS_COUNT),
                              ("alarm", alarms, ALARM_COUNT),
                              ("holding", holding, HOLDING_COUNT)):
        if len(regs) < count:
            raise ValueError(f"{name} block needs {count} registers, got {len(regs)}")

    def f32(addr: int) -> float:
        off = addr - STATUS_START
        return decode_float32(status[off:off + 2])

    return ChargerData(
        state=status[0],
        current_l1=f32(102),
        current_l2=f32(104),
        current_l3=f32(106),
        voltage_l1=f32(108),
        voltage_l2=f32(110),
        voltage_l3=f32(112),
        power_l1=f32(120),
        power_l2=f32(122),
        power_l3=f32(124),
        power_total=f32(126),
        session_energy=f32(128),
        lifetime_energy=decode_uint64(status[30:34]),
        fallback_current=f32(134),
        max_current=f32(136),
        phase_mode=status[38],
        error_bits=alarms[0],
        warning_bits=alarms[1],
        waiting_bits=alarms[2],
        current_limit=decode_float32(holding[0:2]),
        fallback_limit=decode_float32(holding[2:4]),
        fallback_timeout=decode_uint32(holding[4:6]),
        if_start=holding[6],
        runtime_phase_mode=holding[7],
    )


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class NexBlueError(Exception):
    """The charger answered with a Modbus error, or the request was invalid."""


class NexBlueConnectionError(NexBlueError):
    """No usable TCP connection to the charger."""


class NexBlueStateError(NexBlueError):
    """The charger is not in a state that allows the request; nothing was written."""


def _unit_keyword(method) -> str:
    """Name of the unit-id keyword: device_id (pymodbus >= 3.9), slave, or unit."""
    params = inspect.signature(method).parameters
    for name in ("device_id", "slave", "unit"):
        if name in params:
            return name
    raise NexBlueError("unsupported pymodbus version: no unit-id keyword found")


class NexBlueClient:
    """One Modbus TCP connection to a NexBlue charger, serialised by a lock."""

    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 unit_id: int = DEFAULT_UNIT_ID, timeout: float = DEFAULT_TIMEOUT,
                 client_factory=AsyncModbusTcpClient) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self._timeout = timeout
        self._factory = client_factory
        self._client = None
        self._unit_kw: str | None = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        if self._client is not None and self._client.connected:
            return self._client
        await self._drop()
        # reconnect_delay=0 turns off pymodbus' background reconnect loop; the
        # next poll reconnects instead, so a dead charger costs one attempt per poll.
        client = self._factory(self.host, port=self.port, timeout=self._timeout,
                               retries=1, reconnect_delay=0)
        if not await client.connect():
            client.close()
            raise NexBlueConnectionError(
                f"cannot connect to {self.host}:{self.port} - is 'Modbus TCP' enabled "
                "in the NexBlue app?")
        if self._unit_kw is None:
            self._unit_kw = _unit_keyword(client.read_input_registers)
        self._client = client
        return client

    async def _drop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _call(self, method_name: str, address: int, **kwargs):
        """One request. Transport errors drop the socket; Modbus errors keep it."""
        client = await self._connect()
        kwargs[self._unit_kw] = self.unit_id
        try:
            response = await getattr(client, method_name)(address, **kwargs)
        except Exception as exc:  # noqa: BLE001 - any transport failure means reconnect
            await self._drop()
            raise NexBlueConnectionError(
                f"{method_name}({address}) failed: {type(exc).__name__}: {exc}") from exc
        if response.isError():
            raise NexBlueError(f"{method_name}({address}) refused by the charger: {response}")
        return response

    async def _read(self, kind: str, start: int, count: int) -> list[int]:
        method = "read_input_registers" if kind == "input" else "read_holding_registers"
        response = await self._call(method, start, count=count)
        registers = list(response.registers)
        if len(registers) != count:
            await self._drop()
            raise NexBlueConnectionError(
                f"short read at {kind} {start}: {len(registers)} of {count} registers")
        return registers

    async def read_raw(self) -> dict[str, list[int]]:
        """All four register blocks, raw, in one locked pass."""
        async with self._lock:
            return {
                "identity": await self._read("input", IDENT_START, IDENT_COUNT),
                "status": await self._read("input", STATUS_START, STATUS_COUNT),
                "alarms": await self._read("input", ALARM_START, ALARM_COUNT),
                "holding": await self._read("holding", HOLDING_START, HOLDING_COUNT),
            }

    async def read_device_info(self) -> DeviceInfo:
        async with self._lock:
            return parse_identity(await self._read("input", IDENT_START, IDENT_COUNT))

    async def read_data(self) -> ChargerData:
        async with self._lock:
            status = await self._read("input", STATUS_START, STATUS_COUNT)
            alarms = await self._read("input", ALARM_START, ALARM_COUNT)
            holding = await self._read("holding", HOLDING_START, HOLDING_COUNT)
        return parse_data(status, alarms, holding)

    async def poll(self, keepalive: CurrentLimitKeepalive | None = None,
                   poll_interval: float = 0.0) -> ChargerData:
        """read_data(), plus the Current Limit keepalive write when it is due."""
        data = await self.read_data()
        if keepalive is not None and keepalive.due(data.fallback_timeout, poll_interval):
            amps = keepalive.setpoint
            readback = await self.set_current_limit(amps)
            keepalive.wrote(amps)
            _LOGGER.debug("keepalive: current limit %.1f A re-written", amps)
            data = dataclasses.replace(data, current_limit=readback)
        return data

    async def set_current_limit(self, amps: float) -> float:
        """Write Current Limit (holding 1000-1001, FLOAT32) and return the read-back value.

        Together with start_charging() (1006), the only registers the client writes.
        """
        amps = float(amps)
        if not CURRENT_LIMIT_MIN <= amps <= CURRENT_LIMIT_MAX:
            raise NexBlueError(
                f"current limit must be {CURRENT_LIMIT_MIN:g}-{CURRENT_LIMIT_MAX:g} A, got {amps:g}")
        async with self._lock:
            await self._call("write_registers", REG_CURRENT_LIMIT, values=encode_float32(amps))
            readback = decode_float32(await self._read("holding", REG_CURRENT_LIMIT, 2))
        _LOGGER.debug("current limit set to %.3f A (requested %.3f A)", readback, amps)
        return readback

    async def start_charging(self) -> int:
        """Write IF_START = 1 (START, holding 1006) and return the read-back value.

        Only allowed while the charger is ``preparing`` (car plugged in, not charging).
        The state is read again under the lock just before the write; in any other state
        NexBlueStateError is raised and nothing is written.
        """
        async with self._lock:
            state = (await self._read("input", STATUS_START, 1))[0]
            if state != STATE_PREPARING:
                raise NexBlueStateError(
                    f"charger state is {STATE_NAMES.get(state, state)}, start needs preparing")
            await self._call("write_registers", REG_IF_START, values=[IF_START_START])
            readback = (await self._read("holding", REG_IF_START, 1))[0]
        _LOGGER.debug("IF_START written (START), read back %s", readback)
        return readback
