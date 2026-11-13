"""When each uploaded video goes public: inside the audience's evening, never two at once.

Batches run whenever the laptop is awake (local time), upload as private, and YouTube's publishAt
releases each video inside `slots.publish_window` in `slots.audience_tz`. Every video, of every kind,
is at least `min_gap` minutes after the previous one.
"""
from __future__ import annotations

import random
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database


def _window(s: Settings, d: date) -> tuple[datetime, datetime]:
    sl = s.cfg["slots"]
    tz = ZoneInfo(sl["audience_tz"])
    start_s, end_s = (sl.get("window_overrides") or {}).get(d.isoformat(), sl["publish_window"])
    sh, sm = (int(x) for x in start_s.split(":"))
    eh, em = (int(x) for x in end_s.split(":"))
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=tz)
    return start, end


def gap_minutes(s: Settings, n_today: int) -> float:
    sl = s.cfg["slots"]
    start, end = _window(s, date.today())
    span = (end - start).total_seconds() / 60
    return float(max(sl["min_gap"], min(sl["max_gap"], span / max(n_today, 1))))


def fit(s: Settings, t: float, not_before: float, rng: random.Random) -> float:
    """The earliest moment >= max(t, not_before) that lies inside a publish window."""
    tz = ZoneInfo(s.cfg["slots"]["audience_tz"])
    jitter = s.cfg["slots"]["jitter_minutes"] * 60
    t = max(t, not_before)
    for _ in range(60):
        d = datetime.fromtimestamp(t, tz=tz).date()
        start, end = _window(s, d)
        if t < start.timestamp():
            t = max(start.timestamp() + rng.uniform(0, jitter), not_before)
            continue
        if t > end.timestamp():
            nxt = d + timedelta(days=1)
            t = max(_window(s, nxt)[0].timestamp() + rng.uniform(0, jitter), not_before)
            continue
        return t
    return t


def next_slot(s: Settings, db: Database, n_today: int, rng: random.Random | None = None,
              now: float | None = None) -> str:
    """Next publishAt (UTC ISO) and remember it, so the following video queues after it."""
    rng = rng or random.Random()
    now = time.time() if now is None else now
    sl = s.cfg["slots"]
    last = float(db.kv_get("last_slot", "0") or 0)
    gap = gap_minutes(s, n_today) * 60
    earliest = now + sl["first_delay_minutes"] * 60
    want = max(earliest, last + gap) + rng.uniform(0, sl["jitter_minutes"] * 60)
    t = fit(s, want, max(earliest, last + sl["min_gap"] * 60), rng)
    db.kv_set("last_slot", str(t))
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
