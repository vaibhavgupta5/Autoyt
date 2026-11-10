import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from autoyt.config import DEFAULTS, Settings, load_settings
from autoyt.db import Database
from autoyt.pipeline import Runner, clips_target, slot_gap_minutes
from autoyt.scheduler import battery_state, preconditions
from autoyt.subtitles import build_ass, clip_relative_words, group_words, wrap_heading

LAY = DEFAULTS["layout"]


def test_wrap_heading():
    assert wrap_heading("He said the quiet part out loud") == ["He said the quiet", "part out loud"]
    assert len(wrap_heading("a " * 80)) == 3


def test_group_words_limits():
    ws = [{"w": f"w{i}", "s": i * 0.3, "e": i * 0.3 + 0.2} for i in range(10)]
    assert all(len(g) <= 3 for g in group_words(ws))


def test_ass_contains_heading_from_frame_one_and_ordered_words(words):
    rel = clip_relative_words(words, 10.0, 62.0)
    ass = build_ass(rel, "Heading here", "Some Show", 52.0, LAY)
    assert "Heading here" in ass and "via Some Show" in ass
    assert "Dialogue: 1,0:00:00.00,0:00:52.00,Head" in ass
    starts = [l.split(",")[1] for l in ass.splitlines() if l.startswith("Dialogue: 2,")]
    assert starts == sorted(starts) and len(starts) > 100


def test_ass_escapes_braces():
    ass = build_ass([{"w": "{evil}", "s": 0.0, "e": 0.5}], "Head {x}", "", 5.0, LAY)
    assert "{evil}" not in ass and "(EVIL)" in ass


def _settings(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("AUTOYT_HOME", str(tmp_path))
    monkeypatch.setenv("MIN_FREE_DISK_GB", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return load_settings()


def test_slot_gap():
    s = Settings(home=None, cfg=DEFAULTS)  # type: ignore[arg-type]
    assert slot_gap_minutes(s, 8) == 171
    assert slot_gap_minutes(s, 3) == 180
    assert slot_gap_minutes(s, 30) == 45


def test_slots_increase_and_future(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    db = Database(tmp_path / "t.db")
    r = Runner(s, db, sleep=lambda x: None)
    slots = [r.next_slot(8) for _ in range(8)]
    assert slots == sorted(slots) and len(set(slots)) == 8
    first = datetime.strptime(slots[0], "%Y-%m-%dT%H:%M:%S.000Z")
    assert first > datetime.utcnow() + timedelta(minutes=25)  # noqa: DTZ003
    span = datetime.strptime(slots[-1], "%Y-%m-%dT%H:%M:%S.000Z") - first
    assert span < timedelta(hours=24)


def test_ramp_up(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, CLIPS_PER_DAY=8)
    db = Database(tmp_path / "t.db")
    assert clips_target(s, db) == 3
    today = datetime.now(ZoneInfo(s.tz)).date()
    db.kv_set("first_upload_day", (today - timedelta(days=8)).isoformat())
    assert clips_target(s, db) == 5
    db.kv_set("first_upload_day", (today - timedelta(days=20)).isoformat())
    assert clips_target(s, db) == 8
    s2 = _settings(tmp_path, monkeypatch, CLIPS_PER_DAY=6, RAMP_UP="false")
    assert clips_target(s2, db) == 6


def test_battery_and_preconditions(tmp_path, monkeypatch):
    bat = tmp_path / "ps" / "BAT0"
    bat.mkdir(parents=True)
    (bat / "capacity").write_text("20\n")
    (bat / "status").write_text("Discharging\n")
    s = _settings(tmp_path, monkeypatch, POWER_SUPPLY_DIR=tmp_path / "ps", MIN_BATTERY_PERCENT=40)
    assert battery_state(s.power_dir) == (20, True)
    ok, why = preconditions(s)
    assert not ok and "battery" in why
    (bat / "status").write_text("Charging\n")
    assert preconditions(s)[0]
    (bat / "status").write_text("Discharging\n")
    (bat / "capacity").write_text("80\n")
    assert preconditions(s)[0]
    s.data_dir.mkdir(exist_ok=True)
    s.pause_file.write_text("")
    assert preconditions(s) == (False, "paused")


def test_no_battery_is_fine(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, POWER_SUPPLY_DIR=tmp_path / "nothing")
    assert preconditions(s)[0]
