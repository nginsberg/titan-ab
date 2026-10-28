import json
import shutil
import subprocess
from pathlib import Path

import pytest

from titan import protocol as P

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CODE = "A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11"


def sh(a):
    return dict(zip(P.FIELDS, a))


def parse_all(chunks):
    got = []
    p = P.FrameParser(got.append)
    for c in chunks:
        p.feed(c)
    return got


# ---- known vectors ----
def test_vectors():
    assert P.hexs(P.get_version()) == "7e 18 00 00 ea c2"
    assert P.hexs(P.control("START")) == "7e 01 00 01 00 45 85"
    assert P.hexs(P.exec_manual(1, 7)) == "7e 03 00 02 01 07 c3 64"
    assert "%x" % P.crc16(b"123456789") == "31c3"


def test_escaping():
    f = list(P.build_frame(2, [0x7E, 0x7D]))
    assert P.hexs(f[:8]) == "7e 02 00 02 7d 5e 7d 5d"
    assert 0x7E not in f[1:]


def test_crc_bytes_7e_7d_escaped_and_round_trip():
    found = {}
    for b in range(256):
        for c in range(256):
            crc = P.raw_frame(2, [b, c])[-2:]
            for v in (0x7E, 0x7D):
                if v in crc and v not in found:
                    found[v] = [b, c]
        if len(found) == 2:
            break
    for pl in found.values():
        wire = P.build_frame(2, pl)
        assert 0x7E not in wire[1:]
        got = parse_all([wire])
        assert len(got) == 1 and got[0].crc_ok and got[0].payload == pl


A2 = [sh([6, 5, 10, 26, 5]), sh([14, 5, 10, 26, 5])]
B2 = [sh([3, 7, 10, 21, 5]), sh([17, 7, 12, 21, 5])]
EXPECT_PAYLOAD = ("05 02 41 42 00 00 02 "
                  "01 01 02 06 05 0a 1a 05 0e 05 0a 1a 05 "
                  "01 01 02 03 07 0a 15 05 11 07 0c 15 05")


def test_save_drill_payload():
    assert P.hexs(P.save_drill_payload(5, "AB", P.ab_balls(A2, B2, 1))) == EXPECT_PAYLOAD
    p = P.save_drill_payload(5, "AB", P.ab_balls(A2, B2, 1), machine_pos=2, shuffle=True)
    assert p[4:7] == [2, 1, 2]
    with pytest.raises(ValueError):
        P.save_drill_payload(5, "é", [])
    s3 = [sh([1 + i, 5, 10, 20, 5]) for i in range(3)]
    pl = P.save_drill_payload(24, "AB RANDOM", P.ab_balls(s3, s3, 2))
    assert len(pl) == 14 + 4 * 18 and pl[-1] == 5


def test_ball_test_payload_is_one_ball_record():
    shot = sh([10, 9, 11, 21, 6])
    f = P.ball_test(False, 1, [shot])
    got = parse_all([f])
    assert got[0].func == 0x04 and got[0].crc_ok
    assert got[0].payload == [0, 1, 1, 10, 9, 11, 21, 6]
    assert got[0].payload == P.ball_bytes(False, 1, [shot])


# ---- parser ----
def test_parser_round_trips():
    wire = P.save_drill(5, "AB", P.ab_balls(A2, B2, 1))
    got = parse_all(P.chunk(wire, 20))
    assert len(got) == 1 and got[0].crc_ok and got[0].func == 8 and P.hexs(got[0].payload) == EXPECT_PAYLOAD
    w = list(P.build_frame(2, [24, 24])) + list(P.build_frame(0x17, []))
    got = parse_all([[b] for b in w])
    assert [g.func for g in got] == [2, 0x17]
    got = parse_all([P.build_frame(0x17, [0])[1:]])
    assert len(got) == 1 and got[0].crc_ok
    w = bytearray(P.build_frame(0x17, [0]))
    w[-1] ^= 0xFF
    got = parse_all([w])
    assert len(got) == 1 and not got[0].crc_ok


