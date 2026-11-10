"""Light polling loop: once a day, whenever the laptop is on, with retries."""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from .config import Settings
from .db import Database
from .pipeline import Runner, today

log = logging.getLogger("autoyt.scheduler")


def battery_state(power_dir: str) -> tuple[int | None, bool]:
    """(capacity percent or None if no battery, discharging?)"""
    base = Path(power_dir)
    if not base.exists():
        return None, False
    for bat in sorted(base.glob("BAT*")):
        try:
            cap = int((bat / "capacity").read_text().strip())
            status = (bat / "status").read_text().strip().lower()
            return cap, status == "discharging"
        except (OSError, ValueError):
            continue
    return None, False


def preconditions(s: Settings) -> tuple[bool, str]:
    if s.paused or s.pause_file.exists():
        return False, "paused"
    cap, discharging = battery_state(s.power_dir)
    if cap is not None and discharging and cap < s.min_battery:
        return False, f"battery {cap}% < {s.min_battery}% and not charging"
    s.data_dir.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(s.data_dir).free / 1e9
    if free_gb < s.min_free_disk_gb:
        return False, f"only {free_gb:.0f} GB free (< {s.min_free_disk_gb:.0f})"
    return True, "ok"


def tick(s: Settings, db: Database, runner: Runner) -> str:
    day = today(s)
    run = db.get_run(day)
    if run and run["status"] in ("done", "failed", "skipped"):
        return "idle"
    if run and run["next_try_at"] and run["next_try_at"] > time.time():
        return "waiting"
    ok, why = preconditions(s)
    if not ok:
        log.info("not starting today's run: %s", why)
        return f"blocked: {why}"
    log.info("starting run for %s (mode=%s)", day, s.mode)
    return runner.attempt(day)


def daemon(s: Settings) -> None:
    try:
        os.nice(10)
    except OSError:
        pass
    db = Database(s.db_path)
    runner = Runner(s, db)
    log.info("daemon started: mode=%s, poll every %d min, tz=%s", s.mode, s.poll_minutes, s.tz)
    # a run left 'running' by a crash or reboot must be picked up again
    for r in db.query("SELECT day FROM runs WHERE status='running'"):
        db.update_run(r["day"], status="pending", next_try_at=None)
    while True:
        try:
            tick(s, db, runner)
        except Exception:  # noqa: BLE001
            log.exception("tick crashed; will try again next poll")
        time.sleep(s.poll_minutes * 60)
