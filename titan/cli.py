"""Command line: `python -m titan <cmd>`. Every command except `daemon` is a thin socket client (no bleak)."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from . import protocol as P

PROJECT = Path(__file__).resolve().parent.parent
SPEED_CAP = 14  # same as daemon.SPEED_CAP (daemon enforces it too); not imported to keep clients light


def default_runs() -> Path:
    return Path(os.environ.get("TITAN_RUNS_DIR") or PROJECT / "runs")


# ---------------------------------------------------------------- calibration ----
HORZ_LEVELS = {lv: P.horz_signed_deg(lv) for lv in range(1, 20)}
HEIGHT_LEVELS = {lv: float(P.height_deg(lv)) for lv in range(1, 51)}
# A target further than half the end step beyond the table's extremes is out of range (refused, never clamped).
HORZ_LIMITS = (-11.25, 11.25)
HEIGHT_LIMITS = (5.0, 60.5)


class CalibError(Exception):
    pass


def load_calib(runs) -> dict:
    """No file = zero offsets. A file that is unreadable or holds a non-finite / non-numeric offset raises CalibError
    (fire/drill then refuse) instead of silently using zero."""
    path = Path(runs) / "calibration.json"
    if not path.exists():
        return {"horz_deg": 0.0, "height_deg": 0.0}
    try:
        d = json.loads(path.read_text())
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        out = {}
        for k in ("horz_deg", "height_deg"):
            v = d.get(k, 0.0)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError("%s is %r, need a finite number" % (k, v))
            out[k] = float(v)
        return out
    except (OSError, ValueError) as e:
        raise CalibError("calibration file %s is invalid (%s); fix it or run `calib reset`" % (path, e)) from None


def save_calib(runs, cal: dict):
    Path(runs).mkdir(parents=True, exist_ok=True)
    (Path(runs) / "calibration.json").write_text(json.dumps(cal, indent=1) + "\n")


def nearest_level(target: float, table: dict, orig: int) -> int:
    """Nearest level by degrees; ties go to the level closest to the original."""
    return min(table, key=lambda lv: (round(abs(table[lv] - target), 9), abs(lv - orig)))


def adjust_field(fld: str, level: int, offset: float):
    """Returns (new_level, target_deg, error)."""
    if fld == "horz":
        table, (lo, hi), unit = HORZ_LEVELS, HORZ_LIMITS, "horz"
    else:
        table, (lo, hi), unit = HEIGHT_LEVELS, HEIGHT_LIMITS, "height"
    if not math.isfinite(offset):
        return None, offset, "%s offset %r is not a finite number" % (fld, offset)
    if not offset:
        return level, table[level], None
    target = table[level] + offset
    if target < lo or target > hi:
        return None, target, "%s level %d (%s) + offset %+g° = %g° is outside the machine's range (%g° to %g°)" % (
            unit, level, P.label(fld, level), offset, target, table[min(table)], table[max(table)])
    return nearest_level(target, table, level), target, None


def apply_calib(shot: dict, cal: dict, name: str = "shot"):
    """Returns (adjusted_shot, notes, error). Never clamps."""
    out = dict(shot)
    notes = []
    for fld, key in (("horz", "horz_deg"), ("height", "height_deg")):
        new, target, err = adjust_field(fld, shot[fld], cal.get(key, 0.0))
        if err:
            return None, notes, "%s: %s" % (name, err)
        if new != shot[fld]:
            notes.append("calib %s %s: %d (%s) -> %d (%s), target %g°" % (
                name, fld, shot[fld], P.label(fld, shot[fld]), new, P.label(fld, new), target))
        out[fld] = new
    return out, notes, None


def calib_text(cal) -> str:
    return "calibration: horz %+g°, height %+g°" % (cal["horz_deg"], cal["height_deg"])


# ---------------------------------------------------------------- helpers ----
def _client():
    from . import client
    return client


def call(args, req, timeout=30.0) -> dict | None:
    c = _client()
    try:
        return c.send(args.runs_dir, req, timeout=timeout)
    except c.DaemonDown as e:
        print("ERROR: %s" % e)
        return None
    except (TimeoutError, OSError) as e:
        print("ERROR: no answer from the daemon (%s: %s). The sequence may still be running; check `status` or "
              "run `stop`." % (type(e).__name__, e))
        return None


def show(resp) -> int:
    if resp is None:
        return 2
    for ln in resp.get("lines", []) or []:
        print(ln)
    if resp.get("error") and resp["error"] not in (resp.get("lines") or []):
        print("ERROR: " + resp["error"])
    return 0 if resp.get("ok") else 1


def check_speed(shots, allow_fast) -> str | None:
    for nm, s in shots:
        if s["speed"] > SPEED_CAP and not allow_fast:
            return "%s speed level %d (%s) is above the safety cap %d (%s); add --allow-fast to send it" % (
                nm, s["speed"], P.label("speed", s["speed"]), SPEED_CAP, P.label("speed", SPEED_CAP))
    return None


def prepare(args, named_shots):
    """Calibrate (unless --raw) and apply the speed cap. Returns (shots, ok)."""
    out = []
    try:
        cal = load_calib(args.runs_dir)
    except CalibError as e:
        if not args.raw:
            print("REFUSED: %s" % e)
            return None
        cal = None
    if args.raw:
        print("raw levels (no calibration)")
    elif cal["horz_deg"] or cal["height_deg"]:
        print(calib_text(cal))
    for nm, s in named_shots:
        if not args.raw:
            adj, notes, err = apply_calib(s, cal, nm)
            for n in notes:
                print(n)
            if err:
                print("REFUSED: " + err)
                return None
            s = adj
        out.append((nm, s))
    err = check_speed(out, args.allow_fast)
    if err:
        print("REFUSED: " + err)
        return None
    return out


# ---------------------------------------------------------------- commands ----
def calib_status(runs) -> str:
    try:
        return calib_text(load_calib(runs))
    except CalibError as e:
        return "calibration: INVALID - %s" % e


def cmd_status(args) -> int:
    cal = calib_status(args.runs_dir)
    resp = call(args, {"cmd": "status"}, timeout=5.0)
    if resp is None:
        print("daemon: NOT RUNNING")
        print(cal)
        return 2
    d = resp.get("data", {})
    print("daemon: running (%s)" % d.get("transport"))
    print("connected: %s%s" % ("yes" if d.get("connected") else "NO", (" to " + d["device"]) if d.get("connected") else ""))
    print("version: %s   pin: %s" % (d.get("version"), d.get("pin")))
    print("machine status: %s" % d.get("machine_status"))
    print("last command: %s" % d.get("last_cmd"))
    if d.get("stream", "-") != "-":
        print("stream: %s" % d.get("stream"))
    if d.get("sequence_running"):
        print("a fire/drill sequence is running")
    if d.get("pin_needed"):
        print("PIN NEEDED: run `.venv/bin/python -m titan pin NNNNNN` with the digits on the machine screen")
    for a in d.get("alarms") or []:
        print("recent alarm: %s" % a)
    print(cal)
    return 0 if d.get("connected") else 1


def cmd_log(args) -> int:
    logs = sorted(Path(args.runs_dir).glob("session-*.log"))
    if not logs:
        print("no session log in %s" % args.runs_dir)
        return 1
    lines = logs[-1].read_text(errors="replace").splitlines()
    if not args.all:
        def poll(ln):
            return ("GetRobotStatus" in ln or "func=0x17" in ln
                    or (" RX~ " in ln and ("chunk 7e 17 " in ln or "chunk 17 " in ln)))
        lines = [ln for ln in lines if not poll(ln)]
    print("%s (last %d lines%s)" % (logs[-1], args.n, "" if args.all else ", status polls hidden"))
    for ln in lines[-args.n:]:
        print(ln)
    return 0


def cmd_stop(args) -> int:
    resp = call(args, {"cmd": "stop"}, timeout=10.0)
    if resp is None:
        print("STOP NOT SENT - PRESS SPACE IN THE DAEMON WINDOW OR USE MACHINE POWER BUTTON")
        return 2
    rc = show(resp)
    if rc:
        print("STOP NOT CONFIRMED - USE MACHINE POWER BUTTON")
    return rc


def cmd_simple(args) -> int:
    return show(call(args, {"cmd": args.cmd}))


def cmd_pin(args) -> int:
    return show(call(args, {"cmd": "pin", "code": args.code}))


def cmd_fire(args) -> int:
    shot, err = P.parse_shot(args.shot, "shot")
    if err:
        print("REFUSED: " + err)
        return 1
    prepared = prepare(args, [("shot", shot)])
    if prepared is None:
        return 1
    s = prepared[0][1]
    print("fire via %s: %s = %s" % (args.via, P.shot_code(s), P.shot_label(s)))
    return show(call(args, {"cmd": "fire", "shot": s, "via": args.via, "allow_fast": args.allow_fast}))


DEFAULT_CODE = "A 13.9.11.22.3 15.10.12.18.3 19.8.8.21.3 B 15.9.13.15.9 12.7.9.20.9 19.7.11.21.9"


def named_shots(args, extra=()):
    """Parse the code, mirror for --half left, return (named shots incl. extras, error)."""
    r = P.parse_drill_code(args.code)
    if not r["ok"]:
        return None, r["error"]
    named = [("%s%d" % (g, i + 1), s) for g in ("A", "B") for i, s in enumerate(r["groups"][g])] + list(extra)
    if args.half == "left":
        named = [(nm, P.mirror_shot(s)) for nm, s in named]
        print("left half: horz mirrored (20 - h)")
    return named, None


def cmd_drill(args) -> int:
    extra = []
    pairs = args.pairs
    if args.out_ball:
        out, err = P.parse_shot(args.out_ball, "--out-ball")
        if err:
            print("REFUSED: " + err)
            return 1
        if pairs is not None:
            print("REFUSED: --pairs and --out-ball conflict: --out-every sets the pairs")
            return 1
        if not 3 <= args.out_every <= 24:
            print("REFUSED: --out-every must be 3-24")
            return 1
        pairs = P.out_pairs(args.out_every)
        extra = [("OUT", out)]
    pairs = 2 if pairs is None else pairs
    named, err = named_shots(args, extra)
    if err:
        print("REFUSED: " + err)
        return 1
    prepared = prepare(args, named)
    if prepared is None:
        return 1
    A = [s for nm, s in prepared if nm.startswith("A")]
    B = [s for nm, s in prepared if nm.startswith("B")]
    out_s = next((s for nm, s in prepared if nm == "OUT"), None)
    print("drill slot %s, %d pairs, repeat %d: %s" % (args.slot or "auto", pairs, args.repeat,
                                                       P.format_drill_code({"A": A, "B": B})))
    if out_s:
        print("out ball %s in the last B ball: about 1 in %d B balls" % (P.shot_code(out_s), 3 * pairs))
    return show(call(args, {"cmd": "drill", "A": A, "B": B, "pairs": pairs, "repeat": args.repeat,
                            "slot": args.slot, "allow_fast": args.allow_fast, "out": out_s}, timeout=60.0))


def cmd_stream(args) -> int:
    named, err = named_shots(args)
    if err:
        print("REFUSED: " + err)
        return 1
    prepared = prepare(args, named)
    if prepared is None:
        return 1
    A = [s for nm, s in prepared if nm.startswith("A")]
    B = [s for nm, s in prepared if nm.startswith("B")]
    print("stream %d pairs: %s" % (args.pairs, P.format_drill_code({"A": A, "B": B})))
    return show(call(args, {"cmd": "stream", "A": A, "B": B, "pairs": args.pairs, "allow_fast": args.allow_fast}))


def cmd_raw(args) -> int:
    return show(call(args, {"cmd": "raw", "func": int(args.func, 0), "payload": args.payload, "timeout": args.timeout,
                       "unsafe": args.unsafe}))


def cmd_calib(args) -> int:
    for nm, v in (("--horz-deg", args.horz_deg), ("--height-deg", args.height_deg)):
        if v is not None and not math.isfinite(v):
            print("REFUSED: %s must be a finite number of degrees, got %r" % (nm, v))
            return 1
    if args.action == "reset":
        save_calib(args.runs_dir, {"horz_deg": 0.0, "height_deg": 0.0})
    try:
        cal = load_calib(args.runs_dir)
    except CalibError as e:
        print("ERROR: %s" % e)
        return 1
    if args.action == "reset":
        cal = {"horz_deg": 0.0, "height_deg": 0.0}
        save_calib(args.runs_dir, cal)
    elif args.action == "set":
        if args.horz_deg is None and args.height_deg is None:
            print("calib set needs --horz-deg and/or --height-deg")
            return 1
        if args.horz_deg is not None:
            cal["horz_deg"] = args.horz_deg
        if args.height_deg is not None:
            cal["height_deg"] = args.height_deg
        save_calib(args.runs_dir, cal)
    print(calib_text(cal) + "  (file %s)" % (Path(args.runs_dir) / "calibration.json"))
    print("horz: positive = further right; height: positive = higher. Applied to fire/drill unless --raw.")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m titan", description="Titan ACE controller")
    ap.add_argument("--runs-dir", type=Path, default=default_runs(), help="runtime dir (socket, logs, calibration)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="run the daemon (owner starts this in Terminal.app)")
    d.add_argument("--fake", action="store_true", help="simulated machine, no Bluetooth")
    d.add_argument("--fake-write-delay", type=float, default=0.0)
    d.add_argument("--fake-pin", default=None)
    d.add_argument("--address", default=None, help="BLE address/UUID to connect to (skips the scan)")
    d.add_argument("--pin", default=None, help="6-digit machine PIN")
    d.add_argument("--no-keyboard", action="store_true", help="do not read keys (tests)")
    d.add_argument("--poll", type=float, default=1.0, help="status poll interval, seconds")

    sub.add_parser("status")
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=40)
    lg.add_argument("--all", action="store_true", help="include status polls")
    for c in ("stop", "pause", "start"):
        sub.add_parser(c)
    pn = sub.add_parser("pin", help="send the machine PIN")
    pn.add_argument("code")

    f = sub.add_parser("fire", help="fire one ball: h.s.sp.ht.f")
    f.add_argument("shot")
    f.add_argument("--via", choices=["drill", "balltest"], default="drill",
                   help="drill: SaveDrill + SetCurrentDrill + START, in the same automatic slot as 'drill' (the drill "
                        "there is overwritten by this one-ball FIRE drill); balltest: BallTest + START, saves nothing")
    f.add_argument("--raw", action="store_true", help="ignore calibration")
    f.add_argument("--allow-fast", action="store_true", help="allow speed level above 14 (50 mph)")

    half = dict(choices=["right", "left"], default="right",
                help="drill codes are written for the right half; left mirrors horz as 20 - h (default right)")
    dr = sub.add_parser("drill", help='A/B random drill: "A h.s.sp.ht.f x3 B h.s.sp.ht.f x3"')
    dr.add_argument("code", nargs="?", default=DEFAULT_CODE, help="default: the code that worked on court 2026-10-28")
    dr.add_argument("--pairs", type=int, default=None, help="Random A + Random B pairs (default 2)")
    dr.add_argument("--repeat", type=int, default=1, help="slot repeated N times in SetCurrentDrill (default 1: the "
                    "saved drill loops natively on the machine)")
    dr.add_argument("--slot", type=int, default=None, help="override the automatic slot; must be <= drills stored + 1")
    dr.add_argument("--half", **half)
    dr.add_argument("--out-ball", metavar="CODE", help="a shot code for an out ball, written for the right half; the "
                    "last pair's B ball becomes Random of [B1, B3, OUT]")
    dr.add_argument("--out-every", type=int, default=12, help="with --out-ball, about 1 B ball in N is out (default "
                    "12 = 4 pairs, since the out ball is 1 of 3 in one B ball of N/3 pairs); pairs = round(N/3), so "
                    "a N that is not a multiple of 3 is approximate: the real rate is 1 in 3*pairs (3-24)")
    dr.add_argument("--raw", action="store_true")
    dr.add_argument("--allow-fast", action="store_true")

    st = sub.add_parser("stream", help="A/B drill fired ball by ball from the laptop (no SaveDrill)")
    st.add_argument("code", nargs="?", default=DEFAULT_CODE)
    st.add_argument("--pairs", type=int, default=20)
    st.add_argument("--half", **half)
    st.add_argument("--raw", action="store_true")
    st.add_argument("--allow-fast", action="store_true")

    rw = sub.add_parser("raw", help="debug: send one frame (func, hex payload) and show the reply")
    rw.add_argument("func")
    rw.add_argument("payload", nargs="?", default="")
    rw.add_argument("--timeout", type=float, default=3.0)
    rw.add_argument("--unsafe", action="store_true", help="allow frames that can start the machine or change drills")

    cb = sub.add_parser("calib", help="aim offsets applied to fire/drill")
    cb.add_argument("action", choices=["show", "set", "reset"])
    cb.add_argument("--horz-deg", type=float, default=None)
    cb.add_argument("--height-deg", type=float, default=None)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "daemon":
        from . import daemon
        return daemon.main(args, args.runs_dir)
    handlers = {"status": cmd_status, "log": cmd_log, "stop": cmd_stop, "pause": cmd_simple, "start": cmd_simple,
                "fire": cmd_fire, "drill": cmd_drill, "calib": cmd_calib, "pin": cmd_pin, "stream": cmd_stream, "raw": cmd_raw}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