def test_parser_zero_length_and_unescape():
    got = parse_all([[0x7E, 0x01, 0x00, 0x00]])
    assert len(got) == 1 and got[0].func == 1 and got[0].payload == [] and got[0].crc_ok and got[0].no_crc
    assert [g.func for g in parse_all([[0x20, 0, 0]])] == [0x20]
    assert len(parse_all([[0x7E, 0x17], [0x00], [0x00]])) == 1
    w = list(P.build_frame(0x17, [])) + [0x18, 0x00, 0x00]
    assert [g.func for g in parse_all([w])] == [0x17, 0x18]
    st = list(P.get_status())
    assert len(st) == 6 and P.hexs(st[:4]) == "7e 17 00 00" and ((st[4] << 8) | st[5]) == P.crc16([0x17, 0, 0])
    raw = P.raw_frame(2, [0x00, 0x7E])
    wire = [0x7E]
    for b in raw:
        wire += [0x7D, 0x20] if b == 0 else [0x7D, 0x5E] if b == 0x7E else [b]
    got = parse_all([wire])
    assert len(got) == 1 and got[0].crc_ok and got[0].payload == [0x00, 0x7E]


# ---- drill codes ----
def test_parse_default_code_and_round_trip():
    r = P.parse_drill_code(DEFAULT_CODE)
    assert r["ok"]
    assert r["groups"]["A"][0] == sh([10, 9, 11, 21, 6]) and r["groups"]["B"][2] == sh([13, 7, 11, 21, 11])
    assert P.format_drill_code(r["groups"]) == DEFAULT_CODE
    g = {"A": [sh(x) for x in ([1, 1, 1, 1, 1], [19, 20, 19, 50, 20], [10, 5, 10, 20, 5])],
         "B": [sh(x) for x in ([3, 7, 10, 21, 5], [17, 7, 12, 21, 5], [10, 10, 10, 10, 10])]}
    text = P.format_drill_code(g)
    assert text == "A 1.1.1.1.1 19.20.19.50.20 10.5.10.20.5 B 3.7.10.21.5 17.7.12.21.5 10.10.10.10.10"
    assert P.parse_drill_code(text)["groups"] == g
    r = P.parse_drill_code("\n a 10.9.11.21.6\t12.10.12.17.6   14.8.8.20.6\nb 10.9.13.15.11 7.7.9.20.11\r\n13.7.11.21.11 \n")
    assert P.format_drill_code(r["groups"]) == DEFAULT_CODE
    r = P.parse_drill_code("B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11 A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6")
    assert P.format_drill_code(r["groups"]) == DEFAULT_CODE


REJECTS = [
    ("A 10.9.11.21.6 12.10.12.17.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11", "group A has 2 shots"),
    ("A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11", "group B has 2 shots"),
    (DEFAULT_CODE.replace("12.10.12", "12.21.12"), "shot A2 speed 21 is out of range 1-20"),
    (DEFAULT_CODE.replace("13.7.11.21.11", "13.7.11.51.11"), "shot B3 height 51 is out of range 1-50"),
    (DEFAULT_CODE.replace("13.7.11.21.11", "13.7.11.21.0"), "shot B3 feed 0 is out of range 1-20"),
    (DEFAULT_CODE.replace("A 10.9", "A 20.9"), "shot A1 horz 20"),
    (DEFAULT_CODE.replace("B 10.9.13", "B 10.9.20"), "shot B1 spin 20"),
    (DEFAULT_CODE.replace("10.9.11.21.6", "10.9.11.21.6.5"), "5 whole numbers"),
    (DEFAULT_CODE.replace("12.10", "12.x"), "shot A2"),
    (DEFAULT_CODE.replace("13.7.11.21.11", "13.7.11.-1.11"), "shot B3"),
    (DEFAULT_CODE + " 13.7.11.21.11", "more than 3 shots"),
    ("A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6", "group B is missing"),
    ("B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11", "group A is missing"),
    ("10.9.11.21.6 A 10.9.11.21.6", "must start with A or B"),
    ("A 1.1.1.1.1 1.1.1.1.1 1.1.1.1.1 A 1.1.1.1.1", "appears twice"),
    ("", "empty"),
    ("hello", "must start with A or B"),
    (DEFAULT_CODE.replace("10.9.11.21.6", "10.9.11.21.٦"), "5 whole numbers"),  # non-ASCII digit
]


@pytest.mark.parametrize("text,msg", REJECTS)
def test_code_rejections(text, msg):
    r = P.parse_drill_code(text)
    assert not r["ok"] and "groups" not in r and msg in r["error"]


