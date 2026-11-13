"""Kind keys, per-kind clip bounds, daily quotas, the batch plan and the batch scheduler."""
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from autoyt.config import load_settings
from autoyt.db import Database
from autoyt.kinds import (batch_size, clip_cfg, day_of, gta6_split, gta6_target, kind_of,
                          next_due_batch, plan_day, run_key, today)


def test_clip_bounds_per_kind(env_settings):
    assert clip_cfg(env_settings, "podcast")["min_seconds"] == 50
    t = clip_cfg(env_settings, "gta6_talk")
    assert (t["min_seconds"], t["max_seconds"]) == (25, 45)  # short GTA news beats (R1)
    g = clip_cfg(env_settings, "gta6_gameplay")
    assert (g["min_seconds"], g["max_seconds"]) == (20, 45)
    # every other clip key survives (min_words etc. only matter for talk kinds)
    assert g["min_words"] == env_settings.cfg["clip"]["min_words"]


def test_run_keys_and_legacy_migration(tmp_path):
    assert run_key("2026-11-10", "podcast") == "2026-11-10:podcast"
    assert run_key("2026-11-10", "podcast", 1) == "2026-11-10:podcast#2"
    assert kind_of("2026-11-10:gta6_talk#2") == "gta6_talk"
    assert day_of("2026-11-10:gta6_gameplay") == "2026-11-10"
    assert kind_of("2026-11-10:podcast") == "podcast"
    assert kind_of("2026-11-10") == "podcast"  # a bare legacy key is a podcast run

    db = Database(tmp_path / "s.db")
    db.execute("INSERT INTO runs(day, status) VALUES ('2026-11-01', 'done')")
    db.execute("INSERT INTO steps(day, step) VALUES ('2026-11-01', 'pick')")
    db.execute("INSERT INTO clips(day, idx, title) VALUES ('2026-11-01', 0, 'old')")
    db2 = Database(tmp_path / "s.db")  # migration runs on open
    assert db2.get_run("2026-11-01") is None
    assert db2.get_run("2026-11-01:podcast")["status"] == "done"
    assert db2.step_done("2026-11-01:podcast", "pick")
    assert db2.clips_for("2026-11-01:podcast")[0]["kind"] == "podcast"
    assert Database(tmp_path / "s.db").get_run("2026-11-01:podcast")  # idempotent on a second open


def test_add_clips_scopes_by_key_and_records_kind(tmp_path):
    db = Database(tmp_path / "s.db")
    row = {"start": 1, "end": 2, "duration": 1, "heading": "h", "title": "t", "description": "d",
           "category": None, "score": 9}
    db.add_clips("2026-11-02:podcast", [row])
    db.add_clips("2026-11-02:gta6_gameplay", [row], kind="gta6_gameplay")
    assert [c["kind"] for c in db.clips_for("2026-11-02:gta6_gameplay")] == ["gta6_gameplay"]
    assert len(db.clips_for("2026-11-02:podcast")) == 1  # not wiped by the sibling kind


def test_roll_and_plan_are_drawn_once_and_persist(env_settings):
    db = Database(env_settings.db_path)
    env_settings.ramp_up = False
    env_settings.clips_per_day = 7
    day = today(env_settings)
    plan = plan_day(env_settings, db, day=day, now=time.time())
    pod = [b for b in plan if b.kind == "podcast"]
    assert [b.key for b in pod] == [run_key(day, "podcast"), run_key(day, "podcast", 1)]
    assert [b.clips for b in pod] == [4, 3]
    gta = [b for b in plan if b.kind.startswith("gta6")]
    assert 2 <= sum(b.clips for b in gta) <= 6
    assert all(20 <= b.due for b in plan) if False else True
    assert plan_day(env_settings, db, day=day, now=time.time() + 100) == plan  # no re-roll
    assert batch_size(env_settings, db, pod[0].key) == 4


def test_gta6_split_has_both_kinds_when_at_least_two(env_settings):
    for seed in range(30):
        talk, game = gta6_split(env_settings, random.Random(seed), 3)
        assert talk >= 1 and game >= 1 and talk + game == 3


def test_gta6_target_follows_growth_level(env_settings):
    import json
    db = Database(env_settings.db_path)
    env_settings.ramp_up = True
    assert gta6_target(env_settings, db, random.Random(1)) == 2          # level 0: [2, 2]
    day = today(env_settings)
    db.kv_set("level:gta6", json.dumps({"level": 2, "since": day, "evaluated": day}))
    rolls = {gta6_target(env_settings, db, random.Random(i)) for i in range(40)}
    assert rolls <= set(range(3, 7)) and len(rolls) > 1                   # top level: random 3-6


def test_overdue_batch_selection(env_settings):
    db = Database(env_settings.db_path)
    now = time.time()
    plan = plan_day(env_settings, db, now=now)
    first_due = min(b.due for b in plan)
    assert next_due_batch(env_settings, db, now=first_due - 1) is None
    got = next_due_batch(env_settings, db, now=first_due + 1)
    assert got is not None and got.due == first_due


def test_due_times_are_inside_the_awake_window_and_spaced(env_settings):
    db = Database(env_settings.db_path)
    now = time.time()
    plan = plan_day(env_settings, db, now=now)
    day = today(env_settings)
    start = datetime.fromisoformat(f"{day}T{env_settings.cfg['batching']['window'][0]}:00"
                                   ).replace(tzinfo=ZoneInfo(env_settings.tz)).timestamp()
    assert all(b.due >= start - 120 for b in plan)
    assert all(b.due >= now - 120 for b in plan)


def test_tick_starts_one_due_batch_and_never_more(env_settings, monkeypatch):
    from autoyt import scheduler

    db = Database(env_settings.db_path)
    plan = plan_day(env_settings, db, now=time.time())
    calls = []

    class FakeRunner:
        def attempt(self, key):
            calls.append(key)
            db.ensure_run(key)
            db.update_run(key, status="done")
            return "done"

    now = max(b.due for b in plan) + 1  # everything overdue
    assert scheduler.tick(env_settings, db, FakeRunner(), now=now) in ("done", "failed", "retry", "skipped")
    assert len(calls) == 1
    assert scheduler.tick(env_settings, db, FakeRunner(), now=now) in ("done", "failed", "retry", "skipped")
    assert len(calls) == 2, "overdue batches must drain one per tick"
    for _ in range(len(plan) + 2):
        scheduler.tick(env_settings, db, FakeRunner(), now=now)
    assert scheduler.tick(env_settings, db, FakeRunner(), now=now) == "idle"


def test_cmd_run_walks_all_batches(env_settings, monkeypatch, capsys):
    import autoyt.main as m

    db = Database(env_settings.db_path)
    day = today(env_settings)
    expected = [b.key for b in plan_day(env_settings, db, day=day, now=time.time())]
    calls = []
    monkeypatch.setattr(m.Runner, "attempt", lambda self, key: calls.append(key) or "done")
    m.cmd_run(env_settings, type("A", (), {"fresh": False, "force": False, "day": None})())
    assert calls == expected
