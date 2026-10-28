"""Daemon end to end with the fake machine, over the real unix socket."""
import asyncio
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from titan import cli, client
from titan import protocol as P
from titan.daemon import Daemon
from titan.fake import FakeTransport

ROOT = Path(__file__).resolve().parent.parent
F = P.FUNC
CODE = "A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11"
CTRL_START = (F["CONTROL"], [0])
CTRL_STOP = (F["CONTROL"], [2])


def short_runs_dir():
    # unix socket paths are limited to ~104 bytes on macOS, so keep this short (runs/ is gitignored)
    (ROOT / "runs").mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="t-", dir=ROOT / "runs"))


class Harness:
    def __init__(self, write_delay=0.0, pin=None, daemon_pin=None, initial_status=2, drills=0):
        self.runs = short_runs_dir()
        self.fake = FakeTransport(write_delay=write_delay, pin=pin, drills=drills)
        self.fake.status = initial_status
        self.out = io.StringIO()
        self.d = Daemon(self.fake, self.runs, pin=daemon_pin, keyboard=False, out=self.out, poll_interval=0.2,
                        fake=True)
        self.loop = asyncio.new_event_loop()
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        t0 = time.time()
        while not (self.d.ready and self.d.connect_count >= 1):
            assert time.time() - t0 < 5, "daemon did not connect: " + self.out.getvalue()
            time.sleep(0.02)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.d.run())

    def call(self, req, timeout=30):
        return client.send(self.runs, req, timeout)

    def cli(self, *argv):
        return cli.main(["--runs-dir", str(self.runs)] + list(argv))

    def frames(self, skip=(F["STATUS"], F["VERSION"], F["VERIFY"], F["CRC_LIST"])):
        return [(f, p) for f, p in list(self.fake.frames) if f not in skip]

    def clear(self):
        self.call({"cmd": "fake", "clear": True})

    def close(self):
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.d.shutdown("test end")))
        self.th.join(10)
        shutil.rmtree(self.runs, ignore_errors=True)


@pytest.fixture
def h():
    # 22 stored drills: slot 23 (fire --via drill's scratch slot) is the next free one, as the machine requires
    hh = Harness(drills=22)
    yield hh
    hh.close()


def test_status_and_socket_permissions(h, capsys):
    assert oct(os.stat(h.runs / "titan.sock").st_mode & 0o777) == "0o600"
    assert h.cli("status") == 0
    out = capsys.readouterr().out
    assert "connected: yes" in out and "TITAN7.3.1P" in out and "calibration: horz +0°" in out


def test_drill_sends_savedrill_setcurrent_start_in_order(h, capsys):
    h.clear()
    assert h.cli("drill", CODE) == 0
    out = capsys.readouterr().out
    assert "1. SaveDrill" in out and "3. Control START: reply payload [00]" in out
    g = P.parse_drill_code(CODE)["groups"]
    assert h.frames() == [
        (F["SAVE_DRILL"], P.save_drill_payload(23, "AB RANDOM", P.ab_balls(g["A"], g["B"], 2))),
        (F["SET_CURRENT"], [23]),
        CTRL_START,
    ]
    h.clear()
    assert h.cli("drill", CODE, "--pairs", "1", "--repeat", "3", "--slot", "5") == 0
    assert h.frames()[1] == (F["SET_CURRENT"], [5, 5, 5])
    assert h.frames()[0][1][:7] == [5, 9] + list(b"AB RANDOM")[:5]


def slot_of(h):
    sd = [p for f, p in h.frames() if f == F["SAVE_DRILL"]]
    return sd[-1][0] if sd else None


def test_slot_is_next_free_then_reused(capsys):
    hh = Harness(drills=16)
    try:
        assert hh.cli("drill", CODE) == 0
        assert slot_of(hh) == 17 and hh.fake.drills == 17
        assert "machine holds 16 drills; using slot 17 (next free)" in capsys.readouterr().out
        assert json.loads((hh.runs / "drill_slot.json").read_text()) == {"slot": 17}
        hh.clear()
        assert hh.cli("drill", CODE) == 0          # our slot is <= N now: overwritten, the list does not grow
        assert slot_of(hh) == 17 and hh.fake.drills == 17
        assert hh.frames()[1] == (F["SET_CURRENT"], [17])
    finally:
        hh.close()


