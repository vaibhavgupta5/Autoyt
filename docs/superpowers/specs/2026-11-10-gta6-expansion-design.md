# Design: GTA6 video kinds + quality fixes + 10–13 videos/day

Date: 2026-11-10
Status: approved-pending-final-read (decisions recorded below)
Builder check: done 2026-11-10 — buildable, corrections folded in (§2, §6, §8)

## Intent and why

The channel (@TheRiddleGod) currently publishes 3 auto-generated podcast-clip
Shorts/day via the autoyt pipeline. Three goals:

1. **Scale**: go from 3/day to **7 podcast-style/day**, plus **3–6 GTA6-style
   videos/day** (random count/day), total **10–13/day**, via a gradual ramp.
2. **New content kinds**: GTA6 videos in **two** full-screen formats —
   (a) people talking/reacting about GTA6, (b) GTA6 gameplay. Neither format
   may contain Subway Surfers or Temple Run footage in any way.
3. **Quality**: fix the visual defects found in published shorts (confirmed by
   frame inspection of zq-4QX4c99w and AxaFpZ4BMkE).

Demographic context: GTA VI launches **19 Nov 2026**. Pre-release anticipation
is at peak; post-release the gameplay kind becomes self-feeding.

## Owner decisions (recorded)

- **GTA6 permission posture**: `fair_use_credit` — credit + removal note in
  every description; accept takedown/strike risk; the existing monitor
  auto-disables a source on strike.
