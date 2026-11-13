"""Kinds (podcast / GTA6 talk / GTA6 gameplay): run keys, daily quotas and the batch plan.

A run is keyed ``"<day>:<kind>"`` with an optional ``#<n>`` suffix for a second batch of the same
kind on the same day. The composite key is stored in the existing TEXT ``day`` columns, so every
``WHERE day=?`` query keeps working and legacy rows are migrated once to ``<day>:podcast``.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database

log = logging.getLogger("autoyt.kinds")

KINDS = ("podcast", "gta6_talk", "gta6_gameplay")
KIND_LABEL = {"podcast": "podcast", "gta6_talk": "GTA6 talk", "gta6_gameplay": "GTA6 gameplay"}
FINISHED = ("done", "failed", "skipped")


def today(s: Settings) -> str:
    return datetime.now(ZoneInfo(s.tz)).strftime("%Y-%m-%d")


def day_of(key: str) -> str:
    return key.split(":", 1)[0]


def kind_of(key: str) -> str:
    """Kind of a run key. A bare legacy day (``2026-11-01``) is a podcast run."""
    if ":" not in key:
        return "podcast"
    return key.split(":", 1)[1].split("#", 1)[0].split("~", 1)[0]


def run_key(day: str, kind: str, batch_idx: int = 0) -> str:
    """``batch_idx`` is zero-based: 0 -> ``day:kind``, 1 -> ``day:kind#2``."""
    return f"{day}:{kind}" + (f"#{batch_idx + 1}" if batch_idx else "")


def clip_cfg(s: Settings, kind: str) -> dict:
    """The global clip settings with this kind's overrides applied."""
    base = dict(s.cfg["clip"])
    base.update(s.cfg["kinds"].get(kind, {}).get("clip", {}))
    return base


# ------------------------------------------------------------------ growth levels
GROUPS = {"podcast": ("podcast",), "gta6": ("gta6_talk", "gta6_gameplay")}


def _levels(s: Settings, group: str) -> list:
    g = s.cfg["growth"]
    return list(g["podcast_levels"] if group == "podcast" else g["gta6_levels"])


def _recent_strike(db: Database, days: float = 7) -> bool:
    cutoff = time.time() - days * 86400
    return db.one("SELECT 1 FROM disabled_sources WHERE at>?", (cutoff,)) is not None


def growth_level(s: Settings, db: Database, group: str, day: str | None = None) -> int:
    """Current volume level of a kind group, re-evaluated at most once a day from real view counts.

    Up a level: 7-day median of 72-hour views >= thresholds[level], with enough samples and at least
    `min_days_at_level` days on the current level. Down a level: median under a third of the threshold
    that earned the current level. A strike or claim in the last 7 days holds everything.
    """
    levels = _levels(s, group)
    if not s.ramp_up:
        return len(levels) - 1
    day = day or today(s)
    g = s.cfg["growth"]
    key = f"level:{group}"
    try:
        st = json.loads(db.kv_get(key) or "{}")
    except ValueError:
        st = {}
    lvl = max(0, min(len(levels) - 1, int(st.get("level", 0))))
    since = st.get("since") or day
    if st.get("evaluated") == day:
        return lvl
    from .stats import median_views
    med, n = median_views(db, GROUPS[group])
    thr = [float(x) for x in g["thresholds"]]
    held = (date.fromisoformat(day) - date.fromisoformat(since)).days
    new = lvl
    if _recent_strike(db):
        pass
    elif (n >= int(g["min_samples"]) and lvl < len(levels) - 1 and lvl < len(thr)
          and held >= int(g["min_days_at_level"]) and med >= thr[lvl]):
        new = lvl + 1
    elif lvl > 0 and n >= int(g["min_samples"]) and med < thr[lvl - 1] / 3:
        new = lvl - 1
    if new != lvl:
        log.info("growth: %s level %d -> %d (median 72 h views %.0f over %d clips)", group, lvl, new, med, n)
        since = day
    db.kv_set(key, json.dumps({"level": new, "since": since, "evaluated": day}))
    return new


