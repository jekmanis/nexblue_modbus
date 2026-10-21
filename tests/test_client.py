"""nexblue_client: decoding against a real register dump, and the connection rules."""

from __future__ import annotations

import asyncio
import struct

import pytest

import nexblue_client as nb


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def test_dcba_float32_known_wire_values():
    # Wire values seen on the charger (FINDINGS.md).
    assert nb.decode_float32([0x0000, 0x8041]) == 16.0
    assert nb.decode_float32([0x0000, 0x2041]) == 10.0
    assert nb.decode_float32([0x0000, 0x80BF]) == -1.0
    assert nb.encode_float32(10.0) == [0x0000, 0x2041]
    assert nb.encode_float32(16.0) == [0x0000, 0x8041]


def test_dcba_uint32_date_register():
    # Date 38-39 is the vendor's own byte-order check: 20261021 under DCBA.
    assert nb.decode_uint32([0x9D28, 0x3501]) == 20261021


def test_dcba_uint64_lifetime_energy():
    assert nb.decode_uint64([0x4FE2, 0x0F00, 0x0000, 0x0000]) == 1_040_975
    # Upper words matter: a value above 2**32 must not be truncated.
    wire = struct.pack("<Q", 5_000_000_000)
    regs = [struct.unpack(">H", wire[i:i + 2])[0] for i in range(0, 8, 2)]
    assert nb.decode_uint64(regs) == 5_000_000_000


@pytest.mark.parametrize("value", [0.0, 6.0, 10.5, 16.0, 32.0, 230.25, -1.0])
def test_float32_round_trip(value):
    assert nb.decode_float32(nb.encode_float32(value)) == value


def test_active_bits_labels_and_undocumented():
    assert nb.active_bits(0, nb.ERROR_BITS) == []
    assert nb.active_bits(0x0040, nb.WAITING_BITS) == ["Fallback triggered by Modbus"]
    assert nb.active_bits(0x8001, nb.ERROR_BITS) == ["CP voltage abnormal", "bit 15"]


def test_parse_identity_fixture(idle_dump):
    info = nb.parse_identity(idle_dump["identity"])
    assert info == nb.DeviceInfo(
        brand="NexBlue", model="NB1620B", serial="E2TEST0001", firmware="03.09.97",
        hardware="02.05.01", mapping_version=0x0100, connector_count=1,
    )


def test_parse_data_fixture(idle_dump):
    data = nb.parse_data(idle_dump["status"], idle_dump["alarms"], idle_dump["holding"])
    assert data.state == 0 and data.state_name == "available"
    assert data.current_l1 == pytest.approx(0.042, abs=1e-3)
    assert data.voltage_l1 == pytest.approx(232.674, abs=1e-3)
    assert data.voltage_l2 == pytest.approx(232.378, abs=1e-3)
    assert data.voltage_l3 == pytest.approx(232.955, abs=1e-3)
    assert data.power_total == 0.0
    assert data.session_energy == pytest.approx(18.034, abs=1e-3)
    assert data.lifetime_energy == 1_040_975
    assert data.fallback_current == 6.0
    assert data.max_current == 20.0
    assert data.phase_mode == 0 and data.phase_mode_name == "adaptive"
    assert (data.error_bits, data.warning_bits, data.waiting_bits) == (0, 0, 0x0040)
    assert data.current_limit == 16.0
    assert data.fallback_limit == -1.0
    assert data.fallback_timeout == 300
    assert (data.if_start, data.runtime_phase_mode) == (0, 0)


def test_parse_data_charging_values():
    # The fixture is idle (all power 0), so check the power/current offsets with
    # distinct synthetic values: a swapped offset would show up as a wrong field.
    status = [0] * nb.STATUS_COUNT
    status[0] = 2
    values = {102: 10.1, 104: 10.2, 106: 10.3, 108: 231.0, 110: 232.0, 112: 233.0,
              120: 2300.0, 122: 2310.0, 124: 2320.0, 126: 6930.0, 128: 4.5,
              134: 6.0, 136: 20.0}
    for addr, value in values.items():
        off = addr - nb.STATUS_START
        status[off:off + 2] = nb.encode_float32(value)
    status[38] = 2
    data = nb.parse_data(status, [0, 0, 0], [0] * nb.HOLDING_COUNT)
    assert data.state_name == "charging"
    assert (data.current_l1, data.current_l2, data.current_l3) == pytest.approx((10.1, 10.2, 10.3))
    assert (data.voltage_l1, data.voltage_l2, data.voltage_l3) == (231.0, 232.0, 233.0)
    assert (data.power_l1, data.power_l2, data.power_l3, data.power_total) == (
        2300.0, 2310.0, 2320.0, 6930.0)
    assert data.session_energy == 4.5
    assert data.phase_mode_name == "three_phase"


