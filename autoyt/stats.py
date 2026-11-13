"""Performance feedback: view counts per clip at fixed ages, and what the pipeline learns from them.

Once a day the Data API is asked for the statistics of clips that turned 1, 3 and 7 days old (one cheap
call per 50 videos). Those numbers feed the summary email, the source weights used when picking the next
video, the category hint given to the model, and the growth level that decides how many clips a day run.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database

log = logging.getLogger("autoyt.stats")

GTA_KINDS = ("gta6_talk", "gta6_gameplay")
SCOPE_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPE_COMMENTS = "https://www.googleapis.com/auth/youtube.force-ssl"


# ------------------------------------------------------------------ time helpers
def published_at(row) -> float:
    """When the clip went (or goes) public: its publishAt, else the upload time."""
    pa = row["publish_at"]
    if pa:
        try:
            return datetime.strptime(pa, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return float(row["updated_at"] or row["created_at"] or time.time())


def _uploaded(db: Database, since_days: float = 30) -> list:
    cutoff = time.time() - since_days * 86400
    return db.query("SELECT c.*, r.source FROM clips c LEFT JOIN runs r ON r.day=c.day "
                    "WHERE c.youtube_id IS NOT NULL AND c.status='uploaded' AND c.created_at>?", (cutoff,))


# ------------------------------------------------------------------ fetching
def fetch(s: Settings, db: Database, service, now: float | None = None) -> int:
    """Record views/likes/comments for every clip that reached a checkpoint age. Returns rows written."""
    now = time.time() if now is None else now
    checkpoints = [int(x) for x in s.cfg["stats"]["checkpoints"]]
    due: dict[str, list[int]] = {}
    for r in _uploaded(db, since_days=max(checkpoints) + 14):
        age = (now - published_at(r)) / 86400
        cps = [cp for cp in checkpoints if age >= cp and not db.has_stat(r["youtube_id"], cp)]
        if cps:
            due[r["youtube_id"]] = cps
    if not due:
        return 0
    written = 0
    ids = list(due)
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        resp = service.videos().list(part="statistics", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            for cp in due[item["id"]]:
                db.add_stat(item["id"], cp, int(st.get("viewCount", 0)), int(st.get("likeCount", 0)),
                            int(st.get("commentCount", 0)))
                written += 1
    _fetch_retention(s, db, ids, now)
    log.info("stats: recorded %d checkpoint rows for %d videos", written, len(ids))
    return written


def _fetch_retention(s: Settings, db: Database, ids: list[str], now: float) -> None:
    """Average view percentage, if the login includes YouTube Analytics. Best effort, silent otherwise."""
    if SCOPE_ANALYTICS not in token_scopes(s) or not ids:
        return
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(s.secrets_dir / "token.json"))
        yta = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        end = datetime.fromtimestamp(now, tz=timezone.utc).date()
        start = end - timedelta(days=30)
        for i in range(0, len(ids), 50):
            rep = yta.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                                      metrics="averageViewPercentage", dimensions="video",
                                      filters="video==" + ",".join(ids[i:i + 50])).execute()
            for vid, pct in rep.get("rows") or []:
                db.execute("UPDATE clip_stats SET avg_view_pct=? WHERE youtube_id=?", (float(pct), vid))
    except Exception as e:  # noqa: BLE001
        log.info("retention not fetched: %s", e)


def token_scopes(s: Settings) -> set[str]:
    try:
        return set(json.loads((s.secrets_dir / "token.json").read_text()).get("scopes") or [])
    except (OSError, ValueError):
        return set()


# ------------------------------------------------------------------ reading
def samples(db: Database, kinds: tuple[str, ...] | None = None, days: float = 7, age: int = 3) -> list[dict]:
    """Clips published in the last `days` days that have a stat at `age` days."""
    cutoff = time.time() - (days + age) * 86400
    rows = db.query("SELECT c.kind, c.category, c.duration, c.publish_at, c.updated_at, c.created_at, c.title, "
                    "c.youtube_id, r.source, st.views, st.likes, st.avg_view_pct "
                    "FROM clip_stats st JOIN clips c ON c.youtube_id=st.youtube_id "
                    "LEFT JOIN runs r ON r.day=c.day WHERE st.age_days=? AND c.created_at>?", (age, cutoff))
    out = [dict(r) for r in rows]
    if kinds:
        out = [r for r in out if r["kind"] in kinds]
    return out


def median_views(db: Database, kinds: tuple[str, ...] | None, days: float = 7) -> tuple[float, int]:
    vals = [r["views"] for r in samples(db, kinds, days)]
    return (float(statistics.median(vals)) if vals else 0.0), len(vals)


def source_multipliers(s: Settings, db: Database) -> dict[str, float]:
    """Per-source pick multiplier: its median 72-hour views over the channel's, clamped to [0.25, 3].

    Inactive until enough clips have 72-hour data, so the first weeks explore every source evenly.
    """
    rows = samples(db, None, days=30)
    if len(rows) < int(s.cfg["stats"]["min_clips_for_weights"]):
        return {}
    overall = max(1.0, statistics.median(r["views"] for r in rows))
    by: dict[str, list[int]] = {}
    for r in rows:
        if r["source"]:
            by.setdefault(r["source"], []).append(r["views"])
    return {src: max(0.25, min(3.0, statistics.median(v) / overall)) for src, v in by.items() if len(v) >= 2}


def category_hint(s: Settings, db: Database, kinds: tuple[str, ...]) -> str:
    """One line for the selection prompt, e.g. 'informative clips get the most views, then funny'."""
    rows = [r for r in samples(db, kinds, days=21) if r["category"]]
    if len(rows) < int(s.cfg["stats"]["min_clips_for_weights"]):
        return ""
    by: dict[str, list[int]] = {}
    for r in rows:
        by.setdefault(r["category"], []).append(r["views"])
    ranked = sorted(by, key=lambda c: -statistics.median(by[c]))
    if len(ranked) < 2:
        return ""
    return (f"On this channel, {ranked[0]} clips currently get the most views, then {ranked[1]}. "
            f"When two moments are equally good, prefer {ranked[0]}.")


def summary_block(s: Settings, db: Database) -> str:
    """Last 7 days of 72-hour views by kind, source, category, length and publish hour, plus top/bottom 3."""
    rows = samples(db, None, days=7)
    if not rows:
        return "Performance: no 72-hour view counts yet."
    tz = ZoneInfo(s.cfg["slots"]["audience_tz"])

    def table(label: str, key) -> str:
        by: dict[str, list[int]] = {}
        for r in rows:
            k = key(r)
            if k is not None:
                by.setdefault(str(k), []).append(r["views"])
        lines = [f"  {k:24} clips={len(v):3}  median={statistics.median(v):7.0f}  total={sum(v):7}"
                 for k, v in sorted(by.items(), key=lambda kv: -statistics.median(kv[1]))]
        return f"{label}:\n" + "\n".join(lines)

    def bucket(r):
        d = r["duration"] or 0
        return "<30s" if d < 30 else "30-45s" if d < 45 else "45-60s"

    def hour(r):
        t = published_at(r)
        return f"{datetime.fromtimestamp(t, tz=tz).hour:02d}h {tz.key.split('/')[-1]}"

    ranked = sorted(rows, key=lambda r: -r["views"])
    top = "\n".join(f"  {r['views']:6} views  {r['title'][:60]}  https://youtu.be/{r['youtube_id']}" for r in ranked[:3])
    low = "\n".join(f"  {r['views']:6} views  {r['title'][:60]}" for r in ranked[-3:])
    return "\n\n".join([
        f"Performance, last 7 days (views 72 h after publishing, {len(rows)} clips)",
        table("By kind", lambda r: r["kind"]), table("By source", lambda r: r["source"]),
        table("By category", lambda r: r["category"] or "-"), table("By length", bucket),
        table("By publish hour", hour), "Best:\n" + top, "Weakest:\n" + low,
    ])


# ------------------------------------------------------------------ engagement
def comments_enabled(s: Settings) -> bool:
    mode = str(s.cfg["engagement"].get("comment", "auto")).lower()
    return mode != "off" and SCOPE_COMMENTS in token_scopes(s)


def post_due_comments(s: Settings, db: Database, service, now: float | None = None, limit: int = 10) -> int:
    """One question comment per video once it is public (comments on a private video are refused)."""
    if not comments_enabled(s):
        return 0
    now = time.time() if now is None else now
    posted = 0
    for r in db.query("SELECT * FROM clips WHERE status='uploaded' AND youtube_id IS NOT NULL "
                      "AND comment_posted=0 AND comment_question IS NOT NULL AND comment_question != ''"):
        if published_at(r) > now - 300 or posted >= limit:
            continue
        try:
            service.commentThreads().insert(part="snippet", body={"snippet": {
                "videoId": r["youtube_id"],
                "topLevelComment": {"snippet": {"textOriginal": r["comment_question"][:300]}}}}).execute()
            db.update_clip(r["id"], comment_posted=1)
            posted += 1
        except Exception as e:  # noqa: BLE001
            log.warning("comment on %s failed: %s", r["youtube_id"], e)
            db.update_clip(r["id"], comment_posted=-1)
    return posted