def clips_target(s: Settings, db: Database, day: str | None = None) -> int:
    """Podcast clips per day: the growth level, capped by CLIPS_PER_DAY."""
    levels = _levels(s, "podcast")
    return max(1, min(s.clips_per_day, int(levels[growth_level(s, db, "podcast", day)])))


def gta6_target(s: Settings, db: Database, rng: random.Random, day: str | None = None) -> int:
    """GTA videos per day: a random count inside the current level's [min, max]."""
    lo, hi = (int(x) for x in _levels(s, "gta6")[growth_level(s, db, "gta6", day)])
    return rng.randint(lo, max(lo, hi))


def shift_quota(s: Settings, db: Database, n_pod: int, n_gta: int) -> tuple[int, int]:
    """Move up to `max_shift` daily slots toward the kind group whose median views are `shift_ratio`x the other's."""
    from .stats import median_views
    g = s.cfg["growth"]
    pm, pn = median_views(db, GROUPS["podcast"])
    gm, gn = median_views(db, GROUPS["gta6"])
    if min(pn, gn) < int(g["min_samples"]):
        return n_pod, n_gta
    ratio, k = float(g["shift_ratio"]), int(g["max_shift"])
    if gm >= ratio * max(pm, 1.0):
        move = min(k, max(0, n_pod - 1))
        return n_pod - move, n_gta + move
    if pm >= ratio * max(gm, 1.0):
        move = min(k, max(0, n_gta - 1))
        return n_pod + move, n_gta - move
    return n_pod, n_gta


def gta6_split(s: Settings, rng: random.Random, total: int) -> tuple[int, int]:
    """Split a day's GTA6 count into (talk, gameplay). Both kinds run once there are >= 2."""
    if total <= 0:
        return 0, 0
    weight = float(s.cfg["gta6"].get("talk_weight", 0.5))
    if total == 1:
        return (1, 0) if rng.random() < weight else (0, 1)
    talk = max(1, min(total - 1, round(total * weight)))
    return talk, total - talk


@dataclass
class Batch:
    key: str
    kind: str
    clips: int
    due: float


def _at(s: Settings, day: str, hhmm: str) -> float:
    h, m = (int(x) for x in str(hhmm).split(":"))
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(s.tz)).timestamp()


def _due_times(s: Settings, day: str, n: int, rng: random.Random, now: float) -> list[float]:
    """Random start times inside the awake window, at least ``min_gap_hours`` apart when it fits."""
    bcfg = s.cfg["batching"]
    start, end = _at(s, day, bcfg["window"][0]), _at(s, day, bcfg["window"][1])
    if end <= now:               # window already gone (machine off, or a plan drawn late)
        start, end = now + 60, now + 3600
    elif start < now:            # mid-window: use what is left
        start = min(now, end - 60)
    span = max(60.0, end - start)
    if n <= 1:
        return [start + span * rng.uniform(0.2, 0.8)]
    gap = min(float(bcfg["min_gap_hours"]) * 3600, span / n * 0.8)
    slots = sorted(rng.uniform(0, span) for _ in range(n))
    for i in range(1, n):
        slots[i] = max(slots[i], slots[i - 1] + gap)
    over = slots[-1] - span
    if over > 0:
        slots = [x - over for x in slots]
        for i in range(n - 2, -1, -1):
            slots[i] = min(slots[i], slots[i + 1] - gap)
    if slots[0] < 0:             # window too small for the gaps: spread evenly instead
        slots = [i * span / (n - 1) for i in range(n)]
    return [start + x for x in slots]


def _window_end(s: Settings, day: str) -> float:
    return _at(s, day, s.cfg["batching"]["window"][1])