def test_parse_rejects_short_blocks(idle_dump):
    with pytest.raises(ValueError):
        nb.parse_identity(idle_dump["identity"][:40])
    with pytest.raises(ValueError):
        nb.parse_data(idle_dump["status"][:38], idle_dump["alarms"], idle_dump["holding"])


def test_unknown_state_has_no_name():
    status = [0] * nb.STATUS_COUNT
    status[0] = 42
    assert nb.parse_data(status, [0, 0, 0], [0] * nb.HOLDING_COUNT).state_name is None


# --------------------------------------------------------------------------
# Client behaviour, against a fake pymodbus client
# --------------------------------------------------------------------------

class _Response:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class FakeModbus:
    """Stands in for AsyncModbusTcpClient (pymodbus 3.13 signatures)."""

    instances: list["FakeModbus"] = []

    def __init__(self, host, *, port=502, timeout=3, retries=3, reconnect_delay=0.1):
        self.kwargs = dict(host=host, port=port, timeout=timeout, retries=retries,
                           reconnect_delay=reconnect_delay)
        self.connected = False
        self.closed = False
        self.dump: dict = {}
        self.writes: list = []
        self.fail_next: Exception | None = None
        self.refuse_next = False
        self.connect_ok = True
        FakeModbus.instances.append(self)

    async def connect(self):
        self.connected = self.connect_ok
        return self.connected

    def close(self):
        self.connected = False
        self.closed = True

    def _maybe_fail(self):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        if self.refuse_next:
            self.refuse_next = False
            return _Response(error=True)
        return None

    async def read_input_registers(self, address, *, count=1, device_id=1, no_response_expected=False):
        assert device_id == 200
        return self._maybe_fail() or _Response(self._block("input", address, count))

    async def read_holding_registers(self, address, *, count=1, device_id=1, no_response_expected=False):
        assert device_id == 200
        return self._maybe_fail() or _Response(self._block("holding", address, count))

    async def write_registers(self, address, values, *, device_id=1, no_response_expected=False):
        assert device_id == 200
        failed = self._maybe_fail()
        if failed:
            return failed
        self.writes.append((address, list(values)))
        holding = self.dump["holding"]
        holding[address - 1000:address - 1000 + len(values)] = values
        return _Response()

    def _block(self, kind, address, count):
        if kind == "holding":
            return self.dump["holding"][address - 1000:address - 1000 + count]
        for name, start in (("identity", 0), ("status", 100), ("alarms", 400)):
            block = self.dump[name]
            if start <= address < start + len(block):
                return block[address - start:address - start + count]
        raise AssertionError(f"unexpected read at input {address}")


@pytest.fixture
def fake(idle_dump):
    FakeModbus.instances = []

    def factory(host, **kwargs):
        client = FakeModbus(host, **kwargs)
        client.dump = {k: list(v) for k, v in idle_dump.items()}
        return client

    return factory


def run(coro):
    return asyncio.run(coro)


def test_one_connection_for_many_reads(fake):
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)

    async def scenario():
        info = await client.read_device_info()
        for _ in range(3):
            data = await client.read_data()
        await client.close()
        return info, data

    info, data = run(scenario())
    assert info.serial == "E2TEST0001"
    assert data.current_limit == 16.0
    assert len(FakeModbus.instances) == 1
    # pymodbus' background reconnect loop must be off; we reconnect on the next poll.
    assert FakeModbus.instances[0].kwargs["reconnect_delay"] == 0
    assert FakeModbus.instances[0].closed


def test_transport_error_drops_socket_and_next_call_reconnects(fake):
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)

    async def scenario():
        await client.read_data()
        FakeModbus.instances[0].fail_next = ConnectionResetError("reset by peer")
        with pytest.raises(nb.NexBlueConnectionError):
            await client.read_data()
        assert FakeModbus.instances[0].closed
        return await client.read_data()

    data = run(scenario())
    assert data.lifetime_energy == 1_040_975
    assert len(FakeModbus.instances) == 2
    assert FakeModbus.instances[0].closed and not FakeModbus.instances[1].closed


