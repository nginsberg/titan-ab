"""Simulated Titan machine behind the same transport interface as the BLE transport (no Bluetooth).

Replies like the real protocol (as far as we know it). Knobs for tests: per-chunk write delay, hung writes,
funcs that get no reply, PIN requirement, drills already stored. Drills are kept contiguous 1..N like the real machine:
SaveDrill to an existing slot or N+1 gets an empty-payload reply; any other slot gets no reply and the next frame is
swallowed too (seen on court, firmware 7.4.1PB14).
"""
from __future__ import annotations

import asyncio

from . import protocol as P


class FakeTransport:
    name = "fake"

    def __init__(self, write_delay: float = 0.0, pin: str | None = None, drills: int = 0):
        self.write_delay = write_delay   # seconds per GATT write (chunk)
        self.hang = False                # True: every write blocks forever (until cancelled)
        self.no_reply: set[int] = set()  # func codes the machine ignores
        self.pin = pin                   # if set: version needs a PIN and VerifyCode wants this code
        self.reply_delay = 0.005         # seconds from a complete request to its reply
        self.reply_before_ack = False    # True: the machine sees each chunk before the GATT write call returns
        self.echo_control = False        # True: Control replies [0, cmd] so tests can tell START/STOP replies apart
        self.drills = drills             # number of stored drills (slots 1..N)
        self._swallow = 0                # frames to ignore after a bad SaveDrill
        self.connected = False
        self.status = 2                  # STOP
        self.frames: list[tuple[int, list[int]]] = []   # every complete request received (func, payload)
        self.chunks: list[bytes] = []
        self._notify = None
        self._on_disconnect = None
        self._parser = P.FrameParser(self._on_request)
        self.description = "fake machine"

    # ---- transport interface ----
    def set_callbacks(self, on_notify, on_disconnect):
        self._notify = on_notify
        self._on_disconnect = on_disconnect

    async def connect(self):
        await asyncio.sleep(0.01)
        self.connected = True
        self._parser.reset()

    async def write(self, data: bytes):
        if not self.connected:
            raise ConnectionError("not connected")
        if self.hang:
            await asyncio.Event().wait()
        if self.reply_before_ack:
            self.chunks.append(bytes(data))
            self._parser.feed(data)
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        if not self.reply_before_ack:
            self.chunks.append(bytes(data))
            self._parser.feed(data)

    async def disconnect(self):
        self.connected = False

    def drop_link(self):
        """Simulate the link dropping (tests)."""
        self.connected = False
        if self._on_disconnect:
            self._on_disconnect()

    # ---- machine ----
    def funcs(self) -> list[int]:
        return [f for f, _ in self.frames]

    def _on_request(self, fr: P.Frame):
        if not fr.crc_ok:
            return
        self.frames.append((fr.func, list(fr.payload)))
        if self._swallow:
            self._swallow -= 1
            return
        if fr.func in self.no_reply:
            return
        F = P.FUNC
        reply = [0]
        if fr.func == F["VERSION"]:
            reply = list(("TITAN7.4.1P" if self.pin else "TITAN7.3.1P").encode()) + [0, 0, 0]
        elif fr.func == F["CRC_LIST"]:
            reply = [b for i in range(self.drills) for b in (i + 1, 0xA5)]
        elif fr.func == F["SAVE_DRILL"]:
            n = fr.payload[0] if fr.payload else 0
            if not 1 <= n <= self.drills + 1:
                self._swallow = 1
                return
            self.drills = max(self.drills, n)
            reply = []
        elif fr.func == F["STATUS"]:
            reply = [self.status, 0]
        elif fr.func == F["CONTROL"]:
            cmd = fr.payload[0] if fr.payload else 2
            self.status = {0: 0, 1: 1, 2: 2}.get(cmd, 2)
            if self.echo_control:
                reply = [0, cmd]
        elif fr.func == F["VERIFY"]:
            if not self.pin:
                reply = [0]
            elif fr.payload[1] == 1 and bytes(fr.payload[2:8]).decode("ascii", "replace") == self.pin:
                reply = [0]
            else:
                reply = [2 if fr.payload[0] == 0 else 1]
        frame = P.build_frame(fr.func, reply)
        loop = asyncio.get_running_loop()
        for i, part in enumerate(P.chunk(frame)):
            loop.call_later(self.reply_delay + 0.001 * i, self._deliver, part)

    def _deliver(self, part: bytes):
        if self.connected and self._notify:
            self._notify(part)