def test_saved_slot_beyond_machine_count_is_ignored():
    hh = Harness(drills=3)
    try:
        (hh.runs / "drill_slot.json").write_text(json.dumps({"slot": 9}))   # machine was wiped since
        assert hh.cli("drill", CODE) == 0
        assert slot_of(hh) == 4
    finally:
        hh.close()


def test_explicit_slot_must_be_existing_or_next(capsys):
    hh = Harness(drills=16)
    try:
        assert hh.cli("drill", CODE, "--slot", "23") == 1
        assert "slot 23 is not available" in capsys.readouterr().out and hh.frames() == []
        assert hh.cli("drill", CODE, "--slot", "5") == 0     # an existing slot is overwritten
        assert slot_of(hh) == 5 and hh.fake.drills == 16
    finally:
        hh.close()


def test_full_machine_refused_unless_slot_is_ours(capsys):
    hh = Harness(drills=24)
    try:
        assert hh.cli("drill", CODE) == 1
        assert "already holds 24 drills" in capsys.readouterr().out and hh.frames() == []
        (hh.runs / "drill_slot.json").write_text(json.dumps({"slot": 20}))
        assert hh.cli("drill", CODE) == 0
        assert slot_of(hh) == 20
    finally:
        hh.close()


def test_save_to_non_contiguous_slot_fails_the_sequence():
    """The fake mirrors the machine: slot 23 on a 16-drill machine gets no reply and swallows the next frame."""
    hh = Harness(drills=16)
    try:
        g = P.parse_drill_code(CODE)["groups"]
        r = hh.call({"cmd": "fire", "shot": g["A"][0]})     # fire --via drill saves to scratch slot 23
        assert not r["ok"] and "STOPPED at step 1" in r["error"] and "no reply" in r["error"]
        assert hh.fake.drills == 16 and F["SET_CURRENT"] not in hh.fake.funcs()
    finally:
        hh.close()


def test_half_left_mirrors_horz(h):
    h.clear()
    assert h.cli("drill", CODE, "--half", "left") == 0
    g = P.parse_drill_code(CODE)["groups"]
    A = [P.mirror_shot(s) for s in g["A"]]
    B = [P.mirror_shot(s) for s in g["B"]]
    assert [s["horz"] for s in A + B] == [20 - s["horz"] for s in g["A"] + g["B"]]
    assert h.frames()[0] == (F["SAVE_DRILL"], P.save_drill_payload(23, "AB RANDOM", P.ab_balls(A, B, 2)))
    assert P.mirror_shot({"horz": 1, "speed": 9, "spin": 10, "height": 20, "feed": 5})["horz"] == 19
    assert P.mirror_shot({"horz": 10, "speed": 9, "spin": 10, "height": 20, "feed": 5})["horz"] == 10


def test_out_ball_layout_and_checks(h, capsys):
    h.clear()
    out = "15.10.10.20.9"
    assert h.cli("drill", CODE, "--out-ball", out) == 0
    g = P.parse_drill_code(CODE)["groups"]
    o = P.parse_shot(out)[0]
    balls = P.ab_balls(g["A"], g["B"], 4, o)
    assert len(balls) == 8
    assert h.frames()[0] == (F["SAVE_DRILL"], P.save_drill_payload(23, "AB RANDOM", balls))
    assert balls[7] == P.ball_bytes(True, 1, [g["B"][0], g["B"][2], o])          # 4th B: B1, B3, OUT
    assert balls[1] == balls[3] == balls[5] == P.ball_bytes(True, 1, g["B"])     # other B balls untouched
    assert "about 1 in 12 B balls" in capsys.readouterr().out
    # not a multiple of 3: pairs = round(N/3), real rate 1 in 3 * pairs
    assert P.out_pairs(12) == 4 and P.out_pairs(9) == 3 and P.out_pairs(10) == 3 and P.out_pairs(3) == 1
    # the out ball is mirrored and speed-checked like every other shot
    h.clear()
    assert h.cli("drill", CODE, "--out-ball", out, "--half", "left") == 0
    assert h.frames()[0][1][-5:] == [5, 10, 10, 20, 9]       # OUT is the last shot of the last ball: 20 - 15
    h.clear()
    assert h.cli("drill", CODE, "--out-ball", "15.18.10.20.9") == 1
    assert "OUT speed level 18" in capsys.readouterr().out and h.frames() == []
    assert h.cli("drill", CODE, "--out-ball", out, "--pairs", "2") == 1
    r = h.call({"cmd": "drill", "A": g["A"], "B": g["B"], "out": dict(o, speed=18)})
    assert not r["ok"] and "safety cap" in r["error"]