def test_modbus_error_keeps_socket(fake):
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)

    async def scenario():
        await client.read_data()
        FakeModbus.instances[0].refuse_next = True
        with pytest.raises(nb.NexBlueError) as excinfo:
            await client.read_data()
        assert not isinstance(excinfo.value, nb.NexBlueConnectionError)
        await client.read_data()

    run(scenario())
    assert len(FakeModbus.instances) == 1


def test_connect_failure_raises_connection_error(fake):
    def refusing(host, **kwargs):
        client = fake(host, **kwargs)
        client.connect_ok = False
        return client

    client = nb.NexBlueClient("192.0.2.1", client_factory=refusing)
    with pytest.raises(nb.NexBlueConnectionError):
        run(client.read_data())
    assert FakeModbus.instances[0].closed


def test_set_current_limit_writes_only_1000_and_reads_back(fake):
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)
    assert run(client.set_current_limit(10)) == 10.0
    assert FakeModbus.instances[0].writes == [(1000, [0x0000, 0x2041])]


@pytest.mark.parametrize("amps", [-0.5, 32.5, 100])
def test_set_current_limit_rejects_out_of_range_without_io(fake, amps):
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)
    with pytest.raises(nb.NexBlueError):
        run(client.set_current_limit(amps))
    assert FakeModbus.instances == []


def _in_state(fake, state):
    """A fake charger whose input 100 (State) reads ``state``."""
    def factory(host, **kwargs):
        client = fake(host, **kwargs)
        client.dump["status"][0] = state
        return client
    return factory


def test_start_charging_writes_start_to_1006_only(fake):
    client = nb.NexBlueClient("192.0.2.1", client_factory=_in_state(fake, 1))  # preparing
    assert run(client.start_charging()) == 1
    assert FakeModbus.instances[0].writes == [(1006, [1])]


@pytest.mark.parametrize("state", sorted(set(nb.STATE_NAMES) - {nb.STATE_PREPARING}))
def test_start_charging_refused_unless_preparing(fake, state):
    client = nb.NexBlueClient("192.0.2.1", client_factory=_in_state(fake, state))
    with pytest.raises(nb.NexBlueStateError):
        run(client.start_charging())
    assert FakeModbus.instances[0].writes == []


def test_car_connected_states_cover_every_state():
    names = set(nb.STATE_NAMES.values())
    assert nb.CAR_CONNECTED_STATES | nb.CAR_DISCONNECTED_STATES == names
    assert not nb.CAR_CONNECTED_STATES & nb.CAR_DISCONNECTED_STATES
    assert nb.STATE_NAMES[nb.STATE_PREPARING] == "preparing"


def test_requests_are_serialised(fake):
    """A write issued during a poll must not interleave with the poll's reads."""
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)
    order = []

    async def scenario():
        await client.read_device_info()
        modbus = FakeModbus.instances[0]
        real_read = modbus.read_input_registers

        async def slow_read(address, **kwargs):
            order.append(("read", address))
            await asyncio.sleep(0.01)
            return await real_read(address, **kwargs)

        real_write = modbus.write_registers

        async def logged_write(address, values, **kwargs):
            order.append(("write", address))
            return await real_write(address, values, **kwargs)

        modbus.read_input_registers = slow_read
        modbus.write_registers = logged_write
        await asyncio.gather(client.read_data(), client.set_current_limit(12))

    run(scenario())
    # Both status reads of the poll happen before the write's turn.
    assert order == [("read", 100), ("read", 400), ("write", 1000)]
    assert FakeModbus.instances[0].writes == [(1000, nb.encode_float32(12))]


@pytest.mark.parametrize("keyword", ["device_id", "slave", "unit"])
def test_unit_keyword_detection(keyword):
    namespace: dict = {}
    exec(f"async def method(address, *, count=1, {keyword}=1): pass", namespace)
    assert nb._unit_keyword(namespace["method"]) == keyword


def test_unit_keyword_matches_installed_pymodbus():
    from pymodbus.client import AsyncModbusTcpClient

    assert nb._unit_keyword(AsyncModbusTcpClient.read_input_registers) == "device_id"
    assert nb._unit_keyword(AsyncModbusTcpClient.write_registers) == "device_id"