def plan_day(s: Settings, db: Database, day: str | None = None, now: float | None = None) -> list[Batch]:
    """The day's batches, drawn once and persisted so a retry never re-rolls quotas or times."""
    day = day or today(s)
    cache_key = f"batch_plan:{day}"
    raw = db.kv_get(cache_key)
    if raw:
        try:
            return [Batch(**b) for b in json.loads(raw)]
        except (ValueError, TypeError):
            log.warning("ignoring a corrupt batch plan for %s", day)
    now = time.time() if now is None else now
    rng = random.Random()
    roll_raw = db.kv_get(f"roll:{day}")
    if roll_raw:
        try:
            roll = json.loads(roll_raw)
        except ValueError:
            roll = {}
    else:
        roll = {}
    if not roll:
        n_pod, n_gta = shift_quota(s, db, clips_target(s, db, day), gta6_target(s, db, rng, day))
        talk, game = gta6_split(s, rng, n_gta)
        roll = {"podcast": n_pod, "gta6": n_gta, "talk": talk, "gameplay": game}
        db.kv_set(f"roll:{day}", json.dumps(roll))
    per = max(1, int(s.cfg["batching"]["max_clips_per_batch"]))
    n_pod = int(roll.get("podcast", 0))
    batches: list[Batch] = []
    for i, start in enumerate(range(0, n_pod, per)):
        batches.append(Batch(run_key(day, "podcast", i), "podcast", min(per, n_pod - start), 0.0))
    if int(roll.get("talk", 0)) > 0:
        batches.append(Batch(run_key(day, "gta6_talk"), "gta6_talk", int(roll["talk"]), 0.0))
    if int(roll.get("gameplay", 0)) > 0:
        batches.append(Batch(run_key(day, "gta6_gameplay"), "gta6_gameplay", int(roll["gameplay"]), 0.0))
    dues = _due_times(s, day, len(batches), rng, now)
    for b, due in zip(batches, dues):
        b.due = due
    db.kv_set(cache_key, json.dumps([asdict(b) for b in batches]))
    log.info("%s: %d batches (%s)", day, len(batches),
             ", ".join(f"{b.kind} x{b.clips}" for b in batches))
    return batches


def pending_batches(s: Settings, db: Database, day: str | None = None,
                    now: float | None = None) -> list[Batch]:
    out = []
    for b in plan_day(s, db, day=day, now=now):
        run = db.get_run(b.key)
        if run and run["status"] in FINISHED:
            continue
        out.append(b)
    return out


def next_due_batch(s: Settings, db: Database, day: str | None = None,
                   now: float | None = None) -> Batch | None:
    """The most overdue batch whose time has come. One per scheduler tick, never a burst."""
    now = time.time() if now is None else now
    day = day or today(s)
    pend = pending_batches(s, db, day=day, now=now)
    if not pend:
        return None

    def ready(b: Batch) -> bool:
        run = db.get_run(b.key)
        return not (run and run["next_try_at"] and run["next_try_at"] > now)

    due = [b for b in pend if b.due <= now and ready(b)]
    if due:
        return min(due, key=lambda b: b.due)
    if now >= _window_end(s, day):   # machine was off all day: drain the leftovers one at a time
        late = [b for b in pend if ready(b)]
        if late:
            return min(late, key=lambda b: b.due)
    return None


def batch_size(s: Settings, db: Database, key: str) -> int:
    """How many clips this batch should produce (falls back to the podcast day target)."""
    if ":" in key:
        for b in plan_day(s, db, day=day_of(key)):
            if b.key == key:
                return b.clips
    return clips_target(s, db)


def day_total(s: Settings, db: Database, key: str) -> int:
    """Clips planned for the whole calendar day of this run (all kinds), for spacing publish slots."""
    try:
        return sum(b.clips for b in plan_day(s, db, day=day_of(key))) or 1
    except Exception:  # noqa: BLE001
        return batch_size(s, db, key)