def test_fire_via_drill_encodes(h, capsys):
    h.clear()
    assert h.cli("fire", "10.9.11.21.6") == 0
    shot = {"horz": 10, "speed": 9, "spin": 11, "height": 21, "feed": 6}
    assert h.frames() == [
        (F["SAVE_DRILL"], P.save_drill_payload(23, "FIRE", [P.ball_bytes(False, 1, [shot])])),
        (F["SET_CURRENT"], [23]),
        CTRL_START,
    ]
    # slot 23, name "FIRE", pos 0, no shuffle, 1 ball: Regular, loop 1, 1 shot
    assert h.frames()[0][1] == [23, 4, 70, 73, 82, 69, 0, 0, 1, 0, 1, 1, 10, 9, 11, 21, 6]
    out = capsys.readouterr().out
    assert "1. SaveDrill slot 23" in out and "2. SetCurrentDrill [23]: reply payload [00]" in out


def test_fire_via_balltest_encodes(h, capsys):
    h.clear()
    assert h.cli("fire", "3.7.10.21.5", "--via", "balltest") == 0
    assert h.frames() == [(F["BALL_TEST"], [0, 1, 1, 3, 7, 10, 21, 5]), CTRL_START]
    assert "1. BallTest" in capsys.readouterr().out


def test_fire_applies_calibration(h, capsys):
    cli.save_calib(h.runs, {"horz_deg": 2.0, "height_deg": 1.0})
    h.clear()
    assert h.cli("fire", "10.9.11.21.6", "--via", "balltest") == 0
    out = capsys.readouterr().out
    assert "horz: 10 (straight) -> 11 (2° right)" in out and "height: 21 (31°) -> 22 (32°)" in out
    assert h.frames()[0] == (F["BALL_TEST"], [0, 1, 1, 11, 9, 11, 22, 6])
    h.clear()
    assert h.cli("fire", "10.9.11.21.6", "--via", "balltest", "--raw") == 0
    assert h.frames()[0] == (F["BALL_TEST"], [0, 1, 1, 10, 9, 11, 21, 6])


def test_speed_cap_refusal(h, capsys):
    h.clear()
    assert h.cli("fire", "10.15.11.21.6") == 1
    assert "above the safety cap" in capsys.readouterr().out
    assert h.cli("drill", CODE.replace("12.10.12.17.6", "12.18.12.17.6")) == 1
    assert h.frames() == []
    # the daemon enforces the cap too, for any client
    r = h.call({"cmd": "fire", "shot": {"horz": 10, "speed": 15, "spin": 10, "height": 20, "feed": 5}})
    assert not r["ok"] and "safety cap" in r["error"]
    r = h.call({"cmd": "fire", "shot": {"horz": 10, "speed": 21, "spin": 10, "height": 20, "feed": 5},
                "allow_fast": True})
    assert not r["ok"] and "out of range" in r["error"]
    assert h.frames() == []
    assert h.cli("fire", "10.15.11.21.6", "--allow-fast", "--via", "balltest") == 0
    assert h.frames()[0] == (F["BALL_TEST"], [0, 1, 1, 10, 15, 11, 21, 6])


def _run_bg(fn):
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn()))
    t.start()
    return t, box


def test_stop_during_slow_savedrill_write_never_sends_start(h, capsys):
    h.call({"cmd": "fake", "write_delay": 0.25, "clear": True})
    t, box = _run_bg(lambda: h.call({"cmd": "drill", "A": P.parse_drill_code(CODE)["groups"]["A"],
                                     "B": P.parse_drill_code(CODE)["groups"]["B"]}))
    time.sleep(0.6)   # SaveDrill (5 chunks x 0.25 s) is half written
    assert 0 < len(h.fake.chunks) and not any(f == F["SAVE_DRILL"] for f, _ in h.fake.frames)
    assert h.cli("stop") == 0
    t.join(10)
    r = box["r"]
    assert not r["ok"] and "cancelled" in r["error"]
    time.sleep(1.0)
    fr = h.frames()
    assert CTRL_STOP in fr
    assert CTRL_START not in fr
    assert all(f not in (F["SAVE_DRILL"], F["SET_CURRENT"]) for f, _ in fr)  # half-written frame abandoned
    log = next(h.runs.glob("session-*.log")).read_text()
    assert "abandoned after" in log


