"""Titan ACE (Titan Drills Pro) BLE protocol: pure byte work, no Bluetooth.

Port of protocol.js (the spec). Reconstructed from static analysis of the Android app, not verified on hardware.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FUNC = {
    "CONTROL": 0x01, "SET_CURRENT": 0x02, "EXEC_MANUAL": 0x03, "BALL_TEST": 0x04, "SAVE_DRILL": 0x08,
    "STATUS": 0x17, "VERSION": 0x18, "VERIFY": 0x20,
}
CONTROL = {"START": 0, "PAUSE": 1, "STOP": 2}
STATUS_NAMES = ["START", "PAUSE", "STOP", "INIT", "PENDING_START", "MOTO_STARTING",
                "MOTO_STOPPING", "ALARM", "TESTING", "TEST_PAUSED"]

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_WRITE = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


def crc16(data) -> int:
    """CRC-16/XMODEM, poly 0x1021, init 0."""
    crc = 0
    for b in data:
        crc ^= (b & 0xFF) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def raw_frame(func: int, payload=None) -> list[int]:
    """[func][lenHi][lenLo][payload][crcHi][crcLo]"""
    payload = list(payload or [])
    body = [func & 0xFF, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF] + [b & 0xFF for b in payload]
    c = crc16(body)
    return body + [(c >> 8) & 0xFF, c & 0xFF]


def escape_bytes(data) -> list[int]:
    out = []
    for b in data:
        if b == 0x7E:
            out += [0x7D, 0x5E]
        elif b == 0x7D:
            out += [0x7D, 0x5D]
        else:
            out.append(b)
    return out


def build_frame(func: int, payload=None) -> bytes:
    """On the wire: leading 0x7E, then the escaped raw frame."""
    return bytes([0x7E] + escape_bytes(raw_frame(func, payload)))


def chunk(data: bytes, n: int = 20) -> list[bytes]:
    return [bytes(data[i:i + n]) for i in range(0, len(data), n)]


def hexs(data) -> str:
    return " ".join("%02x" % (b & 0xFF) for b in data)


@dataclass
class Frame:
    func: int
    payload: list[int]
    raw: list[int]
    crc_ok: bool
    no_crc: bool = False


class FrameParser:
    """Incremental reply parser (app: responseProcess).

    0x7E (re)starts a frame; "7D xx" un-escapes to xx ^ 0x20. A len-0 reply is dispatched right after its 3 header
    bytes without a CRC check (a trailing matching CRC is swallowed). Longer frames complete after 5 + len bytes and are
    CRC-checked. A reply without a leading 0x7E is accepted too.
    """

    def __init__(self, on_frame):
        self.on_frame = on_frame
        self.reset()

    def reset(self):
        self.buf: list[int] = []
        self.esc = False
        self.trail = None  # [crcHi, crcLo, index]

    def feed(self, data):
        for b in data:
            b &= 0xFF
            if b == 0x7E:
                self.reset()
                continue
            if self.esc:
                self.esc = False
                b = (b ^ 0x20) & 0xFF
            elif b == 0x7D:
                self.esc = True
                continue
            if self.trail is not None:
                if b == self.trail[self.trail[2]]:
                    self.trail[2] += 1
                    if self.trail[2] == 2:
                        self.trail = None
                    continue
                self.trail = None
            self.buf.append(b)
            if len(self.buf) >= 3:
                length = (self.buf[1] << 8) | self.buf[2]
                if length == 0:
                    h = self.buf[:3]
                    c = crc16(h)
                    self.reset()
                    self.trail = [(c >> 8) & 0xFF, c & 0xFF, 0]
                    self.on_frame(Frame(h[0], [], h, True, True))
                elif len(self.buf) >= 5 + length:
                    need = 5 + length
                    raw = self.buf[:need]
                    self.reset()
                    ok = crc16(raw[:need - 2]) == ((raw[need - 2] << 8) | raw[need - 1])
                    self.on_frame(Frame(raw[0], raw[3:need - 2], raw, ok))


# ---- levels -> labels (common/level_mapping_utils.dart; pickleball tables) ----
HORZ_DEG = [11, 10.5, 10, 9, 8, 7, 5, 3.5, 2, 0, 2, 3.5, 5, 7, 8, 9, 10, 10.5, 11]
SPEED_MPH = [5, 9, 13, 17, 21, 25, 29, 32, 35, 38, 41, 44, 47, 50, 54, 57, 60, 64, 67, 70]
FEED_SEC = [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 15, 20]
HEIGHT_FIRST = [6, 8, 10, 12, 14, 16]
RANGES = {"horz": (1, 19), "speed": (1, 20), "spin": (1, 19), "height": (1, 50), "feed": (1, 20)}
FIELDS = ["horz", "speed", "spin", "height", "feed"]


def height_deg(level: int) -> float:
    i = level - 1
    return HEIGHT_FIRST[i] if i < len(HEIGHT_FIRST) else 11 + i


def horz_signed_deg(level: int) -> float:
    """Signed horizontal angle: negative = left (level < 10), 0 = straight, positive = right."""
    d = HORZ_DEG[level - 1]
    return -d if level < 10 else d


def _num(x) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def label(fld: str, v: int) -> str:
    if fld == "horz":
        return "straight" if v == 10 else "%s° %s" % (_num(HORZ_DEG[v - 1]), "left" if v < 10 else "right")
    if fld == "speed":
        return "%s mph" % SPEED_MPH[v - 1]
    if fld == "spin":
        return "no spin" if v == 10 else "%d rpm %s" % (abs(v - 10) * 300, "backspin" if v < 10 else "topspin")
    if fld == "height":
        return "%s°" % _num(height_deg(v))
    if fld == "feed":
        return "%s s" % _num(FEED_SEC[v - 1])
    return str(v)


def shot_label(s: dict) -> str:
    return ", ".join("%s %s" % (f, label(f, s[f])) for f in FIELDS)


def _js_round(x: float) -> int:
    import math
    return int(math.floor(x + 0.5))


def clamp_shot(s: dict) -> dict:
    """Same as protocol.js clampShot. Only used where the web page used it; the CLI validates strictly instead."""
    out = {}
    for f in FIELDS:
        try:
            v = float(s.get(f))
            v = _js_round(v) if v == v and v not in (float("inf"), float("-inf")) else None
        except (TypeError, ValueError):
            v = None
        if v is None:
            v = RANGES[f][0]
        out[f] = min(RANGES[f][1], max(RANGES[f][0], v))
    return out


def validate_shot(s: dict) -> str | None:
    """Strict check: every field a whole number inside its range. Returns an error or None."""
    for f in FIELDS:
        v = s.get(f)
        if not isinstance(v, int) or isinstance(v, bool):
            return "%s must be a whole number" % f
        lo, hi = RANGES[f]
        if v < lo or v > hi:
            return "%s %d is out of range %d-%d" % (f, v, lo, hi)
    return None


# ---- drill encoding (save_drill_request.dart, model/ball.dart, model/myshot.dart) ----
def shot_bytes(s: dict) -> list[int]:
    s = clamp_shot(s)
    return [s[f] for f in FIELDS]


def ball_bytes(random: bool, loop: int, shots: list[dict]) -> list[int]:
    """Ball = [type 0 Regular / 1 Random][loop][nShots] + shots (5 bytes each)."""
    out = [1 if random else 0, loop, len(shots)]
    for s in shots:
        out += shot_bytes(s)
    return out


def save_drill_payload(number: int, name: str, balls: list[list[int]], machine_pos: int = 0,
                       shuffle: bool = False) -> list[int]:
    """[drillNo][nameLen][name][machinePos][shuffle][nBalls] + balls."""
    nm = []
    for ch in name:
        c = ord(ch)
        if c > 0x7E:
            raise ValueError("drill name must be ASCII")
        nm.append(c)
    out = [number, len(nm)] + nm + [machine_pos or 0, 1 if shuffle else 0, len(balls)]
    for b in balls:
        out += b
    return out


def ab_balls(A: list[dict], B: list[dict], pairs: int) -> list[list[int]]:
    balls = []
    for _ in range(pairs):
        balls.append(ball_bytes(True, 1, A))
        balls.append(ball_bytes(True, 1, B))
    return balls


def repeat_slots(slot: int, n: int) -> list[int]:
    return [slot] * max(1, n)


# ---- drill codes ----
def shot_code(s: dict) -> str:
    return ".".join(str(s[f]) for f in FIELDS)


def format_drill_code(groups: dict) -> str:
    return "A " + " ".join(shot_code(s) for s in groups["A"]) + " B " + " ".join(shot_code(s) for s in groups["B"])


_DIGITS = re.compile(r"[0-9]+")
_GROUP = re.compile(r"[abAB]")


def parse_shot(token: str, name: str = "shot") -> tuple[dict | None, str | None]:
    """Strict 'h.s.sp.ht.f'. Returns (shot, None) or (None, error)."""
    parts = token.split(".")
    if len(parts) != len(FIELDS) or not all(_DIGITS.fullmatch(p) for p in parts):
        return None, '%s "%s" must be 5 whole numbers like horz.speed.spin.height.feed' % (name, token)
    shot = {}
    for f, p in zip(FIELDS, parts):
        v = int(p)
        lo, hi = RANGES[f]
        if v < lo or v > hi:
            return None, "%s %s %d is out of range %d-%d" % (name, f, v, lo, hi)
        shot[f] = v
    return shot, None


def parse_drill_code(text) -> dict:
    """Strict: exactly 3 shots per group, whole numbers, each in range. Never clamps, never partial."""
    def fail(msg):
        return {"ok": False, "error": msg}
    toks = [t for t in str("" if text is None else text).strip().split() if t]
    if not toks:
        return fail("code is empty")
    groups = {"A": [], "B": []}
    seen = set()
    cur = None
    for t in toks:
        if _GROUP.fullmatch(t):
            cur = t.upper()
            if cur in seen:
                return fail("group %s appears twice" % cur)
            seen.add(cur)
            continue
        if not cur:
            return fail('code must start with A or B, found "%s"' % t)
        name = "%s%d" % (cur, len(groups[cur]) + 1)
        if len(groups[cur]) >= 3:
            return fail('group %s has more than 3 shots (extra: "%s")' % (cur, t))
        shot, err = parse_shot(t, "shot " + name)
        if err:
            return fail(err)
        groups[cur].append(shot)
    for g in ("A", "B"):
        if g not in seen:
            return fail("group %s is missing" % g)
        if len(groups[g]) != 3:
            return fail("group %s has %d shots, need exactly 3" % (g, len(groups[g])))
    return {"ok": True, "groups": groups}


# ---- requests ----
def get_version() -> bytes:
    return build_frame(FUNC["VERSION"])


def get_status() -> bytes:
    return build_frame(FUNC["STATUS"])


def control(cmd: str) -> bytes:
    return build_frame(FUNC["CONTROL"], [CONTROL[cmd]])


def set_current(nums: list[int]) -> bytes:
    return build_frame(FUNC["SET_CURRENT"], nums)


def exec_manual(param: int, value: int) -> bytes:
    return build_frame(FUNC["EXEC_MANUAL"], [param, value])


def save_drill(number: int, name: str, balls: list[list[int]], machine_pos: int = 0, shuffle: bool = False) -> bytes:
    return build_frame(FUNC["SAVE_DRILL"], save_drill_payload(number, name, balls, machine_pos, shuffle))


def ball_test(random: bool, loop: int, shots: list[dict]) -> bytes:
    """BallTest 0x04 (request/ball_test_request.dart): payload is one Ball record, same bytes as Ball.toBytes:
    [btype index][loop][nShots] + shots, each shot in MyShot.toBytes order."""
    return build_frame(FUNC["BALL_TEST"], ball_bytes(random, loop, shots))


def verify(kind: int, code: str = "") -> bytes:
    """VerifyCode: [kind][hasCode][6 ASCII digits][0]. kind 0 = automatic, 1 = typed by the user."""
    p = [kind, 1 if code else 0, 0, 0, 0, 0, 0, 0, 0]
    if code:
        for i, ch in enumerate(code[:6]):
            p[2 + i] = ord(ch)
    return build_frame(FUNC["VERIFY"], p)


# ---- reply parsing ----
@dataclass
class Version:
    text: str
    major: int | None
    minor: int | None
    patch: int | None
    mode: str
    mode_name: str
    needs_pin: bool = field(default=False)


def parse_version(raw) -> Version:
    s = "".join(chr(raw[i]) for i in range(3, min(17, len(raw))))

    def d(k):
        return int(s[k]) if k < len(s) and s[k].isdigit() and s[k] in "0123456789" else None
    mode = s[10] if len(s) > 10 else ""
    mode_name = "pickle" if mode in ("P", "Q") else "tennis"
    v = Version(s, d(5), d(7), d(9), mode, mode_name)
    v.needs_pin = (mode_name == "pickle" and v.major is not None and v.minor is not None
                   and v.major >= 7 and v.minor >= 4)
    return v


def parse_verify(frame: Frame) -> str:
    c = frame.payload[0] if frame.payload else None
    return "ok" if c == 0 else "needPin" if c == 2 else "failed"


def parse_status(frame: Frame) -> dict:
    c = frame.payload[0] if frame.payload else None
    if c is None:
        return {"code": None, "name": "UNKNOWN(empty)", "extra": []}
    name = STATUS_NAMES[c] if c < len(STATUS_NAMES) else "UNKNOWN(%d)" % c
    return {"code": c, "name": name, "extra": frame.payload[1:]}
