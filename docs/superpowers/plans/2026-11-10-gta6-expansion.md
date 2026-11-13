# GTA6 expansion + quality fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two full-screen GTA6 video kinds (talk/review and gameplay), raise output to 7 podcast + 3–6 GTA6 clips/day spread over randomized batches, and fix the six confirmed visual-quality defects.

**Architecture:** A run is keyed `"<day>:<kind>[#n]"` stored in the existing TEXT `day` columns (no PK rebuild — the composite key *is* `(day, kind)`), so every existing `WHERE day=?` query keeps working. A new `kinds.py` draws the day's quotas and batch plan (random due times in an awake window, persisted in kv). The pipeline branches per kind: podcast/gta6_talk use the transcript→Gemini path (talk uses a GTA-specific system prompt), gta6_gameplay uses a new audio-energy segment selector with no transcript. A new `layout: full` render template and kind-aware caption anchors cover both full-screen formats.

**Tech Stack:** Python 3.12, SQLite (WAL), ffmpeg/ffprobe, yt-dlp, Gemini, faster-whisper, numpy (already present via faster-whisper), pytest, Docker.

**Spec:** `docs/superpowers/specs/2026-11-10-gta6-expansion-design.md`

## Global Constraints

- GTA6 videos: **no added background music**. `gta6_talk` = source speech only; `gta6_gameplay` = original audio only, LUFS-normalized.
- **No trailer-reaction content anywhere** (`title_exclude` at source level + Gemini prompt rule + music-presence validator heuristic).
- **Leak footage is hard-blocked**: titles matching `leak|leaked|cyberleek|dev build|debug build|early build` are never eligible.
- GTA6 permission posture: `fair_use_credit` — every GTA6 description keeps the credit + removal note.
- Ramp: podcast 3→5→7 (existing `ramp`); GTA6 2/day for 3 days, then random 3–6/day.
- Slots: `min_gap` 30, `max_gap` 120, span 20 h.
- Clip duration: podcast & talk 50–55 s; gameplay 20–45 s.
- Output `1080x1920` @30 fps; existing quality caps (720p source) unchanged.
- Exactly one batch may start per scheduler tick; overdue batches drain one per tick.
- Category `NULL` is valid only for `gta6_gameplay` (`upload.CATEGORY.get` already falls back to `24`).
- Existing 52 tests must keep passing (updates to affected assertions allowed only where the spec changes behavior).

## Review Focus

- A day where one kind's sources are empty/exhausted: the batch must finish `skipped` and the other batches must still run; the day must not become `failed`.
- Legacy databases: existing `runs/steps/clips/day` rows must be migrated to `:podcast` keys and never double-count or crash `status`.
- The laptop being off for hours: overdue batches must run one per tick, not all at once.
- A retry of one batch must never re-pick or re-roll the other batches' quotas, videos, or slots.
- Gameplay clips with no speech: no Whisper call, no caption track, no crash in `_tags/_hashtags` on `category=NULL`.

## Reach addendum (2026-11-11)

Added after reviewing the live container and the channel's numbers. **Read this before executing; it changes task order and adds Tasks R0–R6.**

> **Status 2026-11-11:** Tasks 1–14 and R0–R6 are implemented and covered by `tests/test_kinds.py`, `tests/test_segments.py` and `tests/test_reach.py`. Owner decision: GTA 5 gameplay runs until 18 Nov (labelled GTA 5 everywhere), GTA 6 gameplay only from 19 Nov.

### What the data says (2026-11-11)

- 12 Shorts uploaded 8–10 Nov: **48 views total**, best 14, median ~3, zero likes or comments. Channel: 46 videos, 720 lifetime views, 6 subs. The bottleneck is **reach per video, not volume**. Going 3 → 13/day on this baseline multiplies spam/reused-content risk without fixing reach.
- **The pipeline is currently blocked.** Since 10 Nov, yt-dlp gets "Sign in to confirm you're not a bot" on every metadata probe and audio download (yt-dlp 2026.10.26, deno present, **no PO-token provider**). The 11 Nov run burned 14 probes and 4 audio retries, and is pending on attempt 1/4. Each GTA batch adds more probes, so 10–13/day would make the block worse.
- Clip `1rVBSltSTGA` starts at 0.0 s (episode intro), despite the "skip intros" prompt rule. No code check enforces it.
- The 3 clips from 8 Nov (manual run) all went public at the same minute (20:50 UTC), so they competed with each other.
- Publish times follow "now + 30 min, spread over 20 h" in the laptop's timezone, not the audience's. 10 Nov clips went public at 00:21, 03:24 and 06:28 UTC.
- The thumbnail call fails with 403 on every upload (channel not phone-verified). Shorts mostly ignore custom thumbnails anyway, so this is noise, not a reach lever.
- **GTA VI launches 19 Nov 2026 (8 days).** Search and Shorts demand for "GTA 6" peaks in the next ~3 weeks. Shipping `gta6_talk` before launch is worth more than any other task in this plan.

### Revised execution order

1. **R0 – unblock ingestion** (blocker: nothing ships without it).
2. Tasks 1–4 (done in the working tree; commit them) → 6 → 7 → 8 → 9 → 10 → 11 → 13 → 14 (talk sources only) → 15. **Target: `gta6_talk` live by 16 Nov**, three days before launch.
3. **R1–R4** (reach levers). R1 and R2 are small and can merge with Tasks 9/10.
4. Tasks 5 (done) and 12, then the gameplay half of Task 14. **Gameplay goes live at launch (19 Nov) on real GTA VI footage**, unless the owner explicitly keeps the GTA V day-1 decision. If GTA V footage runs pre-launch, its titles, headings and hashtags must say "GTA 5"/"GTA Online", never "GTA 6"; a GTA6 label on GTA V footage is misleading metadata under YouTube's spam policy and earns swipe-aways from viewers who notice.
5. **R5** (feedback loop), then **R6** (performance-gated ramp) replaces the fixed day-count ramp.

### Task R0: Unblock yt-dlp (PO-token provider, fewer probes)

**Files:** `docker-compose.yml`, `Dockerfile`, `autoyt/ytdlp.py`, `autoyt/sources.py`, `autoyt/monitor.py`, `README.md`

- [ ] Add the `bgutil-ytdlp-pot-provider` plugin to the image (`pip install bgutil-ytdlp-pot-provider`) and a `bgutil-provider` sidecar service (HTTP-server mode, port 4416) to `docker-compose.yml`. Pass `--extractor-args "youtubepot-bgutilhttp:base_url=http://bgutil-provider:4416"` from `ytdlp.py`. This keeps the "no cookies, no login" policy.
- [ ] Verify with `docker compose run --rm autoyt yt-dlp -v --simulate <public id>`: the log must list the bgutil provider, not `PO Token Providers: none`.
- [ ] Cut probe volume. `pick_video` should rank candidates from the cached `listing` table (flat playlist: title, duration, upload date) and run a full metadata probe only on the chosen video, not up to 8 per attempt. Add `--sleep-requests 2` to probes.
- [ ] Treat a bot-check as a **global** back-off, not a per-video reject. On the first bot-check in a run, stop probing, set `kv:bot_block_until = now + 2h`, and skip ticks until then. Today each blocked video is marked rejected, which wrongly burns good videos.
- [ ] Email once when a bot block lasts more than 6 h. The fix is manual (update yt-dlp or the provider).
- [ ] Test: fake `ytdlp` raising the bot-check text → the run defers with `next_try_at`, no `bad_videos` rows are written, and the next tick skips.