def test_stop_while_waiting_for_savedrill_reply_never_sends_start(h):
    h.call({"cmd": "fake", "no_reply": [F["SAVE_DRILL"]], "clear": True})
    t, box = _run_bg(lambda: h.call({"cmd": "fire", "shot": {"horz": 10, "speed": 9, "spin": 11, "height": 21,
                                                             "feed": 6}}))
    time.sleep(0.5)
    assert h.call({"cmd": "stop"})["ok"]
    t.join(10)
    assert not box["r"]["ok"]
    time.sleep(1.0)
    fr = h.frames()
    assert [f for f, _ in fr] == [F["SAVE_DRILL"], F["CONTROL"]] and fr[-1] == CTRL_STOP


def test_pause_cancels_sequence(h):
    h.call({"cmd": "fake", "no_reply": [F["SET_CURRENT"]], "clear": True})
    t, box = _run_bg(lambda: h.call({"cmd": "fire", "shot": {"horz": 10, "speed": 9, "spin": 11, "height": 21,
                                                             "feed": 6}}))
    time.sleep(0.5)
    r = h.call({"cmd": "pause"})
    assert r["ok"], r
    t.join(10)
    time.sleep(0.5)
    assert CTRL_START not in h.frames() and (F["CONTROL"], [1]) in h.frames()


def test_stop_with_every_write_hung_prints_loud_failure(h, capsys):
    h.call({"cmd": "fake", "hang": True, "clear": True})
    t0 = time.time()
    assert h.cli("stop") == 1
    took = time.time() - t0
    out = capsys.readouterr().out
    assert "STOP NOT SENT - USE MACHINE POWER BUTTON" in out
    assert "STOP NOT CONFIRMED - USE MACHINE POWER BUTTON" in out
    assert took < 8
    window = h.out.getvalue()
    assert "#####  STOP NOT SENT - USE MACHINE POWER BUTTON  #####" in window
    assert "STOP NOT CONFIRMED SENT AFTER" in window   # early alarm at 1.5 s
    h.call({"cmd": "fake", "hang": False})


def test_stop_retried_once_when_no_reply(h, capsys):
    h.call({"cmd": "fake", "no_reply": [F["CONTROL"]], "clear": True})
    assert h.cli("stop") == 1
    out = capsys.readouterr().out
    assert "STOP WRITTEN BUT MACHINE DID NOT REPLY" in out
    assert h.frames() == [CTRL_STOP, CTRL_STOP]


def test_disconnect_is_loud_and_reconnects(h):
    h.loop.call_soon_threadsafe(h.fake.drop_link)
    time.sleep(0.3)
    assert "BLUETOOTH DISCONNECTED" in h.out.getvalue()
    r = h.call({"cmd": "fire", "shot": {"horz": 10, "speed": 9, "spin": 11, "height": 21, "feed": 6}})
    assert not r["ok"] and "not connected" in r["error"]
    t0 = time.time()
    while not h.d.connected:
        assert time.time() - t0 < 8
        time.sleep(0.1)
    assert "reconnecting" in h.out.getvalue()


def test_pin_flow():
    hh = Harness(pin="123456", daemon_pin="123456")
    try:
        assert hh.d.pin_state == "PIN ok"
        assert (hh.runs / "pin").read_text().strip() == "123456"
        assert oct(os.stat(hh.runs / "pin").st_mode & 0o777) == "0o600"
    finally:
        hh.close()


