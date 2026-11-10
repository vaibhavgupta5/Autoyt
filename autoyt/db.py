"""SQLite state: runs, steps, clips, listings, usage. Everything resumable."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  day TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'pending',     -- pending|running|done|failed|skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  source TEXT, video_id TEXT, video_title TEXT, video_url TEXT, credit TEXT,
  error TEXT, next_try_at REAL, started_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS steps (day TEXT, step TEXT, done_at REAL, PRIMARY KEY (day, step));
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT, idx INTEGER, start REAL, end REAL, duration REAL,
  heading TEXT, title TEXT, description TEXT, context_line TEXT,
  category TEXT, score REAL, text TEXT,
  status TEXT NOT NULL DEFAULT 'planned',     -- planned|rendered|uploaded|failed|deleted
  file TEXT, publish_at TEXT, youtube_id TEXT, thumb_ok INTEGER,
  music TEXT, error TEXT, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS clips_day ON clips(day);
CREATE TABLE IF NOT EXISTS used_videos (video_id TEXT PRIMARY KEY, source TEXT, used_at REAL);
CREATE TABLE IF NOT EXISTS bad_videos (video_id TEXT PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS listing (
  source TEXT, video_id TEXT, title TEXT, duration REAL, refreshed_at REAL,
  PRIMARY KEY (source, video_id)
);
CREATE TABLE IF NOT EXISTS disabled_sources (name TEXT PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notified (day TEXT, kind TEXT, at REAL, PRIMARY KEY (day, kind));
CREATE TABLE IF NOT EXISTS gameplay_usage (file TEXT, start REAL, used_at REAL);
"""


class Database:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(clips)")}
        if "tags" not in cols:  # added after the first release: migrate an existing database in place
            self.conn.execute("ALTER TABLE clips ADD COLUMN tags TEXT")
        self.conn.commit()

    # ---- helpers ----
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def _update(self, table: str, key: str, value: Any, fields: dict[str, Any]) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE {table} SET {cols} WHERE {key}=?", [*fields.values(), value])

    # ---- runs ----
    def get_run(self, day: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM runs WHERE day=?", (day,))

    def ensure_run(self, day: str) -> sqlite3.Row:
        if not self.get_run(day):
            self.execute("INSERT INTO runs(day, status, updated_at) VALUES (?, 'pending', ?)", (day, time.time()))
        return self.get_run(day)  # type: ignore[return-value]

    def update_run(self, day: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        self._update("runs", "day", day, fields)

    # ---- steps ----
    def step_done(self, day: str, step: str) -> bool:
        return self.one("SELECT 1 FROM steps WHERE day=? AND step=?", (day, step)) is not None

    def mark_step(self, day: str, step: str) -> None:
        self.execute("INSERT OR REPLACE INTO steps(day, step, done_at) VALUES (?,?,?)", (day, step, time.time()))

    def archive_day(self, day: str) -> str:
        """Rename a finished day's rows (run, clips, steps, notices) so the day can run again from scratch.

        Uploaded clips stay tracked under the new name, so the rejection monitor still sees them.
        """
        n = 1
        while self.get_run(f"{day}#{n}"):
            n += 1
        new = f"{day}#{n}"
        for table in ("runs", "clips", "steps", "notified"):
            self.execute(f"UPDATE {table} SET day=? WHERE day=?", (new, day))
        return new

    def clear_steps(self, day: str) -> None:
        self.execute("DELETE FROM steps WHERE day=?", (day,))

    # ---- clips ----
    def add_clips(self, day: str, clips: list[dict]) -> None:
        self.execute("DELETE FROM clips WHERE day=?", (day,))
        now = time.time()
        for i, c in enumerate(clips):
            self.execute(
                "INSERT INTO clips(day, idx, start, end, duration, heading, title, description, context_line,"
                " category, score, text, tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, i, c["start"], c["end"], c["duration"], c["heading"], c["title"], c["description"],
                 c.get("context_line", ""), c["category"], c["score"], c.get("text", ""),
                 json.dumps(c.get("tags") or []), now, now),
            )

    def clips_for(self, day: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM clips WHERE day=? ORDER BY idx", (day,))

    def update_clip(self, clip_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        self._update("clips", "id", clip_id, fields)

    # ---- videos ----
    def mark_used(self, video_id: str, source: str) -> None:
        self.execute("INSERT OR REPLACE INTO used_videos VALUES (?,?,?)", (video_id, source, time.time()))

    def used_ids(self) -> set[str]:
        return {r["video_id"] for r in self.query("SELECT video_id FROM used_videos")}

    def mark_bad(self, video_id: str, reason: str) -> None:
        self.execute("INSERT OR REPLACE INTO bad_videos VALUES (?,?,?)", (video_id, reason[:300], time.time()))

    def bad_ids(self) -> set[str]:
        return {r["video_id"] for r in self.query("SELECT video_id FROM bad_videos")}

    def last_used_source_at(self, source: str) -> float:
        r = self.one("SELECT MAX(used_at) m FROM used_videos WHERE source=?", (source,))
        return float(r["m"] or 0)

    # ---- listing cache ----
    def listing_age(self, source: str) -> float | None:
        r = self.one("SELECT MAX(refreshed_at) m FROM listing WHERE source=?", (source,))
        return None if r is None or r["m"] is None else time.time() - float(r["m"])

    def replace_listing(self, source: str, rows: list[dict]) -> None:
        now = time.time()
        self.execute("DELETE FROM listing WHERE source=?", (source,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO listing VALUES (?,?,?,?,?)",
            [(source, r["id"], r["title"], r["duration"], now) for r in rows],
        )
        self.conn.commit()

    def listing(self, source: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM listing WHERE source=?", (source,))

    # ---- sources ----
    def disable_source(self, name: str, reason: str) -> None:
        self.execute("INSERT OR REPLACE INTO disabled_sources VALUES (?,?,?)", (name, reason[:300], time.time()))

    def disabled_sources(self) -> dict[str, str]:
        return {r["name"]: r["reason"] for r in self.query("SELECT * FROM disabled_sources")}

    # ---- kv / notifications ----
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))

    def was_notified(self, day: str, kind: str) -> bool:
        return self.one("SELECT 1 FROM notified WHERE day=? AND kind=?", (day, kind)) is not None

    def mark_notified(self, day: str, kind: str) -> None:
        self.execute("INSERT OR REPLACE INTO notified VALUES (?,?,?)", (day, kind, time.time()))

    # ---- gameplay ----
    def add_gameplay_usage(self, file: str, start: float) -> None:
        self.execute("INSERT INTO gameplay_usage VALUES (?,?,?)", (file, start, time.time()))

    def recent_gameplay(self, file: str, days: float = 7) -> list[float]:
        cutoff = time.time() - days * 86400
        return [r["start"] for r in self.query("SELECT start FROM gameplay_usage WHERE file=? AND used_at>?", (file, cutoff))]