### Task R1: Hook and clip-boundary rules

**Files:** `autoyt/validate.py`, `autoyt/select_clips.py`, tests

- [ ] Reject candidates that start in the first 90 s or the last 120 s of a source (intros/outros), unless `hook_score >= 9`.
- [ ] Per-kind length: `gta6_talk` 25–45 s (news and breakdown beats are short; shorter clips loop and complete more often); podcast keeps 50–55 s for now, with R5 deciding later. The selection prompt must give the word budget per kind (~65–110 words for talk).
- [ ] The heading must be on screen from frame 0 (assert in the render test). The first spoken line must be the hook: tell Gemini to start on the claim, not on setup ("So, um, the thing about…").
- [ ] Prefer endings that loop: add a `loop_score` field to the prompt and break ties with it.

### Task R2: Search-first metadata for GTA kinds

**Files:** `autoyt/select_clips.py` (talk prompt), `autoyt/pipeline.py` (`_title/_tags/_hashtags/_description`), tests

- [ ] GTA titles must contain the exact phrase "GTA 6" in the first 40 characters, plus the concrete subject (feature, map, price, release time, character). These are the queries people type during launch week.
- [ ] Hashtag order for GTA kinds: `#GTA6 #GTAVI #<subject>`. The first three show above the title. Drop `#shorts` from titles (it wastes title characters and has no effect on Shorts distribution); keep it in the description.
- [ ] Rotate launch-window topics into the talk prompt by date: pre-launch (release time by region, preload, price/editions, map, what's confirmed); launch week (first impressions, tips, hidden details). Pass `days_to_launch` into the prompt.
- [ ] Test: GTA talk metadata contains "GTA 6" in the title prefix and `#GTA6` as the first hashtag; podcast metadata is unchanged.

### Task R3: Publish at the audience's peak hours

**Files:** `autoyt/kinds.py`, `autoyt/pipeline.py` (`next_slot`), `autoyt/config.py`, tests

- [ ] Add `slots.audience_tz` (default `America/New_York`) and `slots.publish_window` (default `["11:00", "23:00"]` in that timezone). Slots are drawn inside that window, with `min_gap` 30 between **all** kinds, so videos never stack.
- [ ] Keep the batch *run* window (laptop-awake hours, local time) separate from the *publish* window. Batches upload early as private and `publishAt` releases them at the right time. Make sure the slot horizon covers the next day's window when today's has passed.
- [ ] Launch override: on 18–20 Nov, put 1–2 GTA slots right after the global unlock and preload news (date-gated config, no code at launch).
- [ ] Test: 13 slots for one day all land inside the window, at least 30 min apart, and in strictly increasing order across kinds.

### Task R4: Stop the thumbnail noise, add a pinned engagement comment

**Files:** `autoyt/pipeline.py`, `autoyt/upload.py`

- [ ] Skip `set_thumbnail` while `kv:thumbs_forbidden` is set (set it on the first 403; retry weekly). This removes one wasted API call and one warning per clip.
- [ ] Optional, behind the `engagement_comment: true` flag: post one top-level comment per video with a question tied to the clip ("Day-one buy or wait for reviews?"). This needs the `youtube.force-ssl` scope, so it needs a one-time re-auth; leave it off until the owner re-authorizes.

### Task R5: Performance feedback loop

**Files:** create `autoyt/stats.py`, `tests/test_stats.py`; modify `autoyt/db.py` (`clip_stats` table), `autoyt/scheduler.py`, `autoyt/notify.py`, `autoyt/sources.py`, `autoyt/main.py`

- [ ] Once per day, fetch `statistics` (views/likes/comments) via the Data API for clips published 1, 3 and 7 days ago, and store `(youtube_id, age_days, views, likes, comments)`. That costs 1 quota unit per 50 ids.
- [ ] If the owner re-auths with `yt-analytics.readonly`, also store `averageViewPercentage` and `averageViewDuration`. Retention is the metric the Shorts feed actually optimizes.
- [ ] Add a "Last 7 days" block to the summary email: views by kind, source, category, length bucket and publish hour, plus the top 3 and bottom 3 clips.
- [ ] Weighting: source weight becomes `config_weight × (0.5 + share of 72 h views)`, clamped to [0.25, 3]. Category and length-bucket preferences go into the selection prompt as hints. This only engages after ≥ 20 clips with 72 h data, so the system explores first.
- [ ] Add a `autoyt stats` CLI command.

### Task R6: Gate the ramp on performance, not on days

**Files:** `autoyt/kinds.py` (`clips_target`, `gta6_target`), `autoyt/config.py`, tests

- [ ] Move to the next step (podcast 3→5→7; GTA 2→3→3–6) only when the 7-day median of 72 h views per clip is at least the threshold for that step (default 50/150/400) **and** no strike or claim arrived in the last 7 days. Drop one step if the median falls below a third of the threshold.
- [ ] While a kind outperforms the other (median ×3 or more for 7 days), shift daily quota toward it within the same total. If GTA talk takes off at launch while podcasts sit at ~3 views, the slots should go to GTA.
- [ ] Test: fixed `clip_stats` fixtures → the expected target per step, a step-down on a slump, and a hold on a strike.

## File Map

- Create: `autoyt/kinds.py`, `autoyt/segments.py`, `tests/test_kinds.py`, `tests/test_segments.py`
- Modify: `autoyt/config.py`, `autoyt/db.py`, `autoyt/sources.py`, `autoyt/select_clips.py`, `autoyt/validate.py`, `autoyt/subtitles.py`, `autoyt/render.py`, `autoyt/qc.py`, `autoyt/assets.py`, `autoyt/pipeline.py`, `autoyt/scheduler.py`, `autoyt/main.py`
- Config: `config/settings.yaml`, `config/sources.yaml`
- Docs: `README.md`, `PLAN.md`

---

### Task 0: Baseline checkpoint (project is not a git repo)

**Files:** none

- [ ] **Step 1: Initialise git and commit the current tree**

```bash
cd /home/vaibhav/Projects/autoyt
git init
printf 'data/\nassets/gameplay/\nassets/music/\nsecrets/\n__pycache__/\n*.egg-info/\n' > .gitignore
git add -A && git commit -m "chore: baseline before GTA6 expansion"
```

- [ ] **Step 2: Run the existing suite to record the baseline**

Run: `python -m pytest tests -q` (project venv; requires ffmpeg)
Expected: all pass (52 tests). Note the exact count in the commit message later if it changed.

- [ ] **Step 3: Commit** — nothing to add; baseline commit from Step 1 stands.

---

### Task 1: Kind and batch config defaults

**Files:**
- Modify: `autoyt/config.py:56-60` (DEFAULTS `ramp`, `slots`, add `kinds`, `gta6`, `batching`)
- Modify: `config/settings.yaml`
- Test: `tests/test_kinds.py` (new)

**Interfaces:**
- Produces: `DEFAULTS["kinds"][kind]["clip"]`, `DEFAULTS["gta6"]`, `DEFAULTS["batching"]`; helper `clip_cfg(s: Settings, kind: str) -> dict` in `autoyt/kinds.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kinds.py
from autoyt.kinds import clip_cfg

def test_clip_bounds_per_kind(env_settings):
    assert clip_cfg(env_settings, "podcast")["min_seconds"] == 50
    assert clip_cfg(env_settings, "gta6_talk")["max_seconds"] == 55
    g = clip_cfg(env_settings, "gta6_gameplay")
    assert (g["min_seconds"], g["max_seconds"]) == (20, 45)
    # every other clip key survives (min_words etc. only matter for talk kinds)
    assert g["min_words"] == env_settings.cfg["clip"]["min_words"]
```

`env_settings` fixture: same as `tests/test_misc.py::s` (tmp AUTOYT_HOME + `load_settings()`).

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_kinds.py::test_clip_bounds_per_kind -v`
Expected: FAIL — `ModuleNotFoundError: autoyt.kinds`.

- [ ] **Step 3: Implement**

In `config.py` DEFAULTS add:

```python
"kinds": {
    "podcast": {"clip": {"min_seconds": 50.0, "max_seconds": 55.0}},
    "gta6_talk": {"clip": {"min_seconds": 50.0, "max_seconds": 55.0}},
    "gta6_gameplay": {"clip": {"min_seconds": 20.0, "max_seconds": 45.0}},
},
"gta6": {"min": 3, "max": 6, "talk_weight": 0.5, "ramp": [[0, 2], [4, 3]]},
"batching": {"max_clips_per_batch": 4, "window": ["08:00", "22:00"], "min_gap_hours": 2},
"slots": {"first_delay_minutes": 30, "target_span_hours": 20, "min_gap": 30, "max_gap": 120,
          "jitter_minutes": 8},
```

In `kinds.py`:

```python
def clip_cfg(s: Settings, kind: str) -> dict:
    base = dict(s.cfg["clip"])
    base.update(s.cfg["kinds"].get(kind, {}).get("clip", {}))
    return base
```

Mirror the new blocks in `config/settings.yaml` (kinds, gta6, batching; slots min_gap 30 / max_gap 120).

- [ ] **Step 4: Run the test** — `pytest tests/test_kinds.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/config.py autoyt/kinds.py config/settings.yaml tests/test_kinds.py
git commit -m "feat: per-kind clip bounds, gta6 quotas and batching defaults"
```

---

### Task 2: Composite run keys, migration, clips.kind

**Files:**
- Modify: `autoyt/db.py:10-53` (SCHEMA + `__init__` migration), `autoyt/db.py:110-120` (`add_clips`)
- Test: `tests/test_kinds.py`

**Interfaces:**
- Produces: `run_key(day: str, kind: str, batch_idx: int = 0) -> str`, `kind_of(key: str) -> str`, `day_of(key: str) -> str` in `kinds.py`; `Database.add_clips(day, clips, kind="podcast")`.
- Key format: `"2026-11-10:podcast"`, second podcast batch `"2026-11-10:podcast#2"`; kinds never contain `#`.

- [ ] **Step 1: Write the failing test**

```python
def test_run_keys_and_legacy_migration(tmp_path):
    from autoyt.db import Database
    from autoyt.kinds import day_of, kind_of, run_key
    assert run_key("2026-11-10", "podcast") == "2026-11-10:podcast"
    assert run_key("2026-11-10", "podcast", 1) == "2026-11-10:podcast#2"
    assert kind_of("2026-11-10:gta6_talk#2") == "gta6_talk"
    assert day_of("2026-11-10:gta6_gameplay") == "2026-11-10"
    assert kind_of("2026-11-10:podcast") == "podcast"

    db = Database(tmp_path / "s.db")
    db.execute("INSERT INTO runs(day, status) VALUES ('2026-11-01', 'done')")
    db.execute("INSERT INTO steps(day, step) VALUES ('2026-11-01', 'pick')")
    db.execute("INSERT INTO clips(day, idx, title) VALUES ('2026-11-01', 0, 'old')")
    db2 = Database(tmp_path / "s.db")  # migration runs on open
    assert db2.get_run("2026-11-01") is None
    assert db2.get_run("2026-11-01:podcast")["status"] == "done"
    assert db2.step_done("2026-11-01:podcast", "pick")
    assert db2.clips_for("2026-11-01:podcast")[0]["kind"] == "podcast"
    # idempotent on a second open
    assert Database(tmp_path / "s.db").get_run("2026-11-01:podcast")

def test_add_clips_scopes_by_key_and_records_kind(tmp_path):
    from autoyt.db import Database
    db = Database(tmp_path / "s.db")
    row = {"start": 1, "end": 2, "duration": 1, "heading": "h", "title": "t", "description": "d",
           "category": None, "score": 9}
    db.add_clips("2026-11-02:podcast", [row])
    db.add_clips("2026-11-02:gta6_gameplay", [row], kind="gta6_gameplay")
    assert [c["kind"] for c in db.clips_for("2026-11-02:gta6_gameplay")] == ["gta6_gameplay"]
    assert len(db.clips_for("2026-11-02:podcast")) == 1  # not wiped by the sibling kind
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_kinds.py -v` → FAIL (`ImportError: run_key`, `no such column: kind`).

- [ ] **Step 3: Implement**

- `kinds.py`: add the three pure helpers (split on `:` then strip a `#n` suffix for `kind_of`).
- `db.py` SCHEMA: add `kind TEXT NOT NULL DEFAULT 'podcast'` to `clips`.
- In `Database.__init__` after `executescript`, add migrations (each guarded by column checks, all idempotent):

```python
cols = {r[1] for r in self.conn.execute("PRAGMA table_info(clips)")}
if "kind" not in cols:
    self.conn.execute("ALTER TABLE clips ADD COLUMN kind TEXT NOT NULL DEFAULT 'podcast'")
for table in ("runs", "steps", "clips"):
    self.conn.execute(f"UPDATE {table} SET day = day || ':podcast' "
                      "WHERE day NOT LIKE '%:%'")
```

- `add_clips(self, day: str, clips: list[dict], kind: str = "podcast")`: include `kind` in the INSERT.

- [ ] **Step 4: Run tests** — `pytest tests/test_kinds.py -v` → PASS. Then `pytest tests -q` → all pass (legacy tests use `today(s)` as the key; they still work because that key just becomes a plain podcast key).

- [ ] **Step 5: Commit**

```bash
git add autoyt/kinds.py autoyt/db.py tests/test_kinds.py
git commit -m "feat: composite day:kind run keys, legacy migration, clips.kind"
```

---

### Task 3: Daily quotas, roll persistence, batch plan

**Files:**
- Modify: `autoyt/kinds.py`, `autoyt/pipeline.py:33-52` (`clips_target` stays for podcast)
- Test: `tests/test_kinds.py`

**Interfaces:**
- Produces:
  - `gta6_target(s, db, rng, day: str) -> int`
  - `plan_day(s, db, now: float | None = None) -> list[Batch]` (`Batch` dataclass: `key, kind, clips, due`)
  - `pending_batches(s, db, now=None) -> list[Batch]` (plan minus runs already `done|failed|skipped`)
  - `next_due_batch(s, db, now=None) -> Batch | None` (most overdue pending batch whose `due <= now`; after window end, the most overdue pending regardless)
  - kv keys: `roll:{day}` (JSON), `batch_plan:{day}` (JSON list)

- [ ] **Step 1: Write the failing test**

```python
import random, time

def test_roll_and_plan_are_drawn_once_and_persist(env_settings):
    from autoyt.db import Database
    from autoyt.kinds import plan_day, gta6_target
    db = Database(env_settings.db_path)
    env_settings.ramp_up = False
    env_settings.clips_per_day = 7
    plan = plan_day(env_settings, db, now=time.time())
    keys = [b.key for b in plan]
    assert keys[0] == f"{plan[0].key.split(':')[0]}:podcast"
    n_pod = sum(b.clips for b in plan if b.kind == "podcast")
    assert n_pod == 7
    assert [b.clips for b in plan if b.kind == "podcast"] == [4, 3]
    gta = [b for b in plan if b.kind.startswith("gta6")]
    assert 2 <= sum(b.clips for b in gta) <= 6  # ramp start = 2 on day one
    assert plan_day(env_settings, db, now=time.time() + 100) == plan  # no re-roll

def test_gta6_split_has_both_kinds_when_at_least_two(env_settings):
    from autoyt.db import Database
    from autoyt.kinds import gta6_split
    db = Database(env_settings.db_path)
    for seed in range(30):
        talk, game = gta6_split(env_settings, random.Random(seed), 3)
        assert talk >= 1 and game >= 1 and talk + game == 3

def test_overdue_batch_selection(env_settings):
    from autoyt.db import Database
    from autoyt.kinds import next_due_batch, plan_day
    db = Database(env_settings.db_path)
    now = time.time()
    plan = plan_day(env_settings, db, now=now)
    first_due = min(b.due for b in plan)
    assert next_due_batch(env_settings, db, now=first_due - 1) is None
    got = next_due_batch(env_settings, db, now=first_due + 1)
    assert got is not None and got.due == first_due
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_kinds.py -v` → FAIL (`ImportError: plan_day`).

- [ ] **Step 3: Implement**

`gta6_target`: age from kv `first_upload_day` like `clips_target`; with `ramp_up` off use `min`; else pick the last ramp step whose `days <= age`; when age is past the last ramp entry, return `rng.randint(min, max)`; before that, the ramp value.

`gta6_split(s, rng, total)`: if total < 2 → `(total, 0)` or `(total, 0)` always talk (gameplay needs ≥1 too but cannot split 1 — owner wants both kinds; for 1 choose by `rng.random() < talk_weight`); else talk = 1..total-1 chosen with `talk_weight` bias (`round(total * talk_weight)`, clamped to `[1, total-1]`), game = total - talk.

`plan_day`:
- `day` from `kinds.today`-style helper (reuse `pipeline.today(s)`? avoid import cycle: compute in `kinds.py` with the same `ZoneInfo(s.tz)` logic).
- Roll stored under `roll:{day}`: `{"podcast": clips_target(s, db), "gta6": gta6_target(...), "talk": x, "gameplay": y}`.
- Podcast batches: chunk `podcast` count by `batching.max_clips_per_batch` (7 → [4,3]); keys `run_key(day, "podcast", i)`.
- One batch each for `gta6_talk` / `gta6_gameplay` when count > 0.
- Due times: parse `batching.window` (`"HH:MM"`) in `s.tz` into epochs for `day`; draw `len(batches)` random offsets, sort, enforce `min_gap_hours*3600` spacing by pushing later offsets (clamp into the window; if the window is too small for the gap, spread evenly). Assign the drawn order to a shuffled batch list so the run order is random too.
- Persist `batch_plan:{day}` JSON (keys, kinds, clips, due). Return the list.

`pending_batches`: load plan, drop keys whose run status is `done|failed|skipped`.
`next_due_batch`: among pending with `due <= now`, the smallest `due`; if none due but `now` is past window end, the smallest `due` anyway; else `None`.

- [ ] **Step 4: Run tests** — `pytest tests/test_kinds.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/kinds.py tests/test_kinds.py
git commit -m "feat: daily kind quotas, persisted batch plan, overdue drain"
```

---

### Task 4: Sources — kind filter, date windows, leak block

**Files:**
- Modify: `autoyt/sources.py:27-46` (`load_sources`, `active_sources`), `:55-62` (`_title_ok`), `:113` (`pick_video` signature)
- Modify: `config/sources.yaml` (new entries — Task 12)
- Test: `tests/test_misc.py` (additions)

**Interfaces:**
- `load_sources(s)` sets `src.setdefault("kind", "podcast")`.
- `active_sources(s, db, kind: str | None = None, day: str | None = None)` filters `enabled`, not disabled, `kind` match, and `after`/`until` ISO-date windows (`after <= day` when `after` set; `until >= day` when set).
- `pick_video(s, db, rng=None, kind: str = "podcast") -> Picked` filters by kind and passes the calendar day from the Settings timezone.

- [ ] **Step 1: Write the failing tests**

```python
def test_only_matching_kind_and_window_are_active(s, monkeypatch):
    from autoyt import sources
    db = Database(s.db_path)
    _write_sources(s, [
        {"name": "P", "url": "u1", "permission": "x"},
        {"name": "T", "url": "u2", "permission": "x", "kind": "gta6_talk"},
        {"name": "Old", "url": "u3", "permission": "x", "kind": "gta6_gameplay", "until": "2026-11-18"},
        {"name": "New", "url": "u4", "permission": "x", "kind": "gta6_gameplay", "after": "2026-11-19"},
    ])
    got = [x["name"] for x in sources.active_sources(s, db, "gta6_gameplay", day="2026-11-10")]
    assert got == ["Old"]
    got = [x["name"] for x in sources.active_sources(s, db, "gta6_gameplay", day="2026-11-20")]
    assert got == ["New"]
    assert [x["name"] for x in sources.active_sources(s, db, day="2026-11-10")] == ["P"]

def test_leak_titles_are_never_eligible():
    from autoyt.sources import _title_ok
    assert not _title_ok({"permission": "x"}, "GTA 6 leaked gameplay build")
    assert not _title_ok({"permission": "x"}, "CyberLeek footage part 2")
    assert _title_ok({"permission": "x"}, "GTA 6 review after the reveal")
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_misc.py -k "kind_and_window or leak" -v` → FAIL.

- [ ] **Step 3: Implement**

- `load_sources`: `src.setdefault("kind", "podcast")`.
- `_title_ok`: add a module constant `LEAK = re.compile(r"leak|leaked|cyberleek|dev build|debug build|early build", re.I)`; return False when it matches, before other checks.
- `active_sources(s, db, kind=None, day=None)` as specified; default `day` = today in `s.tz` (share the helper from `kinds.py` to avoid a second timezone implementation).
- `pick_video(..., kind="podcast")` passes `kind` and `day` through; `refresh_listing` unchanged.

- [ ] **Step 4: Run tests** — `pytest tests/test_misc.py -q` → PASS (existing pick tests call `pick_video(s, db, rng)`; the default keeps them green).
- [ ] **Step 5: Commit**

```bash
git add autoyt/sources.py tests/test_misc.py
git commit -m "feat: sources by kind and date window, leak-signal hard block"
```

---

### Task 5: Gameplay segment selector (no transcript)

**Files:**
- Create: `autoyt/segments.py`
- Test: `tests/test_segments.py`

**Interfaces:**
- `build_windows(energy: list[float], hop: float, total: float, n: int, clip_min: float, clip_max: float, skip_s: float, rng) -> list[dict]` → `[{"start","end","duration","score"}]`, non-overlapping, ordered by score desc.
- `scene_cuts(path: Path, threshold: float = 0.4) -> int` — number of scene transitions via `ffprobe`/`ffmpeg select`.
- `select_segments(s, db, video_id: str, duration: float, n: int, clip: dict, rng) -> list[dict]` — downloads audio, splits, measures 1 s RMS, calls `build_windows`.

- [ ] **Step 1: Write the failing test**

```python
def test_windows_center_on_peaks_and_never_overlap():
    from autoyt.segments import build_windows
    import random
    energy = [0.0] * 600
    for peak in (120, 300, 480):
        for i in range(peak - 3, peak + 4):
            energy[i] = 1.0
    w = build_windows(energy, hop=1.0, total=600.0, n=3, clip_min=20, clip_max=30,
                      skip_s=60, rng=random.Random(1))
    assert len(w) == 3
    assert all(20 <= x["duration"] <= 30 for x in w)
    assert w[0]["score"] >= w[-1]["score"]
    for a, b in zip(w, w[1:]):
        assert b["end"] <= a["start"] or a["end"] <= b["start"]

def test_windows_respect_skip_and_bounds():
    from autoyt.segments import build_windows
    import random
    energy = [1.0] * 100
    w = build_windows(energy, 1.0, 100.0, 5, 10, 20, skip_s=40, rng=random.Random(2))
    assert all(x["start"] >= 40 and x["end"] <= 100 for x in w)
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_segments.py -v` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

`build_windows`: smooth energy with a 5 s moving average; candidate center per local maximum above the global mean; length `rng.uniform(clip_min, clip_max)`; center the window on the peak, clamp into `[skip_s, total]`; greedily accept highest-score non-overlapping candidates until `n`. Score = smoothed peak height.

`select_segments`: `audio = ytdlp.download_audio(s, video_id, work)` → `chunks = transcribe.split_audio(audio, work)` → concatenate `transcribe.load_wav` and compute per-hop RMS (hop 1 s) with numpy → `build_windows(...)` → map to clip dicts `{"start","end","duration","score","heading": None}` (heading filled by pipeline), delete the audio/chunks.

- [ ] **Step 4: Run tests** — `pytest tests/test_segments.py -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/segments.py tests/test_segments.py
git commit -m "feat: audio-energy gameplay segment selector"
```

---

### Task 6: `layout: full` render template

**Files:**
- Modify: `autoyt/render.py:23-40` (add `_full_filter`), `:43-91` (`build_command` branch)
- Test: `tests/test_misc.py`

**Interfaces:**
- `build_command(..., layout_name="full")` produces no bottom input, no `vstack`; a single `[v]` scaled/cropped to `layout.width × layout.height` with the ASS burn-in.
- `_full_filter(layout, crop, zoom) -> str` (cover-crop; `custom` crop respected; `zoom > 1` tightens first).

- [ ] **Step 1: Write the failing test**

```python
def test_full_layout_has_no_gameplay_panel(s):
    from pathlib import Path
    from autoyt.render import build_command
    cmd = build_command(s, section=Path("a.mp4"), ass_name="a.ass", out_tmp=Path("o.mp4"),
                        duration=30.0, gameplay=None, music=None, layout_name="full")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "vstack" not in graph and "[bot]" not in graph
    assert "scale=1080:1920" in graph.replace("1080", "1080") or "scale=1080:1920" in graph
    assert "ass=a.ass" in graph
    # no second -i for a gameplay file
    assert cmd.count("-i") == 1
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_misc.py -k full_layout -v` → FAIL (`vstack` present).

- [ ] **Step 3: Implement**

`_full_filter`: `crop=w='min(iw,ih*{W}/{H})':h='min(ih,iw*{H}/{W})'` then `scale={W}:{H}:flags=lanczos`, honor `custom` crop and `zoom` like `_top_filter`. In `build_command`, when `layout_name == "full"`: skip the gameplay input and `[bot]`/`vstack` lines; graph = `[_full_filter(...), f"[top]ass={ass_name}[v]"]`; audio branch unchanged (music=None yields the loudnorm-only path).

- [ ] **Step 4: Run tests** — `pytest tests/test_misc.py -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/render.py tests/test_misc.py
git commit -m "feat: full-screen vertical render layout"
```

---

### Task 7: Captions anchor, heading/credit restyle, no-caption mode

**Files:**
- Modify: `autoyt/subtitles.py:57-112`, `autoyt/config.py:37-48` (layout keys), `config/settings.yaml`
- Test: `tests/test_subtitles_slots.py`

**Interfaces:**
- `build_ass(clip_words, heading, credit, duration, layout, anchor: str = "top", captions: bool = True) -> str`
- `anchor="top"` → `sub_y = layout.get("subtitle_y", 620)`; `anchor="seam"` keeps the old `top_h - 40` (only used if a caller asks).
- `captions=False` → heading/credit only, no `Dialogue: 2` lines.
- `layout` gains `"heading_size": 64`, `"subtitle_y": 620`, `"credit_seconds": 1.5`.

- [ ] **Step 1: Write the failing tests**

```python
def test_captions_are_top_anchored_not_on_the_seam():
    from autoyt.subtitles import build_ass
    layout = {"width": 1080, "height": 1920, "top_height": 1150, "font": "DejaVu Sans",
              "heading_size": 64, "subtitle_size": 74, "subtitle_y": 620,
              "credit_seconds": 1.5}
    words = [{"w": "hello", "s": 0.2, "e": 0.5}, {"w": "world", "s": 0.6, "e": 0.9}]
    ass = build_ass(words, "Heading", "Credit", 2.0, layout)
    assert "pos(540,620)" in ass
    assert "pos(540,1110)" not in ass  # old seam position (top_h-40)
    assert "Dialogue: 0,0:00:00.00,0:00:01.50,Credit" in ass

def test_captions_off_for_gameplay():
    from autoyt.subtitles import build_ass
    layout = {"width": 1080, "height": 1920, "top_height": 1150, "font": "DejaVu Sans",
              "heading_size": 64, "subtitle_size": 74, "subtitle_y": 620, "credit_seconds": 1.5}
    ass = build_ass([], "HOOK LINE", "Rockstar", 30.0, layout, captions=False)
    assert "Dialogue: 2" not in ass and "HOOK LINE" in ass
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_subtitles_slots.py -v` → FAIL.

- [ ] **Step 3: Implement**

- `sub_y` from the anchor; heading box padding 56 → 40 and `box_top` 140 → 120 with `heading_size` 64 (update DEFAULTS + yaml so heights compute consistently).
- Credit dialogue: `End = _ts(layout.get("credit_seconds", 1.5))`, positioned `\an7\pos(60, {H - 64})` on full layout or bottom of the source panel (`top_h - 64`) otherwise — pass a `credit_y` key from the caller if provided; default to `H - 64`.
- `captions=False` skips the group loop.
- Update any existing assertions in `tests/test_subtitles_slots.py` that encode the old seam position (only where the spec changes behavior; keep slot-count assertions).

- [ ] **Step 4: Run tests** — `pytest tests/test_subtitles_slots.py tests/test_misc.py -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/subtitles.py autoyt/config.py config/settings.yaml tests/test_subtitles_slots.py
git commit -m "feat: top-anchored captions, restyled heading/credit, caption-off mode"
```

---

### Task 8: Per-kind QC and upload metadata fallbacks

**Files:**
- Modify: `autoyt/qc.py:10-26` (takes the clip cfg it is given — no signature change; callers pass `clip_cfg(s, kind)`), `autoyt/pipeline.py:379-416` (`_tags`, `_hashtags` handle `category=None`)
- Test: `tests/test_misc.py`

**Interfaces:**
- `qc.check_clip(path, cfg, layout)` already reads only `min_seconds`/`max_seconds` from `cfg`; the pipeline passes the kind's cfg.
- `Runner._tags/_hashtags` never produce the string `"none"`; gameplay clips fall back to a `kind`-derived tag (`gta`, `gta6`).

- [ ] **Step 1: Write the failing test**

```python
def test_metadata_survives_null_category():
    from autoyt.pipeline import Runner
    r = {"context_line": "", "description": "d", "category": None, "tags": '["gta 6"]'}
    run = {"credit": "Some Creator", "video_title": "GTA V stunt"}
    tags = Runner._tags(r, run)
    assert "none" not in tags and "gta 6" in tags
    assert "#none" not in Runner._hashtags(r, run)
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_misc.py -k null_category -v` → FAIL (`category` `None` → `.lower()` crash or `"none"` tag).

- [ ] **Step 3: Implement**

Filter `None`/empty/`"none"` out of the base tag list before use; when `category` is falsy use `"gta"` for gameplay-kind rows (kind is in `r["kind"]`).

- [ ] **Step 4: Run tests** — `pytest tests/test_misc.py -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/pipeline.py autoyt/qc.py tests/test_misc.py
git commit -m "fix: metadata and QC for gameplay clips with no category"
```

---
### Task 9: GTA talk selection prompt + music-presence guard

**Files:**
- Modify: `autoyt/select_clips.py:18-49` (add `SYSTEM_BY_KIND`), `:105` (`call_gemini` system param), `:146` (`collect_candidates` kind param)
- Modify: `autoyt/validate.py` (music guard helper)
- Test: `tests/test_misc.py`

**Interfaces:**
- `collect_candidates(s, sentences, title, credit, per_window=None, kind="podcast")`
- `call_gemini(s, prompt, retries=5, system=None)`
- `validate.music_ratio(wav: Path) -> float` — fraction of 1 s frames whose spectral flatness indicates non-speech content (0..1).
- `build_clip(..., music_ratio: float | None = None)` rejects when `music_ratio > cfg.get("max_music_ratio", 0.35)`.

- [ ] **Step 1: Write the failing test**

```python
def test_gta_talk_prompt_has_anti_reaction_rules():
    from autoyt.select_clips import system_for
    text = system_for("gta6_talk")
    assert "trailer" in text.lower() and "music" in text.lower()
    assert "reaction" in text.lower()
    assert "gta" in text.lower()
    assert system_for("podcast") != text
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_misc.py -k gta_talk_prompt -v` → FAIL.

- [ ] **Step 3: Implement**

- `SYSTEM_BY_KIND = {"podcast": SYSTEM, "gta6_talk": SYSTEM_GTA_TALK, "gta6_gameplay": SYSTEM}` where `SYSTEM_GTA_TALK` keeps the podcast rules and adds: topic must be GTA6 (reviews, news, breakdowns, hot takes); **skip any moment where trailer footage/audio or music is playing, and skip segments describing a reaction video**; title rule updated for all kinds: "the title must sell the payoff or the open question, not describe the topic (no invented facts)".
- `system_for(kind)` helper; `call_gemini(..., system=None)` uses `system or SYSTEM`.
- `music_ratio`: decode 16 kHz mono via ffmpeg to a temp wav, split into 1 s frames with numpy, compute spectral flatness per frame (FFT magnitude → geometric/arithmetic mean ratio); music pads/sustained tones score high, speech lower. Keep it cheap and documented as a heuristic.
- Pipeline hook comes in Task 10 (slice-level check before adding a talk clip to the render queue).

- [ ] **Step 4: Run tests** — `pytest tests/test_misc.py -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/select_clips.py autoyt/validate.py tests/test_misc.py
git commit -m "feat: GTA talk prompt, hook-style titles, music-presence validator"
```

---

### Task 10: Pipeline kind routing

**Files:**
- Modify: `autoyt/pipeline.py` (branch by kind; per-run-key work dirs; gameplay path; skip semantics)
- Modify: `autoyt/monitor.py` (call per calendar day — signature unchanged)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- `Runner.attempt(run_key: str) -> str` (`done|failed|retry|skipped`)
- `Runner.kind(run_key)` → `kinds.kind_of`
- `_run_video(run_key, kind)`; new `_run_gameplay(run_key)`; `_select(run_key, run, tr, n, kind)` uses `clip_cfg(s, kind)` and `collect_candidates(..., kind=kind)`
- Callers of `today(s)` inside the runner become `kinds.day_of(run_key)` where a calendar day is meant (maintenance/monitor/summary date).
- `_summary_email(run_key)` subject carries the kind label.

- [ ] **Step 1: Write the failing tests** (fake pick/segments like the existing `env` fixture)

```python
def test_gameplay_batch_needs_no_transcript_or_captions(env, monkeypatch):
    s, db, runner, calls = env
    from autoyt.pipeline import Runner as R
    key = "2026-11-10:gta6_gameplay"
    monkeypatch.setattr("autoyt.pipeline.pick_video",
                        lambda s_, db_, rng=None, kind="podcast": __import__("autoyt.sources", fromlist=["Picked"])
                        .Picked({"name": "GTA Clips", "credit": "GTA Clips"}, "gv1", "GTA V chaos", 1800.0,
                                "https://youtu.be/gv1"))
    monkeypatch.setattr("autoyt.pipeline.select_segments",
                        lambda *a, **k: [{"start": 100.0, "end": 130.0, "duration": 30.0, "score": 1.0}])
    monkeypatch.setattr("autoyt.transcribe.transcribe_section",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no whisper for gameplay")))
    s.mode = "dry_run"
    assert runner.attempt(key) == "done"
    clips = db.clips_for(key)
    assert len(clips) == 1 and clips[0]["kind"] == "gta6_gameplay" and clips[0]["category"] is None

def test_kind_with_no_sources_is_skipped_not_failed(env, monkeypatch):
    from autoyt.errors import SourceRejected
    s, db, runner, calls = env
    monkeypatch.setattr("autoyt.pipeline.pick_video",
                        lambda *a, **k: (_ for _ in ()).throw(SourceRejected("no sources")))
    key = "2026-11-10:gta6_talk"
    assert runner.attempt(key) == "skipped"
    assert db.get_run(key)["status"] == "skipped"
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_pipeline.py -q` → FAIL (gameplay kind still demands a transcript; skipped status unknown).

- [ ] **Step 3: Implement**

- `attempt`: unchanged flow, but resolve `kind = kinds.kind_of(run_key)`; on `StepFailed("three videos in a row were unusable")` raised from `execute` for a non-podcast kind, set `status="skipped"`, notify once, return `"skipped"`.
- `execute`: maintenance/monitor use `kinds.day_of(run_key)`; pick loop calls `pick_video(..., kind=kind)`; podcast keeps the existing 3-try loop; non-podcast kinds raise the same `StepFailed` after 3 rejections so `attempt` can mark skipped.
- `_run_video`: `work = s.work_dir / run_key` (replace `/` with `_` when building paths — the key contains `:`/`#` only, so no change needed); `db.kv_set(f"dur:{run_key}", ...)`; `db.mark_step(run_key, ...)`.
- Transcript stage only for podcast/gta6_talk. Gameplay: `_run_gameplay` = pick video → `segments.select_segments(...)` (clip cfg from `clip_cfg(s, "gta6_gameplay")`, n = planned batch clips) → insert clip rows with `heading` from a fixed hook pool (`segments.HOOKS`: e.g. "GTA 6 just changed everything", "This is why everyone is playing GTA", "Wait for the end", "POV: you played GTA for 10 years") chosen by rng and `category=None`, `title` = f"{video title} — {hook}" trimmed ≤ 80 → `_clips` → `_finalize`.
- `_clips`/`_process_clip`: pass `kind`; skip Whisper and `assets.choose_gameplay` for gta kinds; `layout_name="full"` and `music=None` for gta kinds; `build_ass(..., captions=(kind == "gta6_talk"), anchor="top")`; for talk kind run `validate.music_ratio` on the downloaded section before rendering — reject the clip (status failed, continue) when above threshold.
- `_finalize`: rmtree `work_dir / run_key`; output dir `out_dir / run_key`.
- `_summary_email(run_key)`: subject `Daily summary {day} [{kind}]: ok/total clips`; body unchanged plus the kind.
- `archive_day`, `_drop_video`, `clear_steps`, `DELETE FROM clips WHERE day=?` all take the run_key as-is.

- [ ] **Step 4: Run tests** — `pytest tests/test_pipeline.py -q` → PASS, and `pytest tests -q` → PASS. Existing tests pass `today(s)` as the key (kind `podcast` default), which must remain valid.

- [ ] **Step 5: Commit**

```bash
git add autoyt/pipeline.py autoyt/monitor.py tests/test_pipeline.py
git commit -m "feat: pipeline routes podcast, gta6_talk and gta6_gameplay batches"
```

---

### Task 11: Batch scheduler

**Files:**
- Modify: `autoyt/scheduler.py:45-76`
- Test: `tests/test_kinds.py`

**Interfaces:**
- `tick(s, db, runner, now: float | None = None) -> str` — returns `idle | waiting | blocked:<why> | done | failed | retry | skipped | started:<run_key>`.
- Exactly one batch attempt per tick; a day is `idle` only when every planned batch is `done|failed|skipped`.

- [ ] **Step 1: Write the failing test**

```python
def test_tick_starts_one_due_batch_and_never_more(env_settings, monkeypatch):
    import time
    from autoyt import scheduler
    from autoyt.db import Database
    from autoyt.kinds import plan_day
    db = Database(env_settings.db_path)
    plan = plan_day(env_settings, db, now=time.time())
    calls = []
    class FakeRunner:
        def attempt(self, key):
            calls.append(key); return "done"
    now = max(b.due for b in plan) + 1  # everything overdue
    assert scheduler.tick(env_settings, db, FakeRunner(), now=now).startswith("started:")
    assert len(calls) == 1
    assert scheduler.tick(env_settings, db, FakeRunner(), now=now).startswith("started:")
    assert len(calls) == 2  # draining one per tick
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_kinds.py -k tick -v` → FAIL (`tick` takes no `now`, treats the day as one run).

- [ ] **Step 3: Implement**

`tick`:
1. `day` from `kinds` helper; `plan_day(s, db)` (creates/normalises the persisted plan).
2. If every batch run is `done|failed|skipped` → `"idle"`.
3. Preconditions; if blocked → `"blocked: ..."`.
4. `batch = next_due_batch(s, db, now)`; if `None` → `"waiting"`.
5. `res = runner.attempt(batch.key)`; log the batch key; return `res` (prefixed `started:` is unnecessary — return `res` directly; the test asserts `startswith("started:")` only if you keep the prefix — implement the plain return and adjust the test accordingly).
6. Note: the `attempt` wrapper already handles per-run retries/backoff via `next_try_at`; `next_due_batch` must also skip a batch whose run has `next_try_at > now`.

`daemon`: startup recovery resets `running`→`pending` (unchanged, works with keys).

- [ ] **Step 4: Run tests** — `pytest tests/test_kinds.py -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/scheduler.py tests/test_kinds.py
git commit -m "feat: scheduler runs one due batch per tick and drains overdue batches"
```

---

### Task 12: Gameplay library bar-crop fix and re-fetch

**Files:**
- Modify: `autoyt/assets.py:93-147` (`_detect_crop` → bar detection incl. blur bars; content-resolution guard; fetch validation), `:213-262` (`fetch_gameplay` accepts `rebuild`), `autoyt/main.py:82` (`fetch-gameplay --rebuild`)
- Test: `tests/test_misc.py`

**Interfaces:**
- `_detect_bars(raw: Path) -> tuple[int, int, int, int] | None` — bbox of real content, using per-row/column luminance variance across sampled frames (replaces pure `cropdetect`; keep `cropdetect` as fallback).
- `prep_gameplay(..., min_content_side: int = 600)` raises `ValueError` when the content box short side is below the floor (candidate is rejected by `fetch_gameplay`).
- `cmd_fetch_gameplay` gains `--rebuild` (delete `index.json` + all files before fetching).

- [ ] **Step 1: Write the failing test**

```python
def test_blur_bars_are_cropped_away(s):
    import subprocess
    from autoyt.assets import _detect_bars
    raw = s.data_dir / "bars.mp4"
    # 720x1280 with 300px solid bars top and bottom -> content is 720x680
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=s=720x680:r=30:d=2", "-vf",
                    "pad=720:1280:0:300:color=black", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw)],
                   check=True)
    w, h, x, y = _detect_bars(raw)
    assert abs(y - 300) <= 12 and abs(h - 680) <= 24 and w == 720
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_misc.py -k blur_bars -v` → FAIL (pure cropdetect returns the padded frame or nothing).

- [ ] **Step 3: Implement**

Sample 6 evenly spaced frames at 360p gray via `ffmpeg -vf "fps=6/duration,scale=360:-2,format=gray" -f rawvideo -`, build a per-row std profile with numpy across frames; rows whose std stays under 8 % of the max row std for ≥ 90 % of samples at the top/bottom edges are bars; convert back to source pixels; fall back to the existing `cropdetect` result when the variance method finds nothing. Also guard `prep_gameplay` with `min_content_side`. In `fetch_gameplay`, clear `index` entries whose files failed validation.

- [ ] **Step 4: Run tests** — `pytest tests/test_misc.py -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/assets.py autoyt/main.py tests/test_misc.py
git commit -m "fix: strip black/blur bars from gameplay footage, guard low-res content"
```

---

### Task 13: CLI for multi-batch days

**Files:**
- Modify: `autoyt/main.py:27-52` (`cmd_run`, `cmd_status`), `:99-124` (`cmd_render_clip`), `:141-155`
- Test: `tests/test_kinds.py`

**Interfaces:**
- `cmd_run`: iterates `plan_day` batches for the day (or the run key given via `--day`); `--force` resets every batch run of that day; `--fresh` archives every batch run (`archive_day(key)`); prints one line per batch.
- `cmd_status`: prints `run_key`, status, kind, attempts; clips list unchanged.
- `render-clip --layout` choices become `fit_blur|center_crop|full`, plus `--no-captions`.

- [ ] **Step 1: Write the failing test**

```python
def test_cmd_run_walks_all_batches(env_settings, monkeypatch, capsys):
    import autoyt.main as m
    from autoyt.kinds import plan_day
    from autoyt.db import Database
    calls = []
    monkeypatch.setattr(m.Runner, "attempt", lambda self, key: calls.append(key) or "done")
    m.cmd_run(env_settings, type("A", (), {"fresh": False, "force": False, "day": "2026-11-10"})())
    plan = plan_day(env_settings, Database(env_settings.db_path))
    assert calls == [b.key for b in plan]
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_kinds.py -k cmd_run -v` → FAIL (single attempt).

- [ ] **Step 3: Implement** as specified; `--day` accepts either `2026-11-10` (all batches that day) or a full run key (that batch only). `render-clip` passes `captions=not a.no_captions` and `anchor="top"`.

- [ ] **Step 4: Run tests** — `pytest tests -q` → PASS.
- [ ] **Step 5: Commit**

```bash
git add autoyt/main.py tests/test_kinds.py
git commit -m "feat: CLI runs and reports per-batch days, full-screen render test"
```

---

### Task 14: Content config — GTA source groups

**Files:**
- Modify: `config/sources.yaml`
- Modify: `config/gameplay.yaml` (comment stating SS/TR2 remain for podcast only)

**Interfaces:** source entries carry `kind` (`podcast` default), `permission: fair_use_credit`, and optional `after`/`until` ISO dates.

- [ ] **Step 1: Discover candidate channels** (needs network)

```bash
yt-dlp --flat-playlist "ytsearch12:gta 6 news review" --print "%(channel)s\t%(channel_id)s\t%(title)s" | sort -u | head -20
yt-dlp --flat-playlist "ytsearch12:gta 5 gameplay no commentary" --print "%(channel)s\t%(channel_id)s\t%(title)s" | sort -u | head -20
```

- [ ] **Step 2: Add entries** — 3–5 `kind: gta6_talk` channels (reviews/news/breakdowns; no reaction channels), 2–3 `kind: gta6_gameplay` channels with `until: 2026-11-18` (GTA V footage), and 2–3 with `after: 2026-11-19` (GTA VI). Each: `permission: fair_use_credit`, `title_include: ["(?i)gta\\s*(5|v|6|vi)"]`, `title_exclude: ["(?i)react|reaction|responds to|trailer reaction"]`, `layout: full`, sensible `min_minutes`/`max_minutes` (talk 15–180, gameplay 10–120).

- [ ] **Step 3: Verify eligibility**

```bash
docker compose run --rm autoyt autoyt check-sources
```
Expected: each new entry prints a non-zero eligible count; any channel with 0 is removed or fixed. Record the counts in the commit message.

- [ ] **Step 4: Commit**

```bash
git add config/sources.yaml config/gameplay.yaml
git commit -m "config: GTA talk and gameplay source groups with launch-date gating"
```

---

### Task 15: Docs, image rebuild, container test

**Files:**
- Modify: `README.md`, `PLAN.md`
- No code changes.

- [ ] **Step 1: Document** — README: three kinds, batch schedule knobs, `fetch-gameplay --rebuild`, GTA audio policy, leak/trailer-reaction exclusions, how to check `status` per batch. PLAN: update the status line and the channel eligibility table (Clip Roty / GTA groups).

- [ ] **Step 2: Local full suite**

Run: `python -m pytest tests -q`
Expected: all pass (original 52 + new tests).

- [ ] **Step 3: Rebuild the image**

```bash
docker compose build --no-cache
```
Expected: build succeeds.

- [ ] **Step 4: Run the suite inside the container**

```bash
docker compose run --rm --entrypoint sh autoyt -c "pip install -q pytest && python -m pytest tests -q"
```
Expected: same pass count. (ffmpeg is in the image; the gameplay/full-layout tests run for real.)

- [ ] **Step 5: Container smoke test — full-screen render**

```bash
mkdir -p data/smoke
docker compose run --rm autoyt sh -c "
  ffmpeg -y -loglevel error -f lavfi -i testsrc2=s=1280x720:r=30:d=12 \
    -f lavfi -i sine=frequency=300:d=12 -c:v libx264 -c:a aac -shortest /app/data/smoke/in.mp4 &&
  autoyt render-clip --input /app/data/smoke/in.mp4 --start 0 --end 6 --heading 'Smoke test' \
    --credit 'Test' --layout full --no-captions --output /app/data/smoke/out.mp4 &&
  ffprobe -v error -show_entries stream=width,height -of csv=p=0 /app/data/smoke/out.mp4"
```
Expected: `1080,1920`.

- [ ] **Step 6: Container smoke test — dry-run day plan**

```bash
docker compose run --rm -e AUTOYT_MODE=dry_run autoyt autoyt status
docker compose run --rm -e AUTOYT_MODE=dry_run autoyt autoyt run --day 2026-11-10
```
Expected: status lists one run per planned batch; `run` prints one result per batch (network-bound steps may fail on source availability — the important check is that batches are attempted independently and a missing kind skips instead of failing the day).

- [ ] **Step 7: Commit**

```bash
git add README.md PLAN.md
git commit -m "docs: kinds, batching, GTA content policy, rebuild instructions"
```

---

## Execution notes

- The spec says "PK rebuild to (day, kind)"; this plan implements the same keying by storing the composite key in the existing `day` columns (Task 2). No SQLite PK rebuild is needed because all queries already key on that column — same observable behavior, dramatically lower migration risk. Flag this to the owner when handing over.
- The spec's "summary email once per day" becomes one summary per batch (subject includes the kind), because batches finish at different times and a day-level dedupe key would drop later batches' reports.
- Trailer/pre-launch official-footage sourcing from the first draft is dropped per owner decision; `gta6_gameplay` runs on GTA V footage until 19 Nov 2026.
- Source curation (Task 14) needs network access and human judgment; leave entries commented-out or disabled if a channel cannot be verified.