# ---- requests / parsers / labels ----
def test_verify_set_current_version_status_labels():
    assert P.hexs(P.raw_frame(0x20, [1, 1, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0])[:12]) == \
        "20 00 09 01 01 31 32 33 34 35 36 00"
    assert P.verify(1, "123456") == bytes([0x7E] + P.escape_bytes(P.raw_frame(0x20, [1, 1] + list(b"123456") + [0])))
    assert P.repeat_slots(24, 1) == [24] and len(P.repeat_slots(24, 10)) == 10
    assert P.hexs(P.set_current([24, 24])[:6]) == "7e 02 00 02 18 18"

    def ver(s):
        return P.parse_version([0x18, 0, 14] + list(s.encode()))
    v = ver("TITAN7.4.1P\0\0\0")
    assert (v.major, v.minor, v.patch, v.mode, v.needs_pin) == (7, 4, 1, "P", True)
    assert not ver("TITAN7.3.1P\0\0\0").needs_pin
    assert ver("TITAN7.4.1Q\0\0\0").needs_pin
    assert not ver("TITAN7.4.1D\0\0\0").needs_pin and ver("TITAN7.4.1D\0\0\0").mode_name == "tennis"
    fr = lambda pl: P.Frame(0, pl, [], True)  # noqa: E731
    assert [P.parse_verify(fr([c])) for c in (0, 1, 2)] == ["ok", "failed", "needPin"]
    assert P.parse_status(fr([2, 5]))["name"] == "STOP" and P.parse_status(fr([7]))["name"] == "ALARM"
    L = P.label
    assert (L("horz", 3), L("horz", 10), L("horz", 19)) == ("10° left", "straight", "11° right")
    assert (L("speed", 7), L("speed", 1), L("speed", 20)) == ("29 mph", "5 mph", "70 mph")
    assert (L("spin", 10), L("spin", 1), L("spin", 19)) == ("no spin", "2700 rpm backspin", "2700 rpm topspin")
    assert (L("height", 1), L("height", 21), L("height", 50)) == ("6°", "31°", "60°")
    assert (L("feed", 5), L("feed", 18), L("feed", 20)) == ("3 s", "9.5 s", "20 s")
    assert P.clamp_shot({"horz": 99, "speed": 0, "spin": 10.4, "height": "x", "feed": 5}) == \
        {"horz": 19, "speed": 1, "spin": 10, "height": 1, "feed": 5}


# ---- cross-check against protocol.js (node) ----
NODE_SCRIPT = r"""
const P = require(process.argv[1]);
const code = process.argv[2];
const g = P.parseDrillCode(code).groups;
const drill = { number: 24, name: 'AB RANDOM', machinePos: 0, shuffle: false, balls: P.abBalls(g.A, g.B, 2) };
const out = {
  getVersion: P.hex(P.requests.getVersion()),
  getStatus: P.hex(P.requests.getStatus()),
  start: P.hex(P.requests.control('START')),
  stop: P.hex(P.requests.control('STOP')),
  pause: P.hex(P.requests.control('PAUSE')),
  setCurrent: P.hex(P.requests.setCurrent(new Array(10).fill(24))),
  saveDrill: P.hex(P.requests.saveDrill(drill)),
  verify1: P.hex(P.requests.verify(1, '123456')),
  verify0: P.hex(P.requests.verify(0, '')),
  escaped: P.hex(P.buildFrame(2, [0x7e, 0x7d, 0x00])),
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_cross_check_byte_for_byte():
    res = subprocess.run(["node", "-e", NODE_SCRIPT, str(ROOT / "protocol.js"), DEFAULT_CODE],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    js = json.loads(res.stdout)
    g = P.parse_drill_code(DEFAULT_CODE)["groups"]
    py = {
        "getVersion": P.hexs(P.get_version()),
        "getStatus": P.hexs(P.get_status()),
        "start": P.hexs(P.control("START")),
        "stop": P.hexs(P.control("STOP")),
        "pause": P.hexs(P.control("PAUSE")),
        "setCurrent": P.hexs(P.set_current([24] * 10)),
        "saveDrill": P.hexs(P.save_drill(24, "AB RANDOM", P.ab_balls(g["A"], g["B"], 2))),
        "verify1": P.hexs(P.verify(1, "123456")),
        "verify0": P.hexs(P.verify(0, "")),
        "escaped": P.hexs(P.build_frame(2, [0x7E, 0x7D, 0x00])),
    }
    assert py == js
