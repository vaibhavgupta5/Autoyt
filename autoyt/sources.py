"""Source allowlist, listing cache and random video picking."""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass

from . import ytdlp
from .config import Settings
from .db import Database
from .errors import BotBlocked, SourceRejected
from .kinds import today

log = logging.getLogger("autoyt.sources")

# Footage from leaks or developer builds is never used, whatever the title filter says.
LEAK = re.compile(r"leak|leaked|cyberleek|dev build|debug build|early build", re.I)


@dataclass
class Picked:
    source: dict
    video_id: str
    title: str
    duration: float
    url: str


def load_sources(s: Settings) -> list[dict]:
    raw = s.yaml_file("sources.yaml") or []
    out = []
    for src in raw:
        if not src.get("name") or not src.get("url"):
            raise ValueError(f"source needs name and url: {src}")
        if not src.get("permission"):
            raise ValueError(f"source {src['name']!r} has no `permission` value, refusing to use it")
        src.setdefault("enabled", True)
        src.setdefault("weight", 1)
        src.setdefault("tabs", ["videos"])
        src.setdefault("credit", src["name"])
        src.setdefault("layout", "center_crop")
        src.setdefault("kind", "podcast")
        out.append(src)
    return out


def _in_window(src: dict, day: str) -> bool:
    """Optional ISO-date window: `after` starts a source, `until` retires it (GTA VI launch)."""
    after, until = src.get("after"), src.get("until")
    if after and day < str(after):
        return False
    if until and day > str(until):
        return False
    return True


def active_sources(s: Settings, db: Database, kind: str = "podcast",
                   day: str | None = None) -> list[dict]:
    disabled = db.disabled_sources()
    day = day or today(s)
    return [x for x in load_sources(s)
            if x["enabled"] and x["name"] not in disabled and x["kind"] == kind and _in_window(x, day)]


def _bounds(s: Settings, src: dict) -> tuple[float, float]:
    lo = float(src.get("min_minutes", s.cfg["source"]["min_minutes"])) * 60
    hi = float(src.get("max_minutes", s.cfg["source"]["max_minutes"])) * 60
    return lo, hi


def _title_ok(src: dict, title: str) -> bool:
    if LEAK.search(title):
        return False
    inc = src.get("title_include")
    exc = src.get("title_exclude") or []
    if inc and not any(re.search(p, title, re.I) for p in inc):
        return False
    if any(re.search(p, title, re.I) for p in exc):
        return False
    return True


def refresh_listing(s: Settings, db: Database, src: dict, force: bool = False) -> int:
    """Cache every eligible-by-length video of the channel. Returns eligible count."""
    # Remember WHEN we listed, even if nothing was eligible (otherwise a channel with 0 matches, like
    # Kai Cenat, is re-listed on every pick, which is wasteful and invites YouTube's rate limit).
    stamp = db.kv_get(f"listing_at:{src['name']}")
    age = time.time() - float(stamp) if stamp else db.listing_age(src["name"])
    refresh_s = s.cfg["source"]["listing_refresh_days"] * 86400
    if not force and age is not None and age < refresh_s:
        return len(db.listing(src["name"]))
    lo, hi = _bounds(s, src)
    rows: dict[str, dict] = {}
    for tab in src["tabs"]:
        url = src["url"].rstrip("/") + "/" + tab
        for e in ytdlp.list_flat(s, url, int(src.get("max_items", s.cfg["source"]["max_listing_items"]))):
            vid, dur, title = e.get("id"), e.get("duration"), e.get("title") or ""
            if not vid or not dur or not (lo <= float(dur) <= hi):
                continue
            if e.get("live_status") in ("is_live", "is_upcoming"):
                continue
            if not _title_ok(src, title):
                continue
            rows[vid] = {"id": vid, "title": title, "duration": float(dur)}
    db.replace_listing(src["name"], list(rows.values()))
    db.kv_set(f"listing_at:{src['name']}", str(time.time()))
    log.info("%s: %d eligible videos cached", src["name"], len(rows))
    return len(rows)


def check_video(s: Settings, src: dict, video_id: str) -> dict:
    """Final check right before use. Raises SourceRejected for anything not clearly public."""
    info = ytdlp.video_info(s, video_id)
    lo, hi = _bounds(s, src)
    avail = info.get("availability")
    if avail not in (None, "public"):
        raise SourceRejected(f"availability is {avail}")
    if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise SourceRejected(f"live_status {info.get('live_status')}")
    if (info.get("age_limit") or 0) >= 18:
        raise SourceRejected("age restricted")
    dur = info.get("duration") or 0
    if not (lo <= dur <= hi):
        raise SourceRejected(f"duration {dur}s outside {lo}-{hi}")
    lang = info.get("language")
    if lang and not str(lang).lower().startswith("en"):
        raise SourceRejected(f"language {lang}")
    return info


def pick_video(s: Settings, db: Database, rng: random.Random | None = None,
               kind: str = "podcast") -> Picked:
    """Weighted random source of this kind, then a random never-used public video from its history."""
    rng = rng or random.Random()
    cooldown = s.cfg["source"]["cooldown_days"] * 86400
    used, bad = db.used_ids(), db.bad_ids()
    sources = active_sources(s, db, kind)
    if not sources:
        raise SourceRejected("no enabled sources")
    for src in sources:  # lazy listing refresh
        try:
            refresh_listing(s, db, src)
        except BotBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("listing refresh failed for %s: %s", src["name"], e)

    def candidates(ignore_cooldown: bool) -> list[tuple[dict, list]]:
        out = []
        for src in sources:
            if not ignore_cooldown and time.time() - db.last_used_source_at(src["name"]) < cooldown:
                continue
            vids = [r for r in db.listing(src["name"]) if r["video_id"] not in used and r["video_id"] not in bad]
            if vids:
                out.append((src, vids))
        return out

    pool = candidates(False) or candidates(True)
    if not pool:
        raise SourceRejected("no unused eligible videos in any enabled source")
    from .stats import source_multipliers  # sources that earn views get picked more often
    mult = source_multipliers(s, db)

    for _ in range(s.cfg["source"]["max_pick_tries"]):
        if not pool:
            break
        src, vids = rng.choices(pool, weights=[p[0]["weight"] * mult.get(p[0]["name"], 1.0) for p in pool])[0]
        row = rng.choice(vids)
        try:
            info = check_video(s, src, row["video_id"])
        except SourceRejected as e:
            log.info("rejected %s (%s): %s", row["video_id"], row["title"][:50], e)
            db.mark_bad(row["video_id"], str(e))
            bad.add(row["video_id"])
            pool = [(sr, [v for v in vs if v["video_id"] not in bad]) for sr, vs in pool]
            pool = [p for p in pool if p[1]]
            continue
        return Picked(src, row["video_id"], info.get("title") or row["title"],
                      float(info.get("duration") or row["duration"]),
                      f"https://www.youtube.com/watch?v={row['video_id']}")
    raise SourceRejected("could not find a usable video after several tries")
