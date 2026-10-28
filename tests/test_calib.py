import json

from titan import cli
from titan import protocol as P

SHOT = {"horz": 10, "speed": 9, "spin": 11, "height": 21, "feed": 6}


def test_zero_offset_is_identity_for_every_level():
    for lv in range(1, 20):
        assert cli.adjust_field("horz", lv, 0.0)[0] == lv
        assert cli.adjust_field("horz", lv, 1e-12)[0] == lv
    for lv in range(1, 51):
        assert cli.adjust_field("height", lv, 0.0)[0] == lv
        assert cli.adjust_field("height", lv, 1e-12)[0] == lv


def test_horz_signed_conversion():
    assert P.horz_signed_deg(1) == -11 and P.horz_signed_deg(10) == 0 and P.horz_signed_deg(19) == 11
    assert cli.adjust_field("horz", 10, 2.0)[0] == 11      # 0 -> +2 (2° right)
    assert cli.adjust_field("horz", 10, -2.0)[0] == 9      # 0 -> -2 (2° left)
    assert cli.adjust_field("horz", 9, 2.0)[0] == 10       # 2° left + 2 = straight
    assert cli.adjust_field("horz", 12, -5.5)[0] == 9      # 3.5 right - 5.5 = 2 left
    assert cli.adjust_field("horz", 10, 1.0)[0] == 10      # tie (0 vs 2): stays nearest the original
    assert cli.adjust_field("horz", 10, 1.1)[0] == 11
    assert cli.adjust_field("horz", 18, 0.5)[0] == 19      # 10.5 -> 11
    assert cli.adjust_field("horz", 19, 0.25)[0] == 19     # 11.25 still in range


def test_height_conversion():
    assert cli.adjust_field("height", 21, 1.0)[0] == 22    # 31° -> 32°
    assert cli.adjust_field("height", 21, -3.0)[0] == 18   # 31 -> 28
    assert cli.adjust_field("height", 7, -2.0)[0] == 6     # 17 -> 15: between 14 (lv5) and 16 (lv6), tie -> nearer orig
    assert cli.adjust_field("height", 3, 2.0)[0] == 4      # 10 -> 12
    assert cli.adjust_field("height", 50, 0.5)[0] == 50


def test_out_of_range_refused_never_clamped():
    new, target, err = cli.adjust_field("horz", 19, 1.0)
    assert new is None and "outside" in err
    assert cli.adjust_field("horz", 1, -0.5)[0] is None
    assert cli.adjust_field("height", 1, -2.0)[0] is None   # 6 - 2 = 4°
    assert cli.adjust_field("height", 50, 1.0)[0] is None   # 61°
    shot, notes, err = cli.apply_calib(dict(SHOT, horz=19), {"horz_deg": 3.0, "height_deg": 0.0}, "A1")
    assert shot is None and err.startswith("A1: horz level 19")


def test_apply_calib_notes():
    shot, notes, err = cli.apply_calib(SHOT, {"horz_deg": 2.0, "height_deg": -1.0}, "shot")
    assert err is None and shot == dict(SHOT, horz=11, height=20)
    assert any("horz: 10 (straight) -> 11 (2° right)" in n for n in notes)
    assert any("height: 21 (31°) -> 20 (30°)" in n for n in notes)
    shot, notes, err = cli.apply_calib(SHOT, {"horz_deg": 0.0, "height_deg": 0.0}, "shot")
    assert shot == SHOT and notes == []


def test_calib_cli_set_show_reset(tmp_path, capsys):
    rd = str(tmp_path)
    assert cli.main(["--runs-dir", rd, "calib", "set", "--horz-deg", "1.5"]) == 0
    assert json.loads((tmp_path / "calibration.json").read_text()) == {"horz_deg": 1.5, "height_deg": 0.0}
    assert cli.main(["--runs-dir", rd, "calib", "set", "--height-deg", "-2"]) == 0
    assert cli.load_calib(rd) == {"horz_deg": 1.5, "height_deg": -2.0}
    assert cli.main(["--runs-dir", rd, "calib", "show"]) == 0
    assert "horz +1.5°, height -2°" in capsys.readouterr().out
    assert cli.main(["--runs-dir", rd, "calib", "reset"]) == 0
    assert cli.load_calib(rd) == {"horz_deg": 0.0, "height_deg": 0.0}


def test_fire_and_drill_refused_by_calib_or_cap_before_contacting_daemon(tmp_path, capsys):
    rd = str(tmp_path)  # no daemon here: a refusal must happen before any socket use
    cli.save_calib(rd, {"horz_deg": 3.0, "height_deg": 0.0})
    assert cli.main(["--runs-dir", rd, "fire", "19.9.11.21.6"]) == 1
    assert "REFUSED: shot: horz level 19" in capsys.readouterr().out
    code = "A 19.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11"
    assert cli.main(["--runs-dir", rd, "drill", code]) == 1
    assert "REFUSED: A1: horz level 19" in capsys.readouterr().out
    assert cli.main(["--runs-dir", rd, "fire", "10.15.11.21.6", "--raw"]) == 1
    assert "above the safety cap 14 (50 mph)" in capsys.readouterr().out
    assert cli.main(["--runs-dir", rd, "fire", "10.21.11.21.6"]) == 1   # out of range, strict parse
    assert "speed 21 is out of range" in capsys.readouterr().out


def test_non_finite_calibration_rejected(tmp_path, capsys):
    rd = str(tmp_path)
    for v in ("nan", "inf", "-inf"):
        assert cli.main(["--runs-dir", rd, "calib", "set", "--horz-deg=" + v]) == 1
        assert "finite" in capsys.readouterr().out
    assert not (tmp_path / "calibration.json").exists()
    assert cli.adjust_field("horz", 10, float("nan"))[0] is None


def test_bad_calibration_file_refuses_fire_and_drill(tmp_path, capsys):
    rd = str(tmp_path)
    code = "A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11"
    for content in ('{"horz_deg": NaN, "height_deg": 0}', '{"horz_deg": Infinity}', '{"horz_deg": "2"}',
                    "not json", "[1, 2]"):
        (tmp_path / "calibration.json").write_text(content)
        assert cli.main(["--runs-dir", rd, "fire", "10.9.11.21.6"]) == 1
        assert "REFUSED: calibration file" in capsys.readouterr().out
        assert cli.main(["--runs-dir", rd, "drill", code]) == 1
        assert "REFUSED: calibration file" in capsys.readouterr().out
        assert cli.main(["--runs-dir", rd, "calib", "show"]) == 1
        assert "calib reset" in capsys.readouterr().out
        cli.main(["--runs-dir", rd, "status"])
        assert "calibration: INVALID" in capsys.readouterr().out
    assert cli.main(["--runs-dir", rd, "calib", "reset"]) == 0
    assert cli.load_calib(rd) == {"horz_deg": 0.0, "height_deg": 0.0}
