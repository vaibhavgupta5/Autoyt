"""One day's run: pick, audio, transcribe, select, render, upload, clean up. Fully resumable."""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import assets, captions, monitor, qc, render, subtitles, transcribe, upload, ytdlp
from .config import Settings
from .db import Database
from .errors import ActionNeeded, SourceRejected, StepFailed, UploadLimit
from .logutil import recent_log
from .notify import notify_once
from .select_clips import collect_candidates
from .sentences import build_sentences
from .sources import load_sources, pick_video
from .validate import strip_links, validate_and_pick

log = logging.getLogger("autoyt.pipeline")


def today(s: Settings) -> str:
    return datetime.now(ZoneInfo(s.tz)).strftime("%Y-%m-%d")


def clips_target(s: Settings, db: Database) -> int:
    """Clips per day, ramping up 3 -> 5 -> 8 after the first real upload."""
    n = s.clips_per_day
    if not s.ramp_up:
        return n
    first = db.kv_get("first_upload_day")
    age = 0
    if first:
        age = (datetime.now(ZoneInfo(s.tz)).date() - date.fromisoformat(first)).days
    step = s.cfg["ramp"][0][1]
    for days, count in s.cfg["ramp"]:
        if age >= days:
            step = count
    return max(1, min(n, step))


def slot_gap_minutes(s: Settings, n: int) -> int:
    sl = s.cfg["slots"]
    gap = int(sl["target_span_hours"] * 60 / max(n - 1, 1))
    return max(sl["min_gap"], min(sl["max_gap"], gap))


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
            except (ActionNeeded, SourceRejected, UploadLimit):
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed (try %d/%d): %s", label, attempt + 1, len(delays) + 1, e)
                if attempt == len(delays):
                    raise StepFailed(f"{label}: {e}") from e
                self.sleep(delays[attempt])

    def next_slot(self, n: int) -> str:
        sl = self.s.cfg["slots"]
        gap = slot_gap_minutes(self.s, n) * 60
        last = float(self.db.kv_get("last_slot", "0") or 0)
        base = max(time.time() + sl["first_delay_minutes"] * 60, last + gap)
        t = base + self.rng.uniform(0, sl["jitter_minutes"] * 60)
        self.db.kv_set("last_slot", str(t))
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _source_cfg(self, name: str) -> dict:
        for src in load_sources(self.s):
            if src["name"] == name:
                return src
        return {"layout": "center_crop", "credit": name}

    # ---------------------------------------------------------- one attempt
    def attempt(self, day: str) -> str:
        """One try at the day's run. Returns done | failed | retry."""
        s, db = self.s, self.db
        run = db.ensure_run(day)
        attempts = run["attempts"] + 1
        db.update_run(day, status="running", attempts=attempts, started_at=time.time(), error=None, next_try_at=None)
        max_attempts = s.cfg["retry"]["run_attempts_per_day"]
        try:
            self.execute(day)
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
        self._summary_email(day)
        return "done"

    def execute(self, day: str) -> None:
        try:
            self.maintenance(day)
            if self.s.mode != "dry_run":
                self._monitor(day)
            for _ in range(3):
                try:
                    self._run_video(day)
                    return
                except SourceRejected as e:
                    log.warning("dropping this video: %s", e)
                    self._drop_video(day, str(e))
            raise StepFailed("three videos in a row were unusable")
        finally:
            transcribe.release_model()  # free the Whisper model's memory between runs

    # ------------------------------------------------------------- stages
    def _run_video(self, day: str) -> None:
        s, db = self.s, self.db
        work = s.work_dir / day
        work.mkdir(parents=True, exist_ok=True)
        run = db.get_run(day)
        if not run["video_id"]:
            picked = pick_video(s, db, self.rng)
            db.mark_used(picked.video_id, picked.source["name"])
            db.kv_set(f"dur:{day}", str(picked.duration))
            db.update_run(day, source=picked.source["name"], video_id=picked.video_id, video_title=picked.title,
                          video_url=picked.url, credit=picked.source.get("credit", picked.source["name"]))
            db.mark_step(day, "pick")
            run = db.get_run(day)
            log.info("picked %s: %s (%s)", run["source"], run["video_title"], run["video_id"])

        tr = self._get_transcript(day, run, work)

        if not db.step_done(day, "select"):
            self._select(day, run, tr)

        self._clips(day, run, tr)
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

    def _select(self, day: str, run, tr: dict) -> None:
        s, db = self.s, self.db
        words = tr["words"]
        sentences = build_sentences(words, punctuated=tr.get("source") != "captions")
        n = clips_target(s, db)
        cfg, banned = s.cfg["clip"], s.cfg["banned_words"]
        cands = collect_candidates(s, sentences, run["video_title"], run["credit"])
        chosen = validate_and_pick(sentences, words, cands, n, cfg, banned)
        if len(chosen) < n:
            log.info("only %d of %d clips; asking for more candidates", len(chosen), n)
            more = collect_candidates(s, sentences, run["video_title"], run["credit"],
                                      per_window=cfg["candidates_per_window"] + 3)
            chosen = validate_and_pick(sentences, words, cands + more, n, cfg, banned)
        if len(chosen) < s.cfg["min_clips_per_video"]:
            raise SourceRejected(f"only {len(chosen)} usable clips in this video")
        db.add_clips(day, chosen)
        db.mark_step(day, "select")
        log.info("selected %d clips (target %d)", len(chosen), n)

    def _fetch_section(self, run, r, section: Path) -> Path:
        """Runs in a worker thread: download one clip's slice of the source video (network-bound)."""
        return self.retry(lambda: ytdlp.download_section(self.s, run["video_id"], r["start"], r["end"], section),
                          f"download clip {r['idx']}")

    def _clips(self, day: str, run, tr: dict) -> None:
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
        pool = ThreadPoolExecutor(max_workers=3)
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
                    self._process_clip(day, r, run, tr, total=len(rows), prefetch=prefetch)
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
                except (ActionNeeded, SourceRejected):
                    raise
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    log.error("clip %d failed: %s", r["idx"], e)
                    db.update_clip(r["id"], status="failed", error=str(e)[:300])
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if failures and not limit_hit:
            raise StepFailed(f"{failures} clip(s) failed")

    def _process_clip(self, day: str, r, run, tr: dict, total: int, prefetch: dict | None = None) -> None:
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
            rel = None
            if tr.get("source") == "captions":
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
            ass = subtitles.build_ass(rel, r["heading"], run["credit"], r["duration"], lay)
            game = assets.choose_gameplay(s, db, r["duration"], self.rng)
            music = assets.choose_music(s, r["duration"], self.rng)
            src = self._source_cfg(run["source"])
            self.retry(lambda: render.render_clip(
                s, section=section, ass_text=ass, out_path=final, duration=r["duration"], gameplay=game,
                music=music, layout_name=src.get("layout", "center_crop"), crop=src.get("crop"),
                zoom=float(src.get("zoom", 1.0))), f"render clip {r['idx']}")
            problems = qc.check_clip(final, s.cfg["clip"], lay)
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
            tags=self._tags(r, run), category=r["category"], publish_at=publish_at,
            privacy="public" if s.mode == "public" else "private"), f"upload clip {r['idx']}")
        thumb_ok = False
        try:
            render.first_frame(final, thumb)
            thumb_ok = upload.set_thumbnail(self._svc(), vid, thumb)
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
    def _title(r) -> str:
        t = r["title"].strip()
        suffix = " #shorts"
        return (t[:100 - len(suffix)] + suffix) if len(t) + len(suffix) > 100 else t + suffix

    @staticmethod
    def _clip_tags(r) -> list[str]:
        try:
            return list(json.loads(r["tags"] or "[]")) if "tags" in r.keys() else []
        except (ValueError, TypeError):
            return []

    @classmethod
    def _tags(cls, r, run) -> list[str]:
        """YouTube search tags (not visible on the page): the AI's topic keywords, then the basics."""
        credit = re.sub(r"\(.*?\)", "", run["credit"] or "").strip().lower()
        base = [*cls._clip_tags(r), "shorts", r["category"], credit,
                "podcast clips" if "podcast" in credit else "stream clips"]
        out: list[str] = []
        used = 0
        for t in base:
            t = strip_links(t).lower().lstrip("#").strip()
            if not t or t in out or used + len(t) + 3 > 450:  # YouTube allows about 500 characters in total
                continue
            out.append(t)
            used += len(t) + 3
            if len(out) >= 15:
                break
        return out

    @classmethod
    def _hashtags(cls, r, run) -> str:
        """Up to 5 visible hashtags. The first three show above the video title."""
        credit = re.sub(r"\(.*?\)", "", run["credit"] or "")
        names = ["shorts", r["category"], credit, *cls._clip_tags(r)]
        tags: list[str] = []
        for n in names:
            h = re.sub(r"[^a-z0-9]", "", n.lower())
            if 2 <= len(h) <= 30 and h not in tags:
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
        ok = sum(1 for c in clips if c["status"] in ("uploaded", "rendered"))
        notify_once(s, db, day, "summary", f"Daily summary {day}: {ok}/{len(clips)} clips", "\n".join(lines))