# --------------------------------------------------------------------------
# Lifetime counter guard
# --------------------------------------------------------------------------

def test_counter_guard_holds_through_false_zero_and_drops():
    guard = nb.MonotonicCounterGuard()
    assert guard.filter(0) is None            # nothing good yet -> unknown, not 0
    assert guard.filter(1_040_975) == 1_040_975
    assert guard.filter(0) == 1_040_975       # transient false zero
    assert guard.filter(1_040_000) == 1_040_975  # lower reading
    assert guard.filter(None) == 1_040_975
    assert guard.filter(1_041_200) == 1_041_200  # genuine increase passes
    assert guard.filter(1_041_200) == 1_041_200  # repeated reads are stable


def test_counter_guard_seeded_from_restored_state():
    guard = nb.MonotonicCounterGuard(last=1_040_975.0)
    assert guard.filter(0) == 1_040_975.0     # first poll after restart reads 0
    assert guard.filter(1_040_975) == 1_040_975


# --------------------------------------------------------------------------
# Current Limit keepalive
# --------------------------------------------------------------------------

@pytest.mark.parametrize("timeout, interval", [
    (300, 100.0),   # charger default
    (1800, 120.0),  # capped
    (90, 30.0),
    (30, 10.0),     # shortest allowed timeout is still beaten
    (0, 100.0),     # out of range -> 300 s default
    (65535, 100.0),
])
def test_keepalive_interval(timeout, interval):
    assert nb.keepalive_interval(timeout) == interval
    assert nb.keepalive_interval(timeout) < (timeout if 30 <= timeout <= 1800 else 300)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_keepalive_never_due_before_ha_sets_a_limit():
    clock = FakeClock()
    keepalive = nb.CurrentLimitKeepalive(clock)
    clock.now += 10_000
    assert not keepalive.due(300, 10)


def test_keepalive_due_before_the_interval_runs_out():
    clock = FakeClock()
    keepalive = nb.CurrentLimitKeepalive(clock)
    keepalive.wrote(12)
    clock.now += 85
    assert not keepalive.due(300, 10)   # next poll at 95 s is still inside 100 s
    clock.now += 5
    assert keepalive.due(300, 10)       # next poll would be at 100 s -> write now
    keepalive.wrote(12)
    assert not keepalive.due(300, 10)


def test_poll_writes_setpoint_only_when_due(fake):
    clock = FakeClock()
    keepalive = nb.CurrentLimitKeepalive(clock)
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)

    async def scenario():
        await client.poll(keepalive, 10)                # no limit set yet -> read only
        assert FakeModbus.instances[0].writes == []
        await client.set_current_limit(12)
        keepalive.wrote(12)
        clock.now += 50
        await client.poll(keepalive, 10)                # not due
        clock.now += 45
        data = await client.poll(keepalive, 10)         # due: 95 + 10 >= 100
        return data

    data = run(scenario())
    writes = FakeModbus.instances[0].writes
    assert writes == [(1000, nb.encode_float32(12))] * 2   # the HA write + one keepalive
    assert data.current_limit == 12.0
    assert len(FakeModbus.instances) == 1                    # same single connection


def test_keepalive_restores_setpoint_after_charger_reset(fake):
    """A charger reboot puts 16 A back in 1000; the keepalive writes HA's value again."""
    clock = FakeClock()
    keepalive = nb.CurrentLimitKeepalive(clock)
    client = nb.NexBlueClient("192.0.2.1", client_factory=fake)

    async def scenario():
        await client.set_current_limit(10)
        keepalive.wrote(10)
        FakeModbus.instances[0].dump["holding"][0:2] = nb.encode_float32(16)
        clock.now += 100
        return await client.poll(keepalive, 10)

    assert run(scenario()).current_limit == 10.0


def test_confirmed_drop_guard():
    guard = nb.ConfirmedDropGuard()
    assert guard.filter(None) is None
    assert guard.filter(18.0) == 18.0
    assert guard.filter(0.0) == 18.0        # 1 low poll: held
    assert guard.filter(18.0) == 18.0       # recovered: counter resets
    assert guard.filter(0.0) == 18.0
    assert guard.filter(0.0) == 18.0
    assert guard.filter(0.2) == 0.2         # 3rd consecutive low poll: accepted
    assert guard.filter(0.1) == 0.2         # a new dip is held again
    assert guard.filter(0.4) == 0.4         # rises pass at once
