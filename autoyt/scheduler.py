"""Light polling loop: the day's batches at their planned times, whenever the laptop is on, with retries.

Each tick starts at most ONE batch, so a laptop that was off for hours drains its overdue batches one per
poll instead of all at once. Between batches it does the cheap daily chores (view stats, comments,
gameplay library upkeep).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from .config import Settings
from .db import Database
from .kinds import FINISHED, next_due_batch, plan_day
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


def chores(s: Settings, db: Database, runner) -> None:
    """Cheap background work while no batch is due. Every part is best effort."""
    svc_fn = getattr(runner, "_svc", None)
    day = today(s)
    if s.mode != "dry_run" and svc_fn is not None:
        from . import stats
        try:
            if db.kv_get("stats_day") != day:
                stats.fetch(s, db, svc_fn())
                db.kv_set("stats_day", day)
            stats.post_due_comments(s, db, svc_fn())
        except Exception as e:  # noqa: BLE001
            log.warning("stats/comments skipped: %s", e)
    if db.kv_get("library_day") != day and svc_fn is not None:
        from . import assets
        db.kv_set("library_day", day)  # once a day, even if it fails
        try:
            assets.ensure_library(s)
        except Exception as e:  # noqa: BLE001
            log.warning("gameplay library upkeep skipped: %s", e)


def tick(s: Settings, db: Database, runner, now: float | None = None) -> str:
    now = time.time() if now is None else now
    day = today(s)
    plan = plan_day(s, db, day=day, now=now)
    if all((r := db.get_run(b.key)) and r["status"] in FINISHED for b in plan):
        chores(s, db, runner)
        return "idle"
    ok, why = preconditions(s)
    if not ok:
        log.info("not starting a batch: %s", why)
        return f"blocked: {why}"
    if float(db.kv_get("bot_block_until", "0") or 0) > now:
        return "blocked: youtube bot check (backing off)"
    batch = next_due_batch(s, db, day=day, now=now)
    if batch is None:
        chores(s, db, runner)
        return "waiting"
    log.info("starting batch %s (%d clips, mode=%s)", batch.key, batch.clips, s.mode)
    return runner.attempt(batch.key)


def daemon(s: Settings) -> None:
    try:
        os.nice(10)  # the laptop's own work always wins the CPU
    except OSError:
        pass
    try:  # and the disk: idle-class I/O for ffmpeg, yt-dlp and Whisper
        os.system(f"ionice -c 3 -p {os.getpid()} >/dev/null 2>&1")
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
