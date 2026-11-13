"""One batch run: pick, audio, transcribe, select, render, upload, clean up. Fully resumable.

A run key is ``<day>:<kind>[#n]``. Podcast and GTA-talk batches go through the transcript -> Gemini path;
GTA-gameplay batches pick loud action moments from the audio and need no transcript at all.
"""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import assets, captions, monitor, qc, render, segments, slots, stats, subtitles, transcribe, upload, ytdlp
from .config import Settings
from .db import Database
from .errors import ActionNeeded, BotBlocked, SourceRejected, StepFailed, UploadLimit
from .kinds import (KIND_LABEL, batch_size, clip_cfg, clips_target, day_of, day_total, kind_of,  # noqa: F401
                    today)  # clips_target and today are re-exported for the CLI
from .logutil import recent_log
from .notify import notify_once
from .select_clips import collect_candidates
from .sentences import build_sentences
from .sources import load_sources, pick_video
from .validate import music_ratio, strip_links, validate_and_pick

GTA = ("gta6_talk", "gta6_gameplay")


class KindSkipped(Exception):
    """This kind has nothing usable today (no sources, all exhausted). The batch is skipped, not failed."""


log = logging.getLogger("autoyt.pipeline")


class Runner:
    def __init__(self, s: Settings, db: Database, sleep=time.sleep, rng: random.Random | None = None,
                 service_factory=None):
        self.s, self.db = s, db
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.service_factory = service_factory or upload.get_service
        self._service = None

    # ------------------------------------------------------------ helpers
    def _svc(self):
        if self._service is None:
            self._service = self.service_factory(self.s)
        return self._service

    def retry(self, fn, label: str):
        delays = list(self.s.cfg["retry"]["step_delays"])
        for attempt in range(len(delays) + 1):
            try:
                return fn()
            except (ActionNeeded, SourceRejected, UploadLimit, BotBlocked):
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed (try %d/%d): %s", label, attempt + 1, len(delays) + 1, e)
                if attempt == len(delays):
                    raise StepFailed(f"{label}: {e}") from e
                self.sleep(delays[attempt])

    def next_slot(self, n: int) -> str:
        """Next publish time inside the audience's window, after every video already queued."""
        return slots.next_slot(self.s, self.db, n, self.rng)

    def _source_cfg(self, name: str) -> dict:
        for src in load_sources(self.s):
            if src["name"] == name:
                return src
        return {"layout": "center_crop", "credit": name}

    # ---------------------------------------------------------- one attempt
    def attempt(self, day: str) -> str:
        """One try at a batch run (``day`` is the run key). Returns done | failed | retry | skipped."""
        s, db = self.s, self.db
        run = db.ensure_run(day)
        attempts = run["attempts"] + 1
        db.update_run(day, status="running", attempts=attempts, started_at=time.time(), error=None, next_try_at=None)
        max_attempts = s.cfg["retry"]["run_attempts_per_day"]
        try:
            self.execute(day)
        except BotBlocked as e:
            # YouTube is blocking this IP. Not this batch's fault: no attempt is used up, and every batch
            # waits until the back-off ends (the scheduler checks bot_block_until).
            until = self._bot_backoff(day, str(e))
            db.update_run(day, status="pending", attempts=attempts - 1, error=f"bot check: {e}"[:500],
                          next_try_at=until)
            return "retry"
        except KindSkipped as e:
            log.warning("%s skipped: %s", day, e)
            db.update_run(day, status="skipped", error=str(e)[:500])
            notify_once(s, db, day, "skipped", f"{KIND_LABEL.get(kind_of(day), day)} batch skipped today",
                        f"{day}: {e}\n\nThe other batches run as planned. Add or re-enable sources of this kind "
                        "in config/sources.yaml if this keeps happening.")
            return "skipped"
        except ActionNeeded as e:
            log.error("action needed: %s", e)
            db.update_run(day, status="failed", error=str(e)[:500])
            notify_once(s, db, day, "action", "Action needed",
                        f"{e}\n\nThe run stops for today. After fixing it, run:\n"
                        "docker compose exec autoyt autoyt run --force\n\nRecent log:\n" + recent_log())
            return "failed"
        except Exception as e:  # noqa: BLE001
            log.exception("run attempt %d/%d failed", attempts, max_attempts)
            if attempts >= max_attempts:
                db.update_run(day, status="failed", error=str(e)[:500])
                notify_once(s, db, day, "failed", f"Run failed after {attempts} attempts",
                            f"Today's run ({day}) failed: {e}\n\nRecent log:\n{recent_log(40)}")
                return "failed"
            nxt = time.time() + s.cfg["retry"]["run_retry_minutes"] * 60
            db.update_run(day, status="pending", error=str(e)[:500], next_try_at=nxt)
            return "retry"
        db.update_run(day, status="done", error=None)
        db.execute("DELETE FROM kv WHERE key IN ('bot_block_until', 'bot_block_since', 'bot_block_streak')")
        self._summary_email(day)
        return "done"

    def _bot_backoff(self, key: str, why: str) -> float:
        s, db = self.s, self.db
        now = time.time()
        steps = [float(h) for h in s.cfg["bot_check"]["backoff_hours"]]
        streak = int(db.kv_get("bot_block_streak", "0") or 0)
        until = now + steps[min(streak, len(steps) - 1)] * 3600
        since = float(db.kv_get("bot_block_since", "0") or 0) or now
        db.kv_set("bot_block_streak", str(streak + 1))
        db.kv_set("bot_block_since", str(since))
        db.kv_set("bot_block_until", str(until))
        log.warning("YouTube bot check (%s); pausing all YouTube work for %.0f h", why[:120], (until - now) / 3600)
        hours = (now - since) / 3600
        if hours >= float(s.cfg["bot_check"]["alert_after_hours"]):
            notify_once(s, db, day_of(key), "botblock", "YouTube is blocking downloads",
                        f"Every YouTube request has hit 'confirm you're not a bot' for {hours:.0f} hours. The "
                        "pipeline keeps backing off and retries by itself. If it does not clear within a day:\n"
                        "  1. docker compose logs bgutil-provider   (the PO-token server must be running)\n"
                        "  2. docker compose build --pull && docker compose up -d   (newer yt-dlp)\n\n"
                        f"Last error: {why[:400]}")
        return until

    def execute(self, day: str) -> None:
        kind = kind_of(day)
        try:
            self.maintenance(day)
            if self.s.mode != "dry_run":
                self._monitor(day)
            last = ""
            for _ in range(3):
                try:
                    if kind == "gta6_gameplay":
                        self._run_gameplay(day)
                    else:
                        self._run_video(day, kind)
                    return
                except SourceRejected as e:
                    log.warning("dropping this video: %s", e)
                    last = str(e)
                    self._drop_video(day, str(e))
            if kind != "podcast":
                raise KindSkipped(f"no usable {KIND_LABEL.get(kind, kind)} video found ({last})")
            raise StepFailed("three videos in a row were unusable")
        finally:
            transcribe.release_model()  # free the Whisper model's memory between runs

    # ------------------------------------------------------------- stages
    def _pick(self, day: str, kind: str):
        s, db = self.s, self.db
        run = db.get_run(day)
        if not run["video_id"]:
            picked = pick_video(s, db, self.rng, kind=kind)
            db.mark_used(picked.video_id, picked.source["name"])
            db.kv_set(f"dur:{day}", str(picked.duration))
            db.update_run(day, source=picked.source["name"], video_id=picked.video_id, video_title=picked.title,
                          video_url=picked.url, credit=picked.source.get("credit", picked.source["name"]))
            db.mark_step(day, "pick")
            run = db.get_run(day)
            log.info("picked %s: %s (%s)", run["source"], run["video_title"], run["video_id"])
        return run

    def _run_video(self, day: str, kind: str = "podcast") -> None:
        s, db = self.s, self.db
        work = s.work_dir / day
        work.mkdir(parents=True, exist_ok=True)
        run = self._pick(day, kind)
        tr = self._get_transcript(day, run, work)

        if not db.step_done(day, "select"):
            self._select(day, run, tr, kind)

        self._clips(day, run, tr, kind)
        self._finalize(day)

    def _run_gameplay(self, day: str) -> None:
        """GTA gameplay: loud action windows from the audio, no transcript, no captions, original sound."""
        s, db = self.s, self.db
        kind = "gta6_gameplay"
        run = self._pick(day, kind)
        if not db.step_done(day, "select"):
            n = batch_size(s, db, day)
            cfg = clip_cfg(s, kind)
            dur = float(db.kv_get(f"dur:{day}", "0") or 0)
            wins = self.retry(lambda: segments.select_segments(s, db, run["video_id"], dur, n, cfg, self.rng),
                              "find gameplay moments")
            if not wins:
                raise SourceRejected("no lively gameplay moments found")
            game = str(self._source_cfg(run["source"]).get("game_label", "GTA 5"))
            rows = []
            for w in wins:
                meta = segments.gameplay_meta(game, self.rng)
                rows.append({**w, **meta, "score": w.get("score", 0), "text": ""})
            db.add_clips(day, rows, kind=kind)
            db.mark_step(day, "select")
            log.info("selected %d gameplay moments (target %d)", len(rows), n)
        self._clips(day, run, {"words": [], "source": "none"}, kind)
        self._finalize(day)

    def _get_transcript(self, day: str, run, work: Path) -> dict:
        """Cache, else YouTube's captions (seconds), else full Whisper (10-30 minutes)."""
        s, db = self.s, self.db
        # A transcript is cached per video, so retrying a video (new prompt, dropped for too few clips,
        # crash) never repeats any of this.
        cache = s.data_dir / "transcripts" / f"{run['video_id']}.json"
        tpath = work / "transcript.json"
        if db.step_done(day, "transcribe") and not tpath.exists():
            # e.g. a forced re-run after the finish step removed the work folder
            if cache.exists():
                shutil.copy(cache, tpath)
            else:
                log.warning("transcript file missing and not cached; redoing audio and transcription")
                db.execute("DELETE FROM steps WHERE day=? AND step IN ('audio','transcribe')", (day,))
        if cache.exists() and not db.step_done(day, "transcribe"):
            shutil.copy(cache, tpath)
            db.mark_step(day, "audio")
            db.mark_step(day, "transcribe")
            log.info("reusing cached transcript for %s", run["video_id"])

        if not db.step_done(day, "transcribe"):
            total = float(db.kv_get(f"dur:{day}", "0") or 0) or None
            tr = None
            if s.transcript_source in ("auto", "captions"):
                tr = self._captions_transcript(run, work, total)
            if tr is not None:
                tpath.write_text(json.dumps(tr))
                db.mark_step(day, "audio")  # no audio download needed on this path
            else:
                if s.transcript_source == "captions":
                    raise SourceRejected("no usable YouTube captions (TRANSCRIPT_SOURCE=captions)")
                cap = float(s.cfg["source"].get("max_whisper_minutes") or 0) * 60
                if cap and total and total > cap:  # 20+ minutes of CPU on the laptop: pick another video
                    raise SourceRejected(f"no YouTube captions and {total / 60:.0f} min is too long for Whisper")
                if not db.step_done(day, "audio"):
                    def get_audio():
                        audio = ytdlp.download_audio(s, run["video_id"], work)
                        transcribe.split_audio(audio, work)
                        audio.unlink(missing_ok=True)
                    self.retry(get_audio, "download audio")
                    db.mark_step(day, "audio")
                transcribe.run_transcription(s, None, work, total_seconds=total)  # type: ignore[arg-type]
            cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(tpath, cache)
            db.mark_step(day, "transcribe")
        return json.loads(tpath.read_text())

    def _captions_transcript(self, run, work: Path, total: float | None) -> dict | None:
        """YouTube's auto-captions with word timing. None means 'use Whisper instead'."""
        try:  # one quick try: if YouTube is unhappy the Whisper path has its own retries
            path = ytdlp.download_captions(self.s, run["video_id"], work)
        except BotBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("caption download failed (%s); using Whisper", e)
            return None
        if not path:
            log.info("no YouTube captions for this video; using Whisper")
            return None
        tr = captions.build_transcript(path)
        path.unlink(missing_ok=True)
        if len(tr["words"]) < 300:
            log.info("captions too sparse (%d words); using Whisper", len(tr["words"]))
            return None
        transcribe.check_speech_density(tr, total_seconds=total)  # raises SourceRejected: not enough speech
        log.info("using YouTube captions: %d words, no Whisper pass over the full video", len(tr["words"]))
        return tr

    def _select(self, day: str, run, tr: dict, kind: str = "podcast") -> None:
        s, db = self.s, self.db
        words = tr["words"]
        sentences = build_sentences(words, punctuated=tr.get("source") != "captions")
        n = batch_size(s, db, day)
        cfg = dict(clip_cfg(s, kind))
        cfg["source_duration"] = float(db.kv_get(f"dur:{day}", "0") or 0) or (words[-1]["e"] if words else 0)
        banned = s.cfg["banned_words"]
        hint = stats.category_hint(s, db, GTA if kind in GTA else ("podcast",))
        ask = dict(kind=kind, clip=cfg, hint=hint, today_iso=day_of(day)[:10])
        cands = collect_candidates(s, sentences, run["video_title"], run["credit"], **ask)
        chosen = validate_and_pick(sentences, words, cands, n, cfg, banned)
        if len(chosen) < n:
            log.info("only %d of %d clips; asking for more candidates", len(chosen), n)
            more = collect_candidates(s, sentences, run["video_title"], run["credit"],
                                      per_window=cfg["candidates_per_window"] + 3, **ask)
            chosen = validate_and_pick(sentences, words, cands + more, n, cfg, banned)
        if len(chosen) < min(s.cfg["min_clips_per_video"], n):
            raise SourceRejected(f"only {len(chosen)} usable clips in this video")
        db.add_clips(day, chosen, kind=kind)
        db.mark_step(day, "select")
        log.info("selected %d clips (target %d)", len(chosen), n)

    def _fetch_section(self, run, r, section: Path) -> Path:
        """Runs in a worker thread: download one clip's slice of the source video (network-bound)."""
        return self.retry(lambda: ytdlp.download_section(self.s, run["video_id"], r["start"], r["end"], section),
                          f"download clip {r['idx']}")

    def _clips(self, day: str, run, tr: dict, kind: str = "podcast") -> None:
        s, db = self.s, self.db
        for r in db.clips_for(day):
            if r["status"] == "failed":
                db.update_clip(r["id"], status="planned", error=None)
        failures, limit_hit = 0, False
        rows = db.clips_for(day)
        work, outdir = s.work_dir / day, s.out_dir / day
        work.mkdir(parents=True, exist_ok=True)

        # Downloads are network-bound and renders are CPU-bound, so fetch every slice at once (3 at a time)
        # while the clips are rendered one after another. The first render starts as soon as its slice lands.
        pool = ThreadPoolExecutor(max_workers=int(s.cfg["resources"]["prefetch_workers"]))
        prefetch = {}
        for r in rows:
            skip = r["status"] in ("uploaded", "deleted") or (outdir / f"clip_{r['idx']:02d}.mp4").exists()
            if not skip:
                prefetch[r["idx"]] = pool.submit(self._fetch_section, run, r, work / f"section_{r['idx']:02d}.mp4")
        try:
            for r in rows:
                if r["status"] in ("uploaded", "deleted") or (s.mode == "dry_run" and r["status"] == "rendered"):
                    continue
                try:
                    self._process_clip(day, r, run, tr, total=day_total(s, db, day), prefetch=prefetch,
                                       kind=kind)
                except UploadLimit as e:
                    log.error("upload limit: %s", e)
                    limit_hit = True
                    for left in db.clips_for(day):
                        if left["status"] in ("planned", "rendered"):
                            db.update_clip(left["id"], status="failed", error="upload limit reached")
                    notify_once(s, db, day, "limit", "YouTube upload limit reached",
                                f"{e}\nThe remaining clips from today were dropped. Tomorrow's run continues "
                                "normally. If this repeats, lower CLIPS_PER_DAY in .env.")
                    break
                except (ActionNeeded, SourceRejected, BotBlocked):
                    raise
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    log.error("clip %d failed: %s", r["idx"], e)
                    db.update_clip(r["id"], status="failed", error=str(e)[:300])
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if failures and not limit_hit:
            raise StepFailed(f"{failures} clip(s) failed")

    def _process_clip(self, day: str, r, run, tr: dict, total: int, prefetch: dict | None = None,
                      kind: str = "podcast") -> None:
        s, db = self.s, self.db
        lay = s.cfg["layout"]
        work = s.work_dir / day
        outdir = s.out_dir / day
        outdir.mkdir(parents=True, exist_ok=True)
        final = outdir / f"clip_{r['idx']:02d}.mp4"
        thumb = outdir / f"clip_{r['idx']:02d}.jpg"
        music_credit = ""
        if not final.exists():
            section = work / f"section_{r['idx']:02d}.mp4"
            fut = (prefetch or {}).get(r["idx"])
            if fut is not None:
                fut.result()  # raises if the download ran out of retries
            else:
                self._fetch_section(run, r, section)
            if kind == "gta6_talk":
                # Belt and braces against trailer music and reaction audio the prompt did not catch.
                ratio = music_ratio(section)
                limit = float(clip_cfg(s, kind).get("max_music_ratio", 0.35))
                if ratio > limit:
                    section.unlink(missing_ok=True)
                    db.update_clip(r["id"], status="deleted", error=f"music-like audio {ratio:.0%}")
                    log.info("clip %d dropped: %.0f%% of it sounds like music", r["idx"], ratio * 100)
                    return
            rel = None
            if kind == "gta6_gameplay":
                rel = []
            elif tr.get("source") == "captions":
                # The clip was FOUND from YouTube's captions; the words burned into the video come from
                # Whisper on just this slice, so they are accurate (and the slice starts at time 0).
                try:
                    rel = transcribe.transcribe_section(s, section, work)
                    if len(rel) < 20:
                        rel = None
                except Exception as e:  # noqa: BLE001
                    log.warning("Whisper on clip %d failed (%s); using the caption timings", r["idx"], e)
            if rel is None:
                rel = subtitles.clip_relative_words(tr["words"], r["start"], r["end"])
            full = kind in GTA
            ass = subtitles.build_ass(rel, r["heading"], run["credit"], r["duration"], lay,
                                      captions=kind != "gta6_gameplay", full=full)
            # GTA kinds: one full-screen picture, the source's own sound, no bottom gameplay, no music.
            game = None if full else assets.choose_gameplay(s, db, r["duration"], self.rng)
            music = None if full else assets.choose_music(s, r["duration"], self.rng)
            src = self._source_cfg(run["source"])
            if full:  # talk footage is maps, screenshots and text: keep most of the frame; gameplay fills it
                layout_name = src.get("layout") if src.get("layout") in ("full", "full_fit") else (
                    "full_fit" if kind == "gta6_talk" else "full")
            else:
                layout_name = src.get("layout", "center_crop")
            self.retry(lambda: render.render_clip(
                s, section=section, ass_text=ass, out_path=final, duration=r["duration"], gameplay=game,
                music=music, layout_name=layout_name, crop=src.get("crop"),
                zoom=float(src.get("zoom", 1.0))), f"render clip {r['idx']}")
            problems = qc.check_clip(final, clip_cfg(s, kind), lay)
            if problems:
                final.unlink(missing_ok=True)
                raise RuntimeError("QC failed: " + "; ".join(problems))
            music_credit = music["credit"] if music else ""
            db.update_clip(r["id"], status="rendered", file=str(final), music=music["title"] if music else None)
            if music_credit:
                db.kv_set(f"music_credit:{r['id']}", music_credit)
            section.unlink(missing_ok=True)
        else:
            music_credit = db.kv_get(f"music_credit:{r['id']}", "") or ""

        if s.mode == "dry_run":
            return
        publish_at = self.next_slot(total) if s.mode == "scheduled" else None
        vid = self.retry(lambda: upload.upload_video(
            self._svc(), final, title=self._title(r), description=self._description(r, run, music_credit),
            tags=self._tags(r, run), category="gaming" if kind in GTA else r["category"], publish_at=publish_at,
            privacy="public" if s.mode == "public" else "private"), f"upload clip {r['idx']}")
        thumb_ok = False
        # A 403 means the channel is not phone-verified; Shorts mostly ignore custom thumbnails anyway,
        # so stop asking for a week instead of failing (and logging) on every upload.
        if float(db.kv_get("thumbs_forbidden_until", "0") or 0) < time.time():
            try:
                render.first_frame(final, thumb)
                st: dict = {}
                thumb_ok = upload.set_thumbnail(self._svc(), vid, thumb, st)
                if st.get("forbidden"):
                    db.kv_set("thumbs_forbidden_until", str(time.time() + 7 * 86400))
            except Exception as e:  # noqa: BLE001
                log.warning("thumbnail step failed: %s", e)
        db.update_clip(r["id"], status="uploaded", youtube_id=vid, publish_at=publish_at, thumb_ok=int(thumb_ok))
        if not db.kv_get("first_upload_day"):
            db.kv_set("first_upload_day", datetime.now(ZoneInfo(s.tz)).date().isoformat())
        log.info("uploaded clip %d as %s (%s)", r["idx"], vid,
                 f"goes public {publish_at}" if publish_at else ("public now" if s.mode == "public" else "private"))
        if s.delete_after_upload:
            final.unlink(missing_ok=True)
            thumb.unlink(missing_ok=True)
            db.update_clip(r["id"], file=None)

    # -------------------------------------------------------- metadata
    @staticmethod
    def _get(r, key: str, default=None):
        try:
            v = r[key]
        except (KeyError, IndexError):
            return default
        return default if v is None else v

    @classmethod
    def _game(cls, r) -> str:
        """GTA 5 footage stays labelled GTA 5; everything else in the GTA feed is GTA 6."""
        if cls._get(r, "kind") == "gta6_gameplay" and "gta 5" in cls._clip_tags(r):
            return "GTA 5"
        return "GTA 6"

    @classmethod
    def _title(cls, r) -> str:
        """No #shorts in the title (it only costs characters); GTA-talk titles lead with "GTA 6"."""
        t = re.sub(r"\s*#shorts\b", "", str(r["title"] or "")).strip()
        if cls._get(r, "kind") == "gta6_talk" and not re.search(r"\bGTA ?(6|VI)\b", t[:40], re.I):
            t = "GTA 6: " + t
        return t[:100]

    @staticmethod
    def _clip_tags(r) -> list[str]:
        try:
            return list(json.loads(r["tags"] or "[]")) if "tags" in r.keys() else []
        except (ValueError, TypeError):
            return []

    @classmethod
    def _base_tags(cls, r, run) -> list[str]:
        kind = cls._get(r, "kind", "podcast")
        credit = re.sub(r"\(.*?\)", "", run["credit"] or "").strip().lower()
        if kind == "gta6_talk":
            return ["gta 6", "gta vi", "grand theft auto 6", "gta 6 news", "shorts", credit]
        if kind == "gta6_gameplay":
            return (["gta 5", "gta online", "gaming", "shorts"] if cls._game(r) == "GTA 5"
                    else ["gta 6", "gta vi", "gta 6 gameplay", "gaming", "shorts"])
        return ["shorts", cls._get(r, "category", ""), credit,
                "podcast clips" if "podcast" in credit else "stream clips"]

    @classmethod
    def _tags(cls, r, run) -> list[str]:
        """YouTube search tags (not visible on the page): the AI's topic keywords, then the basics."""
        kind = cls._get(r, "kind", "podcast")
        base = ([*cls._base_tags(r, run), *cls._clip_tags(r)] if kind in GTA
                else [*cls._clip_tags(r), *cls._base_tags(r, run)])
        out: list[str] = []
        used = 0
        for t in base:
            t = strip_links(str(t or "")).lower().lstrip("#").strip()
            if not t or t == "none" or t in out or used + len(t) + 3 > 450:  # YouTube allows ~500 characters
                continue
            out.append(t)
            used += len(t) + 3
            if len(out) >= 15:
                break
        return out

    @classmethod
    def _hashtags(cls, r, run) -> str:
        """Up to 5 visible hashtags. The first three show above the video title, so they lead with the
        search terms of each kind (#GTA6 #GTAVI for the GTA feed, #GTA5 for GTA 5 footage)."""
        kind = cls._get(r, "kind", "podcast")
        credit = re.sub(r"\(.*?\)", "", run["credit"] or "")
        topic = cls._clip_tags(r)
        if kind == "gta6_talk":
            names = ["GTA6", "GTAVI", *[x for x in topic if "gta" not in x.lower()], "shorts"]
        elif kind == "gta6_gameplay":
            names = (["GTA5", "GTAOnline", "gaming", "shorts"] if cls._game(r) == "GTA 5"
                     else ["GTA6", "GTAVI", "gaming", "shorts"])
        else:
            names = ["shorts", cls._get(r, "category", ""), credit, *topic]
        tags: list[str] = []
        for n in names:
            raw = re.sub(r"[^A-Za-z0-9]", "", str(n or ""))
            h = raw if raw in ("GTA6", "GTAVI", "GTA5", "GTAOnline") else raw.lower()
            if 2 <= len(h) <= 30 and h.lower() not in [x.lower() for x in tags] and h.lower() != "none":
                tags.append(h)
            if len(tags) >= 5:
                break
        return " ".join("#" + h for h in tags)

    @classmethod
    def _description(cls, r, run, music_credit: str) -> str:
        """Plain text with credit, no links anywhere, then hashtags."""
        parts = []
        if r["context_line"]:
            parts.append(strip_links(r["context_line"]))
        if r["description"]:
            parts.append(strip_links(r["description"]))
        parts.append(f"Clip from {strip_links(run['credit'])}: {strip_links(run['video_title'])}")
        parts.append("All credit to the original creator. If you are the owner and want this removed, "
                     "tell us and it will come down.")
        if music_credit:
            parts.append("Music: " + strip_links(music_credit))
        parts.append(cls._hashtags(r, run))
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------ housekeeping
    def _drop_video(self, day: str, reason: str) -> None:
        run = self.db.get_run(day)
        if run and run["video_id"]:
            self.db.mark_bad(run["video_id"], reason)
        self.db.clear_steps(day)
        self.db.execute("DELETE FROM clips WHERE day=?", (day,))
        self.db.update_run(day, source=None, video_id=None, video_title=None, video_url=None, credit=None)
        shutil.rmtree(self.s.work_dir / day, ignore_errors=True)

    def _finalize(self, day: str) -> None:
        shutil.rmtree(self.s.work_dir / day, ignore_errors=True)
        if self.s.mode != "dry_run" and self.s.delete_after_upload:
            d = self.s.out_dir / day
            if d.exists() and not any(d.glob("*.mp4")):
                shutil.rmtree(d, ignore_errors=True)

    def _monitor(self, day: str) -> None:
        try:
            monitor.check_uploaded(self.s, self.db, self._svc(), day)
        except ActionNeeded:
            pass  # will surface properly at upload time
        except Exception as e:  # noqa: BLE001
            log.warning("monitor check skipped: %s", e)

    def maintenance(self, day: str) -> None:
        s = self.s
        now = time.time()
        for base, days in ((s.out_dir, s.cfg["retention"]["dry_run_days"]), (s.work_dir, 1.5)):
            if not base.exists():
                continue
            for d in base.iterdir():
                if d.is_dir() and d.name != day and now - d.stat().st_mtime > days * 86400:
                    shutil.rmtree(d, ignore_errors=True)
        tdir = s.data_dir / "transcripts"
        if tdir.exists():  # cached transcripts are small; keep a month
            for f in tdir.glob("*.json"):
                if now - f.stat().st_mtime > 30 * 86400:
                    f.unlink(missing_ok=True)

    def _summary_email(self, day: str) -> None:
        s, db = self.s, self.db
        run = db.get_run(day)
        clips = db.clips_for(day)
        lines = [f"Source: {run['source']}", f"Video: {run['video_title']}", f"URL: {run['video_url']}",
                 f"Mode: {s.mode}", f"Attempts today: {run['attempts']}", ""]
        for c in clips:
            link = f"https://studio.youtube.com/video/{c['youtube_id']}/edit" if c["youtube_id"] else "-"
            lines.append(f"[{c['status']}] ({c['category']}) {c['title']}\n    heading: {c['heading']}\n"
                         f"    publishes: {c['publish_at'] or '-'}  {link}"
                         + (f"\n    error: {c['error']}" if c["error"] else ""))
        if s.mode == "dry_run":
            lines += ["", f"Dry run: clips are in {s.out_dir / day} for review (kept "
                          f"{s.cfg['retention']['dry_run_days']} days). Nothing was uploaded."]
        free = shutil.disk_usage(s.data_dir).free / 1e9
        lines += ["", f"Free disk: {free:.0f} GB"]
        try:
            lines += ["", stats.summary_block(s, db)]
        except Exception as e:  # noqa: BLE001
            log.warning("performance block skipped: %s", e)
        ok = sum(1 for c in clips if c["status"] in ("uploaded", "rendered"))
        label = KIND_LABEL.get(kind_of(day), kind_of(day))
        notify_once(s, db, day, "summary", f"Summary {day_of(day)} [{label}]: {ok}/{len(clips)} clips",
                    "\n".join(lines))
