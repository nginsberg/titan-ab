"""The daemon: the only process that talks to the machine. Holds the link, serves the unix socket, and its own
terminal window is the owner's emergency STOP key.

STOP rules (fixes carried over from the web page):
- STOP bypasses the request queue and is never dropped.
- A transmit generation counter is bumped by STOP / PAUSE / cancel / disconnect. Every non-urgent frame carries the
  generation it was created under and checks it before every chunk, so a queued START can never go out after STOP and
  a half-written frame is abandoned after its current chunk. A fire/drill sequence captures the generation once, so
  none of its later steps can be sent after a STOP.
- Each GATT write times out after 1 s. STOP is retried once if there is no reply. If no STOP write succeeds the window
  shows a huge "STOP NOT SENT - USE MACHINE POWER BUTTON".
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import protocol as P

WRITE_TIMEOUT = 1.0        # one GATT write
REPLY_TIMEOUT = 2.0
SAVE_DRILL_TIMEOUT = 3.5
STOP_REPLY_TIMEOUT = 1.2
STOP_LOCK_WAIT = 1.2       # how long STOP waits for an in-flight chunk before writing directly
STOP_ALARM_AFTER = 1.5
SPEED_CAP = 14             # level 14 = 50 mph; above needs allow_fast
F = P.FUNC
START_SENT = "START was sent; the machine may be running. Press STOP if it is firing."


class Cancelled(Exception):
    pass


class NoReply(Exception):
    pass


class WriteTimeout(Exception):
    pass


@dataclass
class Waiter:
    func: int
    fut: asyncio.Future
    urgent: bool = False


def runs_path(runs_dir) -> Path:
    p = Path(runs_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


class SessionLog:
    def __init__(self, runs_dir):
        self.dir = runs_path(runs_dir)

    def write(self, direction: str, msg: str):
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + ".%03d" % int((now % 1) * 1000)
        path = self.dir / ("session-%s.log" % time.strftime("%Y%m%d", time.localtime(now)))
        try:
            with open(path, "a") as f:
                f.write("%s %-4s %s\n" % (stamp, direction, msg))
        except OSError:
            pass


# ---------------------------------------------------------------- BLE transport (bleak) ----
class BleakTransport:
    """Real Bluetooth. bleak is imported only inside connect(), so nothing else in the package touches CoreBluetooth."""
    name = "ble"

    def __init__(self, runs_dir, address: str | None = None, say=print):
        self.runs = runs_path(runs_dir)
        self.address = address
        self.explicit = bool(address)
        self.pinned = False
        self.say = say
        self.client = None
        self.connected = False
        self.description = "?"
        self._on_notify = self._on_disconnect = None
        self._loop = None

    def set_callbacks(self, on_notify, on_disconnect):
        self._on_notify, self._on_disconnect = on_notify, on_disconnect

    def _saved_address(self):
        try:
            return json.loads((self.runs / "device.json").read_text()).get("address")
        except (OSError, ValueError):
            return None

    async def connect(self):
        """Connect to exactly one machine. After the first successful connect the address is pinned, so a reconnect
        can only ever reach that same machine. Scans only when there is no explicit (--address) or saved address."""
        from bleak import BleakClient, BleakScanner
        self._loop = asyncio.get_running_loop()
        if self.client is not None:   # left over from a dropped link
            await self.disconnect()
        save = False
        addr = self.address or self._saved_address()
        if addr:
            save = self.explicit and not self.pinned
            self.say("looking for device %s ..." % addr)
            dev = await BleakScanner.find_device_by_address(addr, timeout=10.0)
            if dev is None:
                raise ConnectionError(
                    "device %s not found (machine on? phone app disconnected?). Not scanning for other machines; "
                    "to use a different one restart with --address <uuid> or delete runs/device.json" % addr)
        else:
            self.say('scanning 8 s for a Titan (Nordic UART service, or name containing "titan") ...')
            found = await BleakScanner.discover(timeout=8.0, return_adv=True)
            items = list(found.values())
            nus = [(d, a) for d, a in items if P.NUS_SERVICE in [u.lower() for u in (a.service_uuids or [])]]
            named = [(d, a) for d, a in items if "titan" in (a.local_name or d.name or "").lower()]
            cands = nus or named
            if not cands:
                raise ConnectionError("no Titan found (machine on? phone app disconnected? %d devices seen)" % len(items))
            if len(cands) > 1:
                listing = "; ".join("%s %s RSSI %s" % (a.local_name or d.name or "?", d.address, a.rssi)
                                    for d, a in cands)
                raise ConnectionError("%d possible machines found, not picking one: %s. Restart with "
                                      "--address <address>" % (len(cands), listing))
            dev = cands[0][0]
            save = True
        self.say("connecting to %s (%s) ..." % (dev.name or "?", dev.address))
        client = BleakClient(dev, disconnected_callback=self._disc, timeout=15.0)
        try:
            await client.connect()
            await client.start_notify(P.NUS_NOTIFY, self._notify)
        except BaseException:
            # failed or cancelled part way: do not leave a stray link behind
            try:
                await asyncio.wait_for(client.disconnect(), 5.0)
            except BaseException:
                pass
            raise
        self.client = client
        self.connected = True
        self.address = dev.address   # pinned: reconnects only look for this machine
        self.pinned = True
        self.description = "%s %s" % (dev.name or "?", dev.address)
        if save:
            try:
                (self.runs / "device.json").write_text(json.dumps({"address": dev.address, "name": dev.name}))
            except OSError:
                pass

    def _notify(self, _char, data):
        self._loop.call_soon_threadsafe(self._on_notify, bytes(data))

    def _disc(self, client):
        if client is not self.client:   # an old client, or our own deliberate disconnect
            return
        self.connected = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._on_disconnect)

    async def write(self, data: bytes):
        await self.client.write_gatt_char(P.NUS_WRITE, data, response=True)

    async def disconnect(self):
        c, self.client = self.client, None
        self.connected = False
        if c is not None:
            try:
                await asyncio.wait_for(c.disconnect(), 5.0)
            except Exception:
                pass


# ---------------------------------------------------------------- daemon ----
class Daemon:
    def __init__(self, transport, runs_dir, pin: str | None = None, keyboard: bool = True, out=None,
                 poll_interval: float = 1.0, fake: bool = False):
        self.t = transport
        self.runs = runs_path(runs_dir)
        self.sock_path = self.runs / "titan.sock"
        self.slog = SessionLog(self.runs)
        self.pin = pin
        self.keyboard = keyboard
        self.out = out or sys.stdout
        self.tty_out = hasattr(self.out, "isatty") and self.out.isatty()
        self.poll_interval = poll_interval
        self.fake = fake
        self.parser = P.FrameParser(self._on_frame)
        self.gen = 0
        self.stream_task = None
        self.stream_info = "-"
        self.gen_why = "STOP"
        self.waiters: list[Waiter] = []
        self.connected = False
        self.ever_connected = False
        self.version: P.Version | None = None
        self.pin_state = "-"
        self.pin_needed = False
        self.connect_count = 0
        self.machine_status = "?"
        self.last_cmd = "-"
        self.sequence_running = False
        self.stop_task: asyncio.Task | None = None
        self.alarms: list[str] = []
        self.quitting = False
        self._tty_saved = None
        self._tasks: list[asyncio.Task] = []
        self._server = None
        self._loop = None

    # ---- output ----
    def log(self, direction, msg):
        self.slog.write(direction, msg)

    def _status_text(self):
        conn = "CONNECTED" if self.connected else "NOT CONNECTED"
        pin = "PIN NEEDED (titan pin NNNNNN) | " if self.pin_needed else ""
        return "[%s] %smachine %s | last: %s | SPACE=STOP p=PAUSE q=quit" % (
            conn, pin, self.machine_status, self.last_cmd)

    def _write(self, s):
        try:
            self.out.write(s)
            self.out.flush()
        except Exception:
            pass

    def redraw(self):
        if self.tty_out:
            try:
                width = os.get_terminal_size(self.out.fileno()).columns or 80
            except OSError:
                width = 80
            self._write("\r\033[K" + self._status_text()[: max(10, width - 1)])

    def say(self, msg, direction="--"):
        self.log(direction, msg)
        self._write(("\r\033[K" if self.tty_out else "") + msg + "\n")
        self.redraw()

    def loud(self, msg):
        self.alarms.append(msg)
        self.log("ALRM", msg)
        bar = "#" * (len(msg) + 12)
        block = "\n%s\n%s\n#####  %s  #####\n%s\n%s\n" % (bar, bar, msg, bar, bar)
        self._write(("\r\033[K" if self.tty_out else "") + "\a\a\a" + block)
        self.redraw()

    # ---- frames ----
    def _on_notify(self, data: bytes):
        self.log("RX~", "chunk " + P.hexs(data))
        self.parser.feed(data)

    def _on_frame(self, fr: P.Frame):
        self.log("RX", "frame func=0x%02x len=%d payload=[%s] %s" % (
            fr.func, len(fr.payload), P.hexs(fr.payload),
            "no crc (zero-length)" if fr.no_crc else "crc ok" if fr.crc_ok else "CRC BAD"))
        for w in self.waiters:
            if w.func == fr.func and not w.fut.done():
                self.waiters.remove(w)
                w.fut.set_result(fr)
                return
        if fr.func in P.PUSH:
            self.log("RX", "machine push: %s" % P.PUSH[fr.func])
        else:
            self.log("RX", "unexpected frame (nothing waiting for func 0x%02x)" % fr.func)

    def _add_waiter(self, func, urgent=False) -> Waiter:
        w = Waiter(func, asyncio.get_running_loop().create_future(), urgent)
        self.waiters.append(w)
        return w

    def _drop_waiter(self, w):
        if w in self.waiters:
            self.waiters.remove(w)

    def cancel_all(self, why):
        """Bump the transmit generation and abandon every non-urgent reply wait."""
        self.gen += 1
        self.gen_why = why
        n = 0
        for w in list(self.waiters):
            if not w.urgent:
                self.waiters.remove(w)
                if not w.fut.done():
                    w.fut.set_exception(Cancelled(why))
                    w.fut.exception()  # mark retrieved; the request awaiting it still sees it
                n += 1
        return n

    def _check_gen(self, gen):
        if gen is not None and gen != self.gen:
            raise Cancelled(self.gen_why)

    async def _gatt_write(self, part: bytes):
        if not self.connected:
            raise ConnectionError("not connected")
        try:
            await asyncio.wait_for(self.t.write(part), WRITE_TIMEOUT)
        except asyncio.TimeoutError:
            raise WriteTimeout("GATT write timed out (%.1f s)" % WRITE_TIMEOUT) from None

    async def write_frame(self, frame: bytes, name: str, gen: int | None):
        """Non-urgent frame: checks the generation before every chunk (before and after taking the write lock)."""
        parts = P.chunk(frame)
        self.log("TX", "%s %s (%d B, %d write%s)" % (name, P.hexs(frame), len(frame), len(parts),
                                                       "s" if len(parts) > 1 else ""))
        for i, part in enumerate(parts):
            try:
                self._check_gen(gen)
                async with self._write_lock:
                    self._check_gen(gen)
                    await self._gatt_write(part)
            except Cancelled:
                self.log("TX", "%s abandoned after %d of %d chunks (%s)" % (name, i, len(parts), self.gen_why))
                raise

    async def _urgent_write(self, frame: bytes, name: str, before_write=None):
        """STOP: never checks the generation. Waits briefly for an in-flight chunk, then writes directly.
        before_write runs once the lock is held (or the wait gave up), right before the first chunk goes out."""
        parts = P.chunk(frame)
        self.log("TX", "%s %s (%d B, urgent)" % (name, P.hexs(frame), len(frame)))
        got = False
        try:
            await asyncio.wait_for(self._write_lock.acquire(), STOP_LOCK_WAIT)
            got = True
        except asyncio.TimeoutError:
            self.log("TX", "write lock busy > %.1f s; writing STOP directly" % STOP_LOCK_WAIT)
        try:
            if before_write:
                before_write()
            for part in parts:
                await self._gatt_write(part)
        finally:
            if got:
                self._write_lock.release()

    async def request(self, name, func, frame, timeout=REPLY_TIMEOUT, gen=None, on_written=None) -> P.Frame:
        g = self.gen if gen is None else gen
        self._check_gen(g)
        if not self.connected:
            raise ConnectionError("not connected")
        async with self._req_lock:
            self._check_gen(g)
            w = self._add_waiter(func)
            try:
                await self.write_frame(frame, name, g)
                if on_written:
                    on_written()
                try:
                    return await asyncio.wait_for(w.fut, timeout)
                except asyncio.TimeoutError:
                    raise NoReply("no reply to %s within %.1f s" % (name, timeout)) from None
            finally:
                self._drop_waiter(w)

    # ---- STOP / PAUSE / START ----
    async def stop(self, why="STOP") -> dict:
        n = self.cancel_all(why)
        self.log("--", "STOP (%s): bumped generation to %d, abandoned %d waits" % (why, self.gen, n))
        if self.stop_task is None or self.stop_task.done():
            self.stop_task = asyncio.ensure_future(self._stop_inner(why))
        return await asyncio.shield(self.stop_task)

    async def _stop_inner(self, why) -> dict:
        frame = P.control("STOP")
        if not self.connected:
            self.loud("STOP NOT SENT - NOT CONNECTED - USE MACHINE POWER BUTTON")
            return {"ok": False, "error": "STOP NOT SENT - NOT CONNECTED - USE MACHINE POWER BUTTON"}
        written = False

        def alarm():
            if not written:
                self.loud("STOP NOT CONFIRMED SENT AFTER %.1f s - BE READY TO USE MACHINE POWER BUTTON" % STOP_ALARM_AFTER)
        timer = asyncio.get_running_loop().call_later(STOP_ALARM_AFTER, alarm)
        lines = []
        try:
            for attempt in (1, 2):
                box = {}

                def arm(box=box):
                    # Registered only now, after the write lock: a reply to a frame that was still being written
                    # (e.g. a raced START) while STOP waited for the lock cannot be taken as the STOP reply.
                    box["w"] = self._add_waiter(F["CONTROL"], urgent=True)
                try:
                    await self._urgent_write(frame, "Control STOP (try %d)" % attempt, before_write=arm)
                    written = True
                    w = box["w"]
                except Exception as e:
                    if "w" in box:
                        self._drop_waiter(box["w"])
                    self.log("ERR", "STOP write try %d failed: %s" % (attempt, e))
                    lines.append("STOP write try %d failed: %s" % (attempt, e))
                    continue
                try:
                    fr = await asyncio.wait_for(w.fut, STOP_REPLY_TIMEOUT)
                except asyncio.TimeoutError:
                    fr = None
                    lines.append("STOP try %d: written, no reply within %.1f s" % (attempt, STOP_REPLY_TIMEOUT))
                finally:
                    self._drop_waiter(w)
                if fr is not None:
                    msg = "STOP sent (try %d), reply payload [%s]" % (attempt, P.hexs(fr.payload))
                    self.say(msg)
                    return {"ok": True, "lines": lines + [msg]}
        finally:
            timer.cancel()
        if not written:
            msg = "STOP NOT SENT - USE MACHINE POWER BUTTON"
        else:
            msg = "STOP WRITTEN BUT MACHINE DID NOT REPLY - USE MACHINE POWER BUTTON IF IT IS STILL FIRING"
        self.loud(msg)
        return {"ok": False, "error": msg, "lines": lines}

    async def pause(self) -> dict:
        n = self.cancel_all("PAUSE")
        self.log("--", "PAUSE: abandoned %d waits" % n)
        fr = await self.request("Control PAUSE", F["CONTROL"], P.control("PAUSE"))
        return {"ok": fr.crc_ok, "lines": ["PAUSE sent, reply payload [%s]" % P.hexs(fr.payload)]}

    async def start(self) -> dict:
        fr = await self.request("Control START", F["CONTROL"], P.control("START"))
        return {"ok": fr.crc_ok, "lines": ["START sent, reply payload [%s]" % P.hexs(fr.payload)]}

    # ---- sequences ----
    async def run_sequence(self, steps, on_ok=None) -> dict:
        """steps: (name, func, frame, timeout, is_start). Captures the generation once: STOP/PAUSE kills every later step.
        on_ok(func) runs after each step whose reply came back CRC-valid."""
        if self._seq_lock.locked():
            return {"ok": False, "error": "busy: another fire/drill sequence is running"}
        async with self._seq_lock:
            self.sequence_running = True
            g = self.gen
            lines = []
            state = {"start_written": False, "step": 0}
            try:
                for i, (name, func, frame, timeout, is_start) in enumerate(steps):
                    state["step"] = i
                    def mark(is_start=is_start):
                        if is_start:
                            state["start_written"] = True
                    fr = await self.request(name, func, frame, timeout, gen=g, on_written=mark)
                    if not fr.crc_ok:
                        raise NoReply("%s: reply failed the CRC check" % name)
                    if on_ok:
                        on_ok(func)
                    lines.append("%d. %s: reply payload [%s]" % (i + 1, name, P.hexs(fr.payload)))
                lines.append("Done. Machine should be firing.")
                return {"ok": True, "lines": lines}
            except Cancelled as e:
                lines.append("Sequence cancelled by %s." % e + (" " + START_SENT if state["start_written"] else ""))
                return {"ok": False, "lines": lines, "error": "cancelled by %s" % e}
            except Exception as e:
                name = steps[state["step"]][0]
                last_is_start = steps[state["step"]][4]
                if state["start_written"] or (last_is_start and isinstance(e, WriteTimeout)):
                    msg = START_SENT + " (%s)" % e
                else:
                    msg = "STOPPED at step %d (%s): %s" % (state["step"] + 1, name, e)
                lines.append(msg)
                return {"ok": False, "lines": lines, "error": msg}
            finally:
                self.sequence_running = False

    def _check_shots(self, shots, allow_fast) -> str | None:
        for i, s in enumerate(shots):
            err = P.validate_shot(s)
            if err:
                return "shot %d: %s" % (i + 1, err)
            if s["speed"] > SPEED_CAP and allow_fast is not True:
                return "shot %d: speed level %d (%s) is above the safety cap %d (%s); add --allow-fast" % (
                    i + 1, s["speed"], P.label("speed", s["speed"]), SPEED_CAP, P.label("speed", SPEED_CAP))
        return None

    async def fire(self, shot, via="drill", allow_fast=False) -> dict:
        err = self._check_shots([shot], allow_fast)
        if err:
            return {"ok": False, "error": err}
        if via == "drill":
            picked = await self._pick_slot()
            if isinstance(picked, dict):
                return picked
            slot, why, n = picked
            ball = P.ball_bytes(False, 1, [shot])
            steps = [
                ("SaveDrill slot %d (1 Regular ball)" % slot, F["SAVE_DRILL"],
                 P.save_drill(slot, "FIRE", [ball]), SAVE_DRILL_TIMEOUT, False),
                ("SetCurrentDrill [%d]" % slot, F["SET_CURRENT"], P.set_current([slot]), REPLY_TIMEOUT, False),
                ("Control START", F["CONTROL"], P.control("START"), REPLY_TIMEOUT, True),
            ]
            r = await self.run_sequence(steps, on_ok=lambda f: self._save_slot(slot) if f == F["SAVE_DRILL"] else None)
            r["lines"] = ["machine holds %d drills; using slot %d (%s)" % (n, slot, why)] + r.get("lines", [])
            return r
        elif via == "balltest":
            steps = [
                ("BallTest (Regular, loop 1)", F["BALL_TEST"], P.ball_test(False, 1, [shot]), REPLY_TIMEOUT, False),
                ("Control START", F["CONTROL"], P.control("START"), REPLY_TIMEOUT, True),
            ]
        else:
            return {"ok": False, "error": "via must be drill or balltest"}
        return await self.run_sequence(steps)

    def _slot_file(self):
        return self.runs / "drill_slot.json"

    def _saved_slot(self):
        try:
            v = json.loads(self._slot_file().read_text()).get("slot")
            return v if isinstance(v, int) and not isinstance(v, bool) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _save_slot(self, slot):
        try:
            self._slot_file().write_text(json.dumps({"slot": slot}))
        except OSError:
            pass

    async def drill_count(self) -> int:
        """N = number of drills stored on the machine (GetCrcList: 2 bytes per drill)."""
        fr = await self.request("GetCrcList", F["CRC_LIST"], P.get_crc_list())
        if not fr.crc_ok or len(fr.payload) % 2:
            raise NoReply("GetCrcList reply unusable (%d payload bytes)" % len(fr.payload))
        return len(fr.payload) // 2

    async def _pick_slot(self, slot=None):
        """The machine only accepts SaveDrill for an existing slot or the next free one (it stores drills 1..N), so the
        slot is chosen from GetCrcList: the slot we saved to before if it is <= N, else N+1. `slot` overrides (<= N+1).
        Returns (slot, why, N), or an {"ok": False, "error": ...} dict."""
        if slot is not None and (not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= P.MAX_DRILLS):
            return {"ok": False, "error": "slot must be 1-%d" % P.MAX_DRILLS}
        try:
            n = await self.drill_count()
        except Cancelled as e:
            return {"ok": False, "error": "cancelled by %s" % e}
        except Exception as e:
            return {"ok": False, "error": "could not read the machine's drill list, nothing saved: %s" % e}
        saved = self._saved_slot()
        if slot is not None:
            if slot > n + 1:
                return {"ok": False, "error": "slot %d is not available: the machine holds %d drills, so use a slot "
                        "from 1 to %d" % (slot, n, n + 1)}
            why = "--slot"
        elif saved is not None and 1 <= saved <= n:
            slot, why = saved, "reusing our slot"
        elif n + 1 > P.MAX_DRILLS:
            return {"ok": False, "error": "the machine already holds %d drills (max %d) and none is ours to reuse; "
                    "delete one on the machine, or pick a slot with --slot" % (n, P.MAX_DRILLS)}
        else:
            slot, why = n + 1, "next free"
        return slot, why, n

    async def drill(self, A, B, pairs=2, repeat=1, slot=None, allow_fast=False, out=None) -> dict:
        if len(A) != 3 or len(B) != 3:
            return {"ok": False, "error": "need exactly 3 shots in A and 3 in B"}
        err = self._check_shots(list(A) + list(B) + ([out] if out else []), allow_fast)
        if err:
            return {"ok": False, "error": err}
        for nm, v, lo, hi in (("pairs", pairs, 1, 8), ("repeat", repeat, 1, 99)):
            if not isinstance(v, int) or isinstance(v, bool) or v < lo or v > hi:
                return {"ok": False, "error": "%s must be %d-%d" % (nm, lo, hi)}
        picked = await self._pick_slot(slot)
        if isinstance(picked, dict):
            return picked
        slot, why, n = picked
        frame = P.save_drill(slot, "AB RANDOM", P.ab_balls(A, B, pairs, out))
        steps = [
            ("SaveDrill slot %d (%d pairs)" % (slot, pairs), F["SAVE_DRILL"], frame, SAVE_DRILL_TIMEOUT, False),
            ("SetCurrentDrill x%d" % repeat, F["SET_CURRENT"], P.set_current(P.repeat_slots(slot, repeat)),
             REPLY_TIMEOUT, False),
            ("Control START", F["CONTROL"], P.control("START"), REPLY_TIMEOUT, True),
        ]
        r = await self.run_sequence(steps, on_ok=lambda f: self._save_slot(slot) if f == F["SAVE_DRILL"] else None)
        r["lines"] = ["machine holds %d drills; using slot %d (%s)" % (n, slot, why)] + r.get("lines", [])
        return r

    async def stream(self, A, B, pairs=20, allow_fast=False) -> dict:
        """A/B drill without SaveDrill: the laptop picks a random A shot, fires it as a
        one-ball BallTest, waits that shot's feed time, then does the same for B. STOP (any source) ends it."""
        if not A or not B:
            return {"ok": False, "error": "need at least one shot in A and in B"}
        err = self._check_shots(list(A) + list(B), allow_fast)
        if err:
            return {"ok": False, "error": err}
        if not isinstance(pairs, int) or isinstance(pairs, bool) or not 1 <= pairs <= 100:
            return {"ok": False, "error": "pairs must be 1-100"}
        if self.stream_task is not None and not self.stream_task.done():
            return {"ok": False, "error": "busy: a stream is already running (STOP it first)"}
        self.stream_task = asyncio.ensure_future(self._stream_loop(A, B, pairs, self.gen))
        return {"ok": True, "lines": ["stream started: %d pairs (%d balls). STOP ends it." % (pairs, 2 * pairs)]}

    async def _stream_loop(self, A, B, pairs, g0):
        async def wait(sec):
            end = time.monotonic() + sec
            while time.monotonic() < end:
                if self.gen != g0:
                    return False
                await asyncio.sleep(0.1)
            return self.gen == g0
        try:
            for p in range(pairs):
                for grp, shots in (("A", A), ("B", B)):
                    if self.gen != g0 or not self.connected:
                        self.say("stream ended (STOP or disconnect)")
                        return
                    i = random.randrange(len(shots))
                    s = shots[i]
                    r = await self.run_sequence([
                        ("BallTest %s%d" % (grp, i + 1), F["BALL_TEST"], P.ball_test(False, 1, [s]), REPLY_TIMEOUT, False),
                        ("Control START", F["CONTROL"], P.control("START"), REPLY_TIMEOUT, True)])
                    self.stream_info = "pair %d/%d %s%d" % (p + 1, pairs, grp, i + 1)
                    self.say("stream %s: %s" % (self.stream_info, "ok" if r.get("ok") else r.get("error")))
                    if not r.get("ok"):
                        if self.gen != g0:
                            return
                        if START_SENT not in str(r.get("error")):
                            self.loud("stream aborted: %s" % r.get("error"))
                            return
                    if not await wait(P.FEED_SEC[s["feed"] - 1]):
                        self.say("stream ended (STOP)")
                        return
            self.say("stream finished %d pairs; sending STOP to spin down" % pairs)
            await self.stop("stream finished")
        except Exception as e:
            self.loud("stream crashed: %r - press SPACE to STOP" % (e,))
        finally:
            self.stream_info = "-"

    # ---- connect / PIN / poll ----
    def _pin_file(self):
        return self.runs / "pin"

    def _stored_pin(self):
        if self.pin:
            return self.pin
        try:
            return self._pin_file().read_text().strip()
        except OSError:
            return ""

    def _save_pin(self, code):
        try:
            p = self._pin_file()
            p.write_text(code + "\n")
            os.chmod(p, 0o600)
        except OSError:
            pass

    async def verify(self, kind, code) -> str:
        fr = await self.request("VerifyCode", F["VERIFY"], P.verify(kind, code))
        res = P.parse_verify(fr) if fr.crc_ok else "failed"
        if res == "ok" and code:
            self._save_pin(code)
        self.pin_state = {"ok": "PIN ok", "needPin": "PIN needed", "failed": "PIN wrong"}[res]
        return res

    PIN_NEEDED_MSG = "PIN NEEDED: run `.venv/bin/python -m titan pin NNNNNN` (digits from the machine screen), or restart with --pin"
    RUNNING = ("START", "PENDING_START", "MOTO_STARTING", "TESTING")

    async def after_connect(self):
        fr = await self.request("GetVersion", F["VERSION"], P.get_version())
        v = P.parse_version(fr.raw)
        self.version = v
        self.say("version %r major=%s minor=%s mode=%s (%s) needsPin=%s" % (
            v.text, v.major, v.minor, v.mode, v.mode_name, v.needs_pin))
        if not v.needs_pin:
            self.pin_state = "not required"
            self.pin_needed = False
        else:
            # Never prompts: the keyboard must stay the STOP key. A missing/rejected PIN is a loud persistent state.
            res = await self.verify(0, self._stored_pin())
            if res != "ok" and self.pin:
                res = await self.verify(1, self.pin)
            self.pin_needed = res != "ok"
            if self.pin_needed:
                self.loud(self.PIN_NEEDED_MSG)
            else:
                self.say("PIN ok")
        await self.check_running()

    async def check_running(self):
        """First status after connecting: warn loudly if the machine is already running."""
        try:
            fr = await self.request("GetRobotStatus", F["STATUS"], P.get_status(), timeout=1.5)
        except Exception as e:
            self.say("first status check failed: %s" % e, "ERR")
            return
        if fr.crc_ok:
            self.machine_status = P.parse_status(fr)["name"]
            if self.machine_status in self.RUNNING:
                self.loud("MACHINE IS ALREADY RUNNING (status %s) - press SPACE to STOP" % self.machine_status)
            self.redraw()

    async def connection_loop(self):
        while not self.quitting:
            self._disconnected = None
            try:
                self.machine_status = "connecting"
                self.redraw()
                await asyncio.wait_for(self.t.connect(), 60.0)
                self._disconnected = asyncio.Event()
                if not self.t.connected:
                    raise ConnectionError("link dropped right after connecting")
                self.parser.reset()
                self.connected = True
                self.ever_connected = True
                self.say("CONNECTED to %s" % getattr(self.t, "description", "?"))
                try:
                    await self.after_connect()
                except Exception as e:
                    self.say("after connect: %s" % e, "ERR")
                self.connect_count += 1
                await self._disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.ever_connected:
                    self.loud("RECONNECT FAILED: %s: %s - retrying" % (type(e).__name__, e))
                else:
                    self.say("connect failed: %s: %s" % (type(e).__name__, e), "ERR")
            if self.quitting:
                break
            self.connected = False
            self.machine_status = "?"
            await asyncio.sleep(3.0)
            if not self.quitting:
                self.say("reconnecting ...")

    def _on_disconnect(self):
        was = self.connected
        self.connected = False
        self.cancel_all("disconnected")
        if self.quitting:
            return
        if was:
            self.loud("BLUETOOTH DISCONNECTED - MACHINE MAY STILL BE RUNNING - USE MACHINE POWER BUTTON. Reconnecting ...")
        ev = getattr(self, "_disconnected", None)
        if ev:
            ev.set()

    async def poll_loop(self):
        misses = 0
        while not self.quitting:
            await asyncio.sleep(self.poll_interval)
            if (not self.connected or self.sequence_running or self._req_lock.locked()
                    or (self.stop_task is not None and not self.stop_task.done())):
                continue
            try:
                fr = await self.request("GetRobotStatus", F["STATUS"], P.get_status(), timeout=1.5)
                if fr.crc_ok:
                    self.machine_status = P.parse_status(fr)["name"]
                    misses = 0
            except Exception:
                misses += 1
                if misses >= 3:
                    self.machine_status = "no reply"
            self.redraw()

    # ---- keyboard ----
    def _keyboard_on(self):
        if not (self.keyboard and sys.stdin.isatty()):
            return
        import termios
        import tty
        fd = sys.stdin.fileno()
        if self._tty_saved is None:
            self._tty_saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~termios.IXON        # Ctrl-S must not freeze output / the STOP key
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        try:
            self._old_tstp = signal.signal(signal.SIGTSTP, signal.SIG_IGN)   # Ctrl-Z must not suspend the daemon
        except (ValueError, OSError):
            self._old_tstp = None
        self._loop.add_reader(fd, self._on_key)

    def _keyboard_off(self):
        if self._tty_saved is None:
            return
        import termios
        fd = sys.stdin.fileno()
        try:
            self._loop.remove_reader(fd)
        except Exception:
            pass
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, self._tty_saved)
        except Exception:
            pass
        if getattr(self, "_old_tstp", None) is not None:
            try:
                signal.signal(signal.SIGTSTP, self._old_tstp)
            except (ValueError, OSError):
                pass

    def _on_key(self):
        try:
            data = os.read(sys.stdin.fileno(), 64)
        except OSError:
            return
        if not data:
            return
        keys = data.decode("latin-1")
        # STOP first if it appears anywhere in this read; then q (which also STOPs); PAUSE only on its own.
        if any(ch in " \r\nsS" for ch in keys):
            self.last_cmd = "STOP (key) " + time.strftime("%H:%M:%S")
            self._spawn(self._key_stop())
        if any(ch in "qQ" for ch in keys):
            self._spawn(self.shutdown("q pressed"))
        elif any(ch in "pP" for ch in keys) and not any(ch in " \r\nsS" for ch in keys):
            self.last_cmd = "PAUSE (key) " + time.strftime("%H:%M:%S")
            self._spawn(self._key_pause())

    async def _key_stop(self):
        self.say("STOP pressed")
        await self.stop("STOP key")

    async def _key_pause(self):
        self.say("PAUSE pressed")
        try:
            r = await self.pause()
            self.say(r["lines"][0])
        except Exception as e:
            self.say("PAUSE failed: %s" % e, "ERR")

    def _spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self._tasks.append(t)
        return t

    # ---- socket ----
    async def _handle_client(self, reader, writer):
        try:
            line = await reader.readline()
            try:
                req = json.loads(line.decode() or "{}")
            except ValueError:
                req = {}
            self.log("CMD", json.dumps(req))
            try:
                resp = await self.dispatch(req)
            except Exception as e:
                resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            self.log("CMD", "-> " + json.dumps(resp)[:2000])
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def dispatch(self, req) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, "data": {
                "connected": self.connected, "transport": getattr(self.t, "name", "?"),
                "device": getattr(self.t, "description", "?"),
                "version": self.version.text.replace("\0", "") if self.version else None,
                "needs_pin": self.version.needs_pin if self.version else None, "pin": self.pin_state, "pin_needed": self.pin_needed,
                "machine_status": self.machine_status, "last_cmd": self.last_cmd,
                "sequence_running": self.sequence_running, "stream": self.stream_info, "alarms": self.alarms[-3:]}}
        if cmd == "stop":
            self.say("STOP from client")
            r = await self.stop("STOP command")
        elif cmd == "fake":
            return self._fake_cmd(req)
        else:
            if not self.connected:
                return {"ok": False, "error": "not connected to the machine"}
            if cmd in ("start", "fire", "drill", "stream") and self.stop_task is not None and not self.stop_task.done():
                return {"ok": False, "error": "a STOP is still in progress; try again in a few seconds"}
            if cmd == "pause":
                r = await self.pause()
            elif cmd == "start":
                r = await self.start()
            elif cmd == "fire":
                r = await self.fire(req.get("shot") or {}, req.get("via", "drill"), req.get("allow_fast") is True)
            elif cmd == "drill":
                r = await self.drill(req.get("A") or [], req.get("B") or [], req.get("pairs", 2),
                                     req.get("repeat", 1), req.get("slot"), req.get("allow_fast") is True,
                                     req.get("out"))
            elif cmd == "stream":
                r = await self.stream(req.get("A") or [], req.get("B") or [], req.get("pairs", 20),
                                      req.get("allow_fast") is True)
            elif cmd == "raw":
                # Debug: one frame by func + hex payload; waits for a reply with the same func (or times out).
                func = int(req["func"])
                payload = bytes.fromhex(req.get("payload", ""))
                try:
                    fr = await self.request("raw 0x%02x" % func, func, P.build_frame(func, payload), float(req.get("timeout", 3.0)))
                    r = {"ok": fr.crc_ok, "lines": ["raw 0x%02x reply payload [%s]" % (func, P.hexs(fr.payload))]}
                except NoReply as e:
                    r = {"ok": False, "lines": [], "error": str(e)}
            elif cmd == "pin":
                code = str(req.get("code", ""))
                res = await self.verify(1, code)
                if res == "ok":
                    self.pin_needed = False
                r = {"ok": res == "ok", "lines": ["VerifyCode: %s" % res]}
            else:
                return {"ok": False, "error": "unknown command %r" % cmd}
        self.last_cmd = "%s %s %s" % (cmd, "ok" if r.get("ok") else "FAILED", time.strftime("%H:%M:%S"))
        for ln in ([] if cmd == "stop" else r.get("lines", [])):
            self.say("  " + ln)
        if r.get("error") and r.get("error") not in r.get("lines", []):
            self.say("  " + r["error"])
        return r

    def _fake_cmd(self, req) -> dict:
        if not self.fake:
            return {"ok": False, "error": "fake command only works with --fake"}
        t = self.t
        if "write_delay" in req:
            t.write_delay = float(req["write_delay"])
        if "hang" in req:
            t.hang = bool(req["hang"])
        if "no_reply" in req:
            t.no_reply = set(int(x) for x in req["no_reply"])
        for k in ("reply_delay",):
            if k in req:
                setattr(t, k, float(req[k]))
        for k in ("reply_before_ack", "echo_control"):
            if k in req:
                setattr(t, k, bool(req[k]))
        if req.get("clear"):
            t.frames.clear()
        return {"ok": True, "data": {"frames": [[f, P.hexs(p)] for f, p in t.frames], "status": t.status}}

    # ---- lifecycle ----
    async def _open_socket(self):
        if self.sock_path.exists():
            try:
                r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(self.sock_path)), 1.0)
                w.close()
                raise RuntimeError("another daemon is already listening on %s" % self.sock_path)
            except (ConnectionRefusedError, FileNotFoundError, asyncio.TimeoutError):
                self.sock_path.unlink()
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.sock_path))
        finally:
            os.umask(old)
        os.chmod(self.sock_path, 0o600)

    def banner(self):
        lines = [
            "=" * 66,
            " TITAN DAEMON (%s)  -  THIS WINDOW IS THE EMERGENCY STOP" % getattr(self.t, "name", "?"),
            "   SPACE / ENTER / s = STOP      p = PAUSE      q = STOP and quit",
            "   Keep this window in front while the machine has balls in it.",
            "   Socket: %s" % self.sock_path,
            "   Log:    %s/session-YYYYMMDD.log" % self.runs,
            "=" * 66,
        ]
        for ln in lines:
            self._write(ln + "\n")

    async def shutdown(self, why):
        if self.quitting:
            return
        self.say("shutting down (%s): sending STOP first" % why)
        try:
            if self.connected:
                await asyncio.wait_for(self.stop("shutdown: " + why), 5.0)
        except Exception as e:
            self.say("STOP on shutdown failed: %s" % e, "ERR")
        self.quitting = True
        self._quit.set()

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._write_lock = asyncio.Lock()
        self._req_lock = asyncio.Lock()
        self._seq_lock = asyncio.Lock()
        self._quit = asyncio.Event()
        self.t.set_callbacks(self._on_notify, self._on_disconnect)
        await self._open_socket()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                self._loop.add_signal_handler(sig, lambda s=sig: self._spawn(self.shutdown(signal.Signals(s).name)))
            except (RuntimeError, ValueError, NotImplementedError):
                pass  # not the main thread (tests)
        self.banner()
        self.log("--", "daemon started (%s)" % getattr(self.t, "name", "?"))
        self._keyboard_on()
        conn = asyncio.ensure_future(self.connection_loop())
        poll = asyncio.ensure_future(self.poll_loop())
        try:
            await self._quit.wait()
        except BaseException as e:
            # Cancelled or crashed: STOP before anything else.
            self.log("ERR", "main loop: %r" % (e,))
            if self.connected and not self.quitting:
                try:
                    await asyncio.wait_for(self.stop("exception"), 5.0)
                except BaseException:
                    pass
            raise
        finally:
            self.quitting = True
            self._keyboard_off()
            for t in [conn, poll] + self._tasks:
                if not t.done():
                    t.cancel()
            if self._server:
                self._server.close()
            try:
                self.sock_path.unlink()
            except OSError:
                pass
            try:
                await asyncio.wait_for(self.t.disconnect(), 5.0)
            except BaseException:
                pass
            self.log("--", "daemon stopped")
            self._write("\ndaemon stopped.\n")

    @property
    def ready(self):
        return self.connected and self.version is not None


def make_transport(args, runs_dir, say):
    if args.fake:
        from .fake import FakeTransport
        return FakeTransport(write_delay=args.fake_write_delay, pin=args.fake_pin)
    return BleakTransport(runs_dir, address=args.address, say=say)


def main(args, runs_dir) -> int:
    holder = {}

    def say(msg):
        d = holder.get("d")
        if d:
            d.say(msg)
        else:
            print(msg)
    t = make_transport(args, runs_dir, say)
    d = Daemon(t, runs_dir, pin=args.pin, keyboard=not args.no_keyboard, poll_interval=args.poll, fake=args.fake)
    holder["d"] = d
    try:
        asyncio.run(d.run())
    except KeyboardInterrupt:
        return 130
    except RuntimeError as e:
        print("daemon: %s" % e, file=sys.stderr)
        return 1
    finally:
        if d._tty_saved is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, d._tty_saved)
            except Exception:
                pass
    return 0