- **No trailer-reaction content**: reviews/discussion/talk only; trailer
  reactions excluded (they embed the trailers' licensed music).
- **Gameplay kind active from day 1** (owner decision: volume for reach):
  pre-launch it draws from **GTA V / GTA Online gameplay creators** (the
  series' previous entry — abundant vertical clips), switching automatically
  to GTA VI sources at launch via `after`/`until` flags.
- **Ramp**: gradual. Podcast 3→5→7 over ~1 week (existing ramp); GTA6 starts
  at 2/day, +1/day, settling at random 3–6. Full 10–13 volume ~10 days in.
- **Audio policy for GTA kinds**: no added background music on any GTA6
  video. `gta6_talk` = source speech only. `gta6_gameplay` = **original
  audio** (game SFX/radio as-is), LUFS-normalized only. The existing
  ducked-music mix remains for the podcast kind only.

## Anti-goals (would make this a failure)

- Using leaked GTA6 footage ("CyberLeek", teapotuberhacker) in any video.
  Take-Two is actively subpoenaing accounts over this; guaranteed strikes.
- Shipping trailer-reaction content in any kind — reaction videos embed the
  trailers' licensed music.
- Leaving the Subway Surfers "blur-bar" defect in shipped videos.
- Publishing 10–13/day from a brand-new channel (6 subs, 0–3 views) with no
  ramp — spam-suppression risk.

## Goals / non-goals

- Goals: kind routing in the pipeline, two new full-screen render templates,
  GTA6 source selection, per-day kind quotas, quality fixes, schedule scaling.
- Non-goals: new channels, re-branding the existing channel, TTS voiceover
  (audio stays as source speech / game audio), TikTok/IG cross-posting.

## Approaches considered

For the GTA6 form factor:

- **(A) Same split-screen, GTA6-themed** (bottom panel = GTA6 gameplay).
  Least code, but keeps the two-panel awkwardness.
- **(B) Full-screen GTA6 gameplay** — vertical cropped gameplay, no speech.
- **(C) Full-screen talking/reaction clips** — GTA6 discussion moments,
  vertical cropped, word captions.
- **(D) Both B and C** ← **chosen**. User picked both explicitly. Costs: both
  templates must be built, and the gameplay kind needs selection logic without
  speech transcripts.

## Decided design

### 1. New first-class concept: video `kind`

Clips today carry only Gemini `category` (funny/informative/deep) and the
bottom gameplay panel is chosen at random across all gameplay entries. This
becomes three kinds:

| Kind | Look/day | Source | Bottom gameplay |
|---|---|---|---|
| `podcast` | 7/day (existing split-screen) | existing podcast allowlist | SS/TR2 as today |
| `gta6_talk` | 3–6/day pre-launch; random share post-launch | new GTA6 review/commentary allowlist | none — full screen |
| `gta6_gameplay` | random share of 3–6/day, **active from day 1** | GTA V/Online gameplay creators now; GTA VI creators from 19 Nov | none — full screen |

- GTA6 total/day: **uniform random 3–6** (after ramp). Pre-launch the split
  between talk/gameplay follows configurable weight (default random 50/50,
  min 1 of each when count ≥ 2); at launch the gameplay source group flips
  from GTA V to GTA VI automatically and the mix continues unchanged.
- **No trailer-reaction content in any kind** (owner decision): reaction
  videos embed the trailers' licensed music. See §3 and §6.
- The day's GTA6 roll (count + split) is stored in DB kv (`gta6_roll:{day}`)
  so retries never re-roll quotas.
- Config (`config/settings.yaml`): `kinds: {podcast: {...}, gta6: {min, max,
  talk_weight, ramp}}`. Per-kind clip duration bounds live here too
  (podcast 50–55 s; gameplay 20–45 s) and `qc.py` reads the per-kind bounds
  instead of the global rule.

### 2. Pipeline restructure: one run per (day, kind), executed in spread-out batches

Today the scheduler runs one attempt/day for a single picked video. The runner
loops kinds: each day runs **one pipeline sequence per kind present**, tracked
as `(day, kind)` so each kind retries independently.

**Load-spreading (owner request):** a 10–13-clip day is heavy (parallel slice
downloads + serial ffmpeg renders). The day's work is therefore split into
**batches that run at randomized times while the system is on**, instead of
one long run:

- A **batch = one kind's run**, split further when large: podcast (7 clips)
  splits into two runs of ≤4 clips (`max_clips_per_batch: 4`), so a typical
  day has **4 batches**: podcast-a (4), podcast-b (3), gta6_talk,
  gta6_gameplay.
- At the first tick of each day, the scheduler draws a random due time per
  batch inside the awake window (default 08:00–22:00, config
  `batching.window`), with a minimum separation (default ≥2 h,
  `batching.min_gap_hours`), and stores the plan in kv (`batch_plan:{day}`)
  so retries never re-randomize.
- Each 15-min tick: if a pending batch is due **and** preconditions pass
  (battery, disk, pause file — checked per batch), run exactly **one** batch.
  If the laptop was off and several batches are overdue, run the most
  overdue first, one per tick — load stays spread even after a late boot.
- A batch is complete when its kind's clips for the day are rendered and
  scheduled; upload `publishAt` slots still span the 20 h window globally,
  so publishing order is independent of render order.

Builder-check corrections folded in — the following all become kind-scoped:

- **Work/output dirs**: `work_dir/<day>/<kind>/` and `out_dir/<day>/<kind>/`
  (prevents transcript/slice/clip filename collisions and the `_finalize`
  rmtree deleting a sibling kind's in-flight state).
- **kv keys**: `dur:{day}` → `dur:{day}:{kind}`; summary email sent once per
  day but reports counts per kind (notify `summary` key stays day-level).
- **`clips` queries**: `clips_for`, `add_clips` (which currently DELETEs by
  day — would wipe a sibling kind's rows) scope by `(day, kind)`.
- **`runs`/`steps` tables**: PK changes to `(day, kind)` / `(day, step, kind)`.
  SQLite cannot ALTER a PK — this is a **table rebuild** (create new, copy,
  drop, rename) inside a migration helper, plus versioned `CREATE TABLE` for
  fresh DBs.
- **Scheduler `tick()`**: aggregates across kinds and batches — draws/loads
  the day's batch plan, runs at most one due batch per tick (preconditions
  per batch), and a day is done only when every batch is done/skipped.
- **`archive_day`, `cmd_run --force/--fresh`**: operate across all kinds of
  the day.

### 3. Sourcing

- **gta6_talk**: new entries in `config/sources.yaml`, each tagged
  `kind: gta6_talk` and `permission: fair_use_credit`, with `title_include`
  regex matching GTA6 variants (`GTA\s*6`, `GTA\s*VI`, case-insensitive) so
  the picker only chooses videos actually about the game, and
  `title_exclude` adding `react|reaction|responds to` — trailer-reaction
  content is excluded at the source level (owner decision: reaction videos
  embed trailer music). Target channel types: reviews, breakdowns,
  discussion/podcast commentary, news/explainers. Curate the allowlist by
  hand before go-live (existing eligibility checks: duration, English,
  public). Credit + removal note in every description; monitor auto-disables
  on strike (existing behavior, now the accepted risk posture).
- **gta6_gameplay**: **active from day 1** (owner decision). Two source
  groups with date gating:
  - *Now → 18 Nov* (`until: 2026-11-19`): GTA V / GTA Online gameplay
    creators (stream VODs, montage channels, RP clips) — vertical-friendly
    action footage, clearly titled GTA 5/Online. Regular `used_videos`
    anti-reuse applies.
  - *19 Nov onward* (`after: 2026-11-19`): the same kind flips to a GTA VI
    source group of creators posting console gameplay; the pipeline path is
    identical, only the allowlist changes, so launch day needs zero code.
  - **Segment anti-repeat**: gameplay segments tracked in `gameplay_usage`
    (extended beyond SS/TR2) with the same 7-day recent-window rule, so
    3–6 clips/day never replay overlapping windows of the same footage.
- **Hard block**: any candidate whose title/credit matches leak signals
  (leaked, leak footage, early build, CyberLeek, debug footage) is rejected —
  wired into the existing `title_exclude` check path plus a source-level
  hard filter.

### 4. Full-screen render template (`layout: full`)

New template in `render.py`, selected by kind instead of the hardcoded stack:

- **Crop**: scale source to fill 1080×1920 (16:9 → 9:16). Center-crop by
  default; reuse existing per-source `crop/zoom` config for manual control.
- **gta6_talk**: word-by-word captions burned in, **anchored top-safe over
  the speaker** (new anchor param on the shared ASS builder; also fixes the
  seam-caption defect globally, §7). **No background music added** (owner
  decision) — source speech only, LUFS-normalized.
- **gta6_gameplay**: no captions (no speech), a short on-screen heading hook
  in the top area; audio = **original source audio** (game SFX/radio as-is),
  LUFS-normalized, no added music. Heading text source: Gemini generates
  from title+context; a rotating template pool is the fallback when Gemini
  output is unusable.
- **qc**: per-kind duration bounds (§1); same silence/black-frame checks.

### 5. Counts and schedule

- Podcast: ramp 3 → 5 (day 4) → 7 (day 8) via existing `ramp` mechanism.
- GTA6: day 1–3: 2/day; day 4+: random 3–6/day (min floor via ramp table).
- Slots: `target_span_hours` stays 20; `min_gap` 45 → 30 min, `max_gap`
  180 → 120 so 10–13 uploads keep ~1.5 h average spacing.
- Uploads remain `scheduled` mode with `publishAt`.

### 6. Selection logic

- **gta6_talk**: same transcript → Gemini path with a kind-specific system
  prompt: GTA6 review/discussion highlights (breakdowns, release/spec info,
  hot takes, analysis). The prompt explicitly **rejects moments where
  trailer footage/audio or music is playing** (belt), and the validator adds
  music-presence heuristics on the slice (braces: sustained non-speech
  energy) as a second layer — reaction videos that slip past title filters
  still can't ship licensed audio. Validator unchanged otherwise (duration
  fit, gaps, ≥80 words, banned words). `upload.CATEGORY` map gains a
  fallback for NULL/unknown category (currently `KeyError`), and
  `_tags`/`_hashtags` handle the gameplay kind's missing category.
- **gta6_gameplay**: no transcript, no Whisper. New segment selector:
  (a) audio-energy peak detection (ffmpeg `astats`/`silencedetect`) picks
  20–45 s high-action windows; (b) scene-cut frequency
  (`select='gt(scene,0.4)'`) rejects static stretches. No Gemini in v1;
  a vision/frame-rating pass is a stretch goal.

### 7. Quality fixes (apply globally, all kinds)

Confirmed defects and fixes, priority order:

1. **Subway Surfers blur-bar crop defect**: tighten `gameplay_filter`
   (portrait-first + HUD-region heuristic on sampled frames), clear and
   re-fetch the 6-item library (`fetch_gameplay` only fills deficits — the
   index must be purged first), and add frame-sample validation at fetch
   time. QA gate: manual frame inspection of rendered output.
2. **Caption seam collision**: captions anchored inside the source panel
   (top-third-safe over the speaker), not on the seam — fixes
   `sub_y = top_h - 40` in `subtitles.py` for both layouts.
3. **Heading box / credit collision**: heading box shrinks with padding;
   credit moves to first 1.5 s, bottom-left of source panel, smaller.
4. **Wide-shot tiny-speakers**: per-source `crop` values added for the
   existing podcast allowlist during rollout; face-detection auto-crop is an
   optional stretch (new dependency — builder's call if trivial).
5. **Title hooks**: Gemini title prompt updated — titles sell a payoff or
   curiosity gap, not a description. Still ≤100 chars, still `#shorts`.
6. **Niche branding mismatch** (`The Riddle God` posting podcast clips): out
   of code scope, flagged for the owner (general-entertainment positioning vs
   separate GTA6 channel).

### 8. Data model changes

- `clips`: add `kind TEXT NOT NULL DEFAULT 'podcast'`; all queries filter by
  kind where day-scoped.
- `runs` / `steps`: PK rebuild to `(day, kind)` / `(day, step, kind)` via
  migration helper (create-new → copy → drop → rename), versioned schema for
  fresh DBs.
- `gameplay_usage`: extended to track official-trailer segments too.
- No destructive data loss; existing day rows migrate to `kind='podcast'`.

### 9. Failure handling

- Per-kind independence: `SourceRejected` in gta6_talk drops videos only for
  that kind (≤3 tries as today). A kind that exhausts retries skips
  gracefully; the day still completes for other kinds.
- Upload failure mid-kind behaves as today (per-clip; `UploadLimit` stops the
  day's remaining uploads across all kinds).
- ActionNeeded unchanged.

### 10. Definition of done

- `autoyt daemon` renders and schedules ≥7 podcast + 3–6 GTA6 videos/day in
  dry_run, then scheduled, without manual steps.
- Batches run at randomized, separated times; system load stays spread
  (verify: no tick runs more than one batch; overdue batches drain one per
  tick after a late boot).
- Rendered sample for each kind passes manual frame inspection (no blur bars,
  no seam captions, no heading collision).
- Daily summary email reports counts per kind.
- All existing tests pass; new tests cover: kind quota split + roll
  persistence, batch plan draw/persistence/drain-overdue, gta6 segment
  selection, gameplay filter rejection, full template render (1 frame),
  (day, kind) run resumability, migration of an existing DB, leak-signal
  rejection, music-presence rejection (validator heuristic), per-kind QC
  bounds.

## Left to the builder

- Exact prompts (kind system prompt, title prompt wording, heading pool).
- Face-crop: v1 center-crop; OpenCV face detection optional stretch.
- Curating the concrete gta6_talk channel allowlist (verify at go-live:
  English, duration, clip density, no reaction-format channels) **and** the
  pre-launch gta6_gameplay GTA V/Online source group (needed immediately);
  post-launch GTA VI group seeded on/after 19 Nov.
- Exact music-presence thresholds for the validator's audio heuristic.
