"""BleakTransport against a stub `bleak` module (no CoreBluetooth): device selection, pinning, cleanup."""
import asyncio
import json
import sys
import types

import pytest

from titan import protocol as P
from titan.daemon import BleakTransport


class Dev:
    def __init__(self, address, name):
        self.address, self.name = address, name


class Adv:
    def __init__(self, uuids, name, rssi):
        self.service_uuids, self.local_name, self.rssi = uuids, name, rssi


def make_bleak(calls, visible, scan, fail_notify=None):
    """visible: addresses find_device_by_address can see; scan: list of (Dev, Adv) the scanner returns."""
    class Scanner:
        @staticmethod
        async def find_device_by_address(addr, timeout=10.0):
            calls.append(("find", addr))
            return Dev(addr, "TITAN") if addr in visible else None

        @staticmethod
        async def discover(timeout=5.0, return_adv=False):
            assert return_adv
            calls.append(("scan",))
            return {d.address: (d, a) for d, a in scan}

    class Client:
        def __init__(self, dev, disconnected_callback=None, timeout=10.0):
            self.dev, self.cb = dev, disconnected_callback
            calls.append(("client", dev.address, timeout))

        async def connect(self):
            calls.append(("connect", self.dev.address))

        async def start_notify(self, uuid, cb):
            calls.append(("notify", uuid))
            if fail_notify == "error":
                raise OSError("notify failed")
            if fail_notify == "hang":
                await asyncio.Event().wait()
            self.notify_cb = cb

        async def write_gatt_char(self, uuid, data, response=None):
            calls.append(("write", uuid, bytes(data), response))

        async def disconnect(self):
            calls.append(("disconnect", self.dev.address))

    return types.SimpleNamespace(BleakScanner=Scanner, BleakClient=Client)


NUS = [P.NUS_SERVICE]


@pytest.fixture
def runs(tmp_path):
    return tmp_path


def transport(runs, address=None):
    t = BleakTransport(runs, address=address, say=lambda m: None)
    got = []
    t.set_callbacks(got.append, lambda: got.append("disc"))
    return t, got


def test_single_candidate_scan_connects_saves_and_pins(monkeypatch, runs):
    calls = []
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(
        calls, visible={"BBB", "CCC"},
        scan=[(Dev("AAA", "Speaker"), Adv([], "Speaker", -40)), (Dev("BBB", "TITAN-ACE"), Adv(NUS, None, -70))]))
    t, got = transport(runs)

    async def go():
        await t.connect()
        assert t.client.dev.address == "BBB" and t.address == "BBB"
        await t.write(b"\x7e\x18\x00\x00\xea\xc2")
        assert ("write", P.NUS_WRITE, b"\x7e\x18\x00\x00\xea\xc2", True) in calls
        t.client.notify_cb(None, bytearray(b"\x7e\x17"))
        t.client.cb(t.client)                      # link drops
        await asyncio.sleep(0.01)
        (runs / "device.json").write_text('{"address": "CCC"}')   # even if the file changes, stay on BBB
        calls.clear()
        await t.connect()                          # reconnect: only looks for the pinned address, never scans
        assert ("find", "BBB") in calls and ("scan",) not in calls and t.client.dev.address == "BBB"
    asyncio.run(go())
    assert got == [b"\x7e\x17", "disc"]


def test_first_connect_saves_device_json(monkeypatch, runs):
    calls = []
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(calls, set(), [(Dev("BBB", "TITAN"), Adv(NUS, None, -70))]))
    t, _ = transport(runs)
    asyncio.run(t.connect())
    assert json.loads((runs / "device.json").read_text())["address"] == "BBB"
    assert ("client", "BBB", 15.0) in calls and ("notify", P.NUS_NOTIFY) in calls


def test_pinned_machine_missing_does_not_switch_to_another(monkeypatch, runs):
    calls = []
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(
        calls, visible={"OTHER"}, scan=[(Dev("OTHER", "TITAN"), Adv(NUS, None, -30))]))
    t, _ = transport(runs)
    t.address, t.pinned = "BBB", True          # as after a first connect to BBB
    with pytest.raises(ConnectionError, match="BBB not found"):
        asyncio.run(t.connect())
    assert ("scan",) not in calls and not any(c[0] == "client" for c in calls)


def test_saved_address_missing_does_not_scan_or_overwrite(monkeypatch, runs):
    calls = []
    (runs / "device.json").write_text('{"address": "OLD"}')
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(
        calls, visible=set(), scan=[(Dev("NEW", "TITAN"), Adv(NUS, None, -30))]))
    t, _ = transport(runs)
    with pytest.raises(ConnectionError, match="OLD not found.*Not scanning"):
        asyncio.run(t.connect())
    assert ("scan",) not in calls
    assert json.loads((runs / "device.json").read_text())["address"] == "OLD"


def test_saved_address_found_is_not_rewritten(monkeypatch, runs):
    calls = []
    (runs / "device.json").write_text('{"address": "OLD", "name": "keep me"}')
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(calls, visible={"OLD"}, scan=[]))
    t, _ = transport(runs)
    asyncio.run(t.connect())
    assert t.address == "OLD" and json.loads((runs / "device.json").read_text())["name"] == "keep me"


def test_explicit_address_is_saved(monkeypatch, runs):
    calls = []
    (runs / "device.json").write_text('{"address": "OLD"}')
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(calls, visible={"XYZ"}, scan=[]))
    t, _ = transport(runs, address="XYZ")
    asyncio.run(t.connect())
    assert ("find", "XYZ") in calls and json.loads((runs / "device.json").read_text())["address"] == "XYZ"


def test_several_candidates_refuses_and_lists_them(monkeypatch, runs):
    calls = []
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(calls, set(), [
        (Dev("AAA", "TITAN-1"), Adv(NUS, "TITAN-1", -40)), (Dev("BBB", "TITAN-2"), Adv(NUS, None, -70))]))
    t, _ = transport(runs)
    with pytest.raises(ConnectionError) as ei:
        asyncio.run(t.connect())
    msg = str(ei.value)
    assert "2 possible machines" in msg and "TITAN-1 AAA RSSI -40" in msg and "TITAN-2 BBB RSSI -70" in msg
    assert "--address" in msg
    assert not (runs / "device.json").exists() and not any(c[0] == "client" for c in calls)


@pytest.mark.parametrize("mode", ["error", "hang"])
def test_failed_or_cancelled_connect_disconnects(monkeypatch, runs, mode):
    calls = []
    monkeypatch.setitem(sys.modules, "bleak", make_bleak(
        calls, set(), [(Dev("BBB", "TITAN"), Adv(NUS, None, -70))], fail_notify=mode))
    t, _ = transport(runs)

    async def go():
        await asyncio.wait_for(t.connect(), 0.3)
    with pytest.raises(OSError if mode == "error" else asyncio.TimeoutError):
        asyncio.run(go())
    assert ("connect", "BBB") in calls and ("disconnect", "BBB") in calls
    assert t.client is None and not t.connected and not (runs / "device.json").exists()