def test_subprocess_daemon_and_cli_end_to_end():
    runs = short_runs_dir()
    py = str(ROOT / ".venv" / "bin" / "python")
    if not os.path.exists(py):
        py = sys.executable
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    d = subprocess.Popen([py, "-m", "titan", "--runs-dir", str(runs), "daemon", "--fake", "--no-keyboard"],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        def run(*a):
            return subprocess.run([py, "-m", "titan", "--runs-dir", str(runs)] + list(a), cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=30)
        t0 = time.time()
        while run("status").returncode != 0:
            assert time.time() - t0 < 10
            time.sleep(0.2)
        r = run("drill", CODE, "--pairs", "1", "--repeat", "1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "3. Control START" in r.stdout
        r = run("fire", "10.16.11.21.6")
        assert r.returncode == 1 and "safety cap" in r.stdout
        r = run("log", "-n", "200")
        assert r.returncode == 0 and "SaveDrill" in r.stdout and "GetRobotStatus" not in r.stdout
        d.send_signal(signal.SIGTERM)
        out, _ = d.communicate(timeout=15)
        assert d.returncode == 0
        assert "shutting down (SIGTERM): sending STOP first" in out and "STOP sent" in out
        assert not (runs / "titan.sock").exists()
        r = run("status")
        assert r.returncode == 2 and "NOT RUNNING" in r.stdout
        r = run("stop")
        assert r.returncode == 2 and "STOP NOT SENT" in r.stdout
    finally:
        if d.poll() is None:
            d.kill()
        shutil.rmtree(runs, ignore_errors=True)


def test_keyboard_in_a_real_terminal_stop_pause_quit_and_restore():
    """Runs the daemon on a pseudo-terminal like Terminal.app: SPACE = STOP, p = PAUSE, q = STOP + quit."""
    import pty
    import termios
    runs = short_runs_dir()
    py = str(ROOT / ".venv" / "bin" / "python")
    if not os.path.exists(py):
        py = sys.executable
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    d = subprocess.Popen([py, "-m", "titan", "--runs-dir", str(runs), "daemon", "--fake"], cwd=ROOT, env=env,
                         stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    buf = bytearray()

    def wait_for(text, timeout=8, count=1):
        t0 = time.time()
        import select
        while buf.count(text.encode()) < count:
            assert time.time() - t0 < timeout, "did not see %r in:\n%s" % (text, buf.decode(errors="replace"))
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    buf.extend(os.read(master, 4096))
                except OSError:
                    break
    try:
        wait_for("CONNECTED to fake machine")
        wait_for("[CONNECTED] machine STOP")   # live status line
        os.write(master, b" ")
        wait_for("STOP pressed")
        wait_for("STOP sent (try 1)")
        os.write(master, b"p")
        wait_for("PAUSE sent")
        os.write(master, b"\r")
        wait_for("STOP sent (try 1)", 4, count=2)
        os.write(master, b"q")
        wait_for("shutting down (q pressed): sending STOP first")
        wait_for("daemon stopped.")
        assert d.wait(10) == 0
        after = termios.tcgetattr(slave)
        assert after[3] & termios.ICANON and after[3] & termios.ECHO   # terminal restored
        pendin = getattr(termios, "PENDIN", 0)  # kernel-managed "input pending" bit
        assert after[3] & ~pendin == before[3] & ~pendin
    finally:
        if d.poll() is None:
            d.kill()
        os.close(master)
        os.close(slave)
        shutil.rmtree(runs, ignore_errors=True)


def test_clients_never_import_bleak():
    code = ("import sys, titan.cli, titan.client, titan.protocol, titan.fake, titan.daemon; "
            "titan.cli.main(['--runs-dir', sys.argv[1], 'calib', 'show']); "
            "titan.cli.main(['--runs-dir', sys.argv[1], 'status']); "
            "assert 'bleak' not in sys.modules, 'bleak imported'")
    runs = short_runs_dir()
    try:
        r = subprocess.run([sys.executable, "-c", code, str(runs)], cwd=ROOT, capture_output=True, text=True,
                           timeout=30, env=dict(os.environ, PYTHONPATH=str(ROOT)))
        assert r.returncode == 0, r.stderr
    finally:
        shutil.rmtree(runs, ignore_errors=True)


def test_keyboard_pin_needed_ctrl_z_ctrl_s_and_ctrl_c():
    """Daemon on a pty that is its controlling terminal (like Terminal.app). PIN missing: loud PIN NEEDED, no prompt,
    STOP key stays live. Ctrl-Z does not suspend it, Ctrl-S does not freeze it, Ctrl-C STOPs and quits."""
    import fcntl
    import pty
    import select
    import termios
    runs = short_runs_dir()
    py = str(ROOT / ".venv" / "bin" / "python")
    if not os.path.exists(py):
        py = sys.executable
    master, slave = pty.openpty()

    def make_ctty():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    d = subprocess.Popen([py, "-m", "titan", "--runs-dir", str(runs), "daemon", "--fake", "--fake-pin", "123456"],
                         cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)), stdin=slave, stdout=slave,
                         stderr=slave, close_fds=True, preexec_fn=make_ctty)
    buf = bytearray()

    def wait_for(text, timeout=8, count=1):
        t0 = time.time()
        while buf.count(text.encode()) < count:
            assert time.time() - t0 < timeout, "did not see %r in:\n%s" % (text, buf.decode(errors="replace"))
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    buf.extend(os.read(master, 4096))
                except OSError:
                    break
    try:
        wait_for("#####  PIN NEEDED: run `.venv/bin/python -m titan pin NNNNNN`")
        wait_for("[CONNECTED] PIN NEEDED (titan pin NNNNNN) | machine STOP")   # status line keeps it visible
        assert b"Type the 6 digits" not in buf                                 # no interactive prompt
        assert not termios.tcgetattr(slave)[0] & termios.IXON                 # Ctrl-S cannot freeze output
        os.write(master, b"\x1a")                                              # Ctrl-Z
        time.sleep(0.3)
        os.write(master, b"\x13")                                              # Ctrl-S
        time.sleep(0.2)
        os.write(master, b" ")
        wait_for("STOP sent (try 1)")
        assert d.poll() is None
        r = subprocess.run([py, "-m", "titan", "--runs-dir", str(runs), "pin", "123456"], cwd=ROOT,
                           env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "VerifyCode: ok" in r.stdout
        wait_for("[CONNECTED] machine STOP")
        os.write(master, b"\x03")                                              # Ctrl-C
        wait_for("shutting down (SIGINT): sending STOP first")
        wait_for("daemon stopped.")
        assert d.wait(10) == 0
        # (terminal restore is checked in the other keyboard test; this pty is revoked once its session leader exits)
    finally:
        if d.poll() is None:
            d.kill()
        os.close(master)
        os.close(slave)
        shutil.rmtree(runs, ignore_errors=True)


def test_pin_needed_is_loud_persistent_and_cleared_by_pin_command(capsys):
    hh = Harness(pin="123456")
    try:
        assert hh.d.pin_needed and "PIN NEEDED" in hh.d._status_text()
        assert "#####  PIN NEEDED" in hh.out.getvalue()
        hh.cli("status")
        assert "PIN NEEDED: run" in capsys.readouterr().out
        assert hh.cli("pin", "000000") == 1 and hh.d.pin_needed
        assert hh.cli("pin", "123456") == 0
        assert not hh.d.pin_needed and "PIN NEEDED" not in hh.d._status_text()
        assert (hh.runs / "pin").read_text().strip() == "123456"
    finally:
        hh.close()


def test_already_running_machine_is_loud_on_connect():
    hh = Harness(initial_status=0)   # START
    try:
        assert "MACHINE IS ALREADY RUNNING (status START) - press SPACE to STOP" in hh.out.getvalue()
    finally:
        hh.close()
    hh = Harness()
    try:
        assert "ALREADY RUNNING" not in hh.out.getvalue()
    finally:
        hh.close()


def test_stop_reply_waiter_not_fooled_by_raced_start_reply(h):
    """START is mid-write (slow GATT write, machine already answered) when STOP arrives and waits for the write lock.
    START's reply lands during that wait; it must not be taken as the reply to STOP."""
    h.call({"cmd": "fake", "write_delay": 0.4, "reply_before_ack": True, "reply_delay": 0.15, "echo_control": True,
            "clear": True})
    t, box = _run_bg(lambda: h.call({"cmd": "start"}))
    time.sleep(0.08)                 # START chunk in flight; its reply arrives at ~0.15 s
    r = h.call({"cmd": "stop"})
    t.join(10)
    assert r["ok"], r
    assert "reply payload [00 02]" in r["lines"][-1]      # the STOP echo, not START's [00 00]
    assert not box["r"]["ok"]                              # START's own wait was cancelled by STOP


def test_allow_fast_and_counts_must_be_real_types(h):
    shot = {"horz": 10, "speed": 15, "spin": 10, "height": 20, "feed": 5}
    for v in ("yes", 1, "true"):
        r = h.call({"cmd": "fire", "shot": shot, "allow_fast": v})
        assert not r["ok"] and "safety cap" in r["error"]
    g = P.parse_drill_code(CODE)["groups"]
    for k in ("pairs", "repeat", "slot"):
        r = h.call({"cmd": "drill", "A": g["A"], "B": g["B"], k: True})
        assert not r["ok"] and k in r["error"]
    assert h.frames() == []


def test_client_timeout_message_mentions_sequence(monkeypatch, capsys, tmp_path):
    def boom(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(client, "send", boom)
    assert cli.main(["--runs-dir", str(tmp_path), "start"]) == 2
    out = capsys.readouterr().out
    assert "may still be running; check `status` or run `stop`" in out
