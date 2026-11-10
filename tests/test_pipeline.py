"""End-to-end with fakes for YouTube, Gemini and Whisper; real ffmpeg, render, QC and cleanup."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from autoyt import pipeline, transcribe, upload, ytdlp
from autoyt.config import load_settings
from autoyt.db import Database
from autoyt.errors import SourceRejected, UploadLimit
from autoyt.pipeline import Runner, today
from autoyt.sources import Picked

from conftest import make_words

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed")


def _fake_section(s, video_id, start, end, out_file):
    dur = end - start
    out_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d={dur:.2f}",
                    "-f", "lavfi", "-i", f"sine=frequency=300:d={dur:.2f}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(out_file)], check=True)
    return out_file


def _cands():
    spans = [(5, 14, "funny"), (40, 49, "informative"), (80, 89, "deep"), (120, 129, "funny")]
    return [{"start_sentence": a, "end_sentence": b, "heading": f"Clip number {a}", "title": f"Title {a}",
             "description": "d see https://spam.example.com now", "context_line": "ctx",
             "tags": ["Jake Paul", "boxing", "#fight", "http://bad.example.com", "x"],
             "category": c, "hook_score": 9, "standalone_score": 8}
            for a, b, c in spans]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOYT_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOYT_MODE", "scheduled")
    s = load_settings()
    s.ensure_dirs()
    s.cfg["layout"]["preset"] = "ultrafast"
    s.cfg["retry"]["step_delays"] = [0, 0, 0]
    db = Database(s.db_path)
    calls = {"uploads": [], "thumbs": [], "emails": [], "picks": 0}

    def fake_pick(s_, db_, rng=None):
        calls["picks"] += 1
        return Picked({"name": "Test Show", "credit": "Test Show"}, f"vid{calls['picks']}", "A long episode",
                      3 * 3600.0, "https://www.youtube.com/watch?v=vid")

    def fake_run_tr(s_, audio, work, total_seconds=None):
        tr = {"language": "en", "duration": 1500, "words": make_words(130)}
        (work / "transcript.json").write_text(json.dumps(tr))
        return tr

    def fake_upload(service, path, **kw):
        assert Path(path).exists()
        calls["uploads"].append(kw)
        return f"yt{len(calls['uploads'])}"

    monkeypatch.setattr(ytdlp, "download_captions", lambda *a, **k: None)  # default: no captions -> Whisper path
    monkeypatch.setattr(pipeline, "pick_video", fake_pick)
    def fake_audio(s_, vid, d):
        p = d / "audio.m4a"
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr(ytdlp, "download_audio", fake_audio)
    monkeypatch.setattr(transcribe, "split_audio", lambda a, d: [])
    monkeypatch.setattr(transcribe, "run_transcription", fake_run_tr)
    monkeypatch.setattr(ytdlp, "download_section", _fake_section)
    monkeypatch.setattr(pipeline, "collect_candidates", lambda *a, **k: _cands())
    monkeypatch.setattr(upload, "upload_video", fake_upload)
    monkeypatch.setattr(upload, "set_thumbnail", lambda svc, vid, jpg: calls["thumbs"].append(vid) or True)
    monkeypatch.setattr(pipeline, "notify_once", lambda s_, db_, day, kind, subj, body: calls["emails"].append((kind, subj)))
    runner = Runner(s, db, sleep=lambda x: None, service_factory=lambda s_: object())
    return s, db, runner, calls


def test_full_scheduled_day_uploads_and_deletes_local_clips(env):
    s, db, runner, calls = env
    day = today(s)
    assert runner.attempt(day) == "done"
    clips = db.clips_for(day)
    assert len(clips) == 3 and all(c["status"] == "uploaded" for c in clips)
    assert [c["youtube_id"] for c in clips] == ["yt1", "yt2", "yt3"]
    assert calls["thumbs"] == ["yt1", "yt2", "yt3"]
    # scheduled, in order, in the future
    pubs = [u["publish_at"] for u in calls["uploads"]]
    assert pubs == sorted(pubs) and all(pubs)
    assert calls["uploads"][0]["title"].endswith("#shorts")
    desc = calls["uploads"][0]["description"]
    assert "http" not in desc and "youtu" not in desc and "spam.example.com" not in desc, desc  # no links at all
    assert "#shorts" in desc and "Clip from Test Show" in desc
    tags = calls["uploads"][0]["tags"]
    assert "jake paul" in tags and "boxing" in tags and "fight" in tags and "shorts" in tags
    assert not any("http" in t or "." in t for t in tags) and len(tags) == len(set(tags))
    # local clips and working files are gone after upload
    out = s.out_dir / day
    assert not out.exists() or not list(out.glob("*.mp4")) and not list(out.glob("*.jpg"))
    assert not (s.work_dir / day).exists()
    assert all(c["file"] is None for c in clips)
    assert db.kv_get("first_upload_day")
    assert ("summary" in [k for k, _ in calls["emails"]])
    assert db.get_run(day)["status"] == "done"


def test_transcript_is_cached_per_video_and_reused(env, monkeypatch):
    s, db, runner, calls = env
    counts = {"audio": 0, "transcribe": 0}
    real_audio, real_tr = ytdlp.download_audio, transcribe.run_transcription

    def counting_audio(*a, **k):
        counts["audio"] += 1
        return real_audio(*a, **k)

    def counting_tr(*a, **k):
        counts["transcribe"] += 1
        return real_tr(*a, **k)

    monkeypatch.setattr(ytdlp, "download_audio", counting_audio)
    monkeypatch.setattr(transcribe, "run_transcription", counting_tr)
    s.mode = "dry_run"
    day = today(s)
    assert runner.attempt(day) == "done"
    assert (s.data_dir / "transcripts" / "vid1.json").exists()
    assert counts == {"audio": 1, "transcribe": 1}
    # same video again (e.g. new prompt): steps reset, work dir gone, but the cache must be used
    db.clear_steps(day)
    shutil.rmtree(s.work_dir / day, ignore_errors=True)
    assert runner.attempt(day) == "done"
    assert counts == {"audio": 1, "transcribe": 1}, "transcription must not repeat for a cached video"


def test_public_mode_publishes_immediately_and_scheduled_mode_holds_until_slot(env):
    s, db, runner, calls = env
    s.mode = "public"
    day = today(s)
    assert runner.attempt(day) == "done"
    assert len(calls["uploads"]) == 3
    assert all(u["privacy"] == "public" and u["publish_at"] is None for u in calls["uploads"])
    db.archive_day(day)
    calls["uploads"].clear()
    s.mode = "scheduled"
    assert runner.attempt(day) == "done"
    assert len(calls["uploads"]) == 3 and all(u["publish_at"] for u in calls["uploads"])  # public at their times


def test_captions_path_skips_full_whisper_and_uses_whisper_only_on_clip_slices(env, monkeypatch):
    s, db, runner, calls = env
    ws = make_words(130)  # 1820 words over about 700 seconds
    events = {"events": [{"tStartMs": int(w["s"] * 1000), "dDurationMs": 300,
                          "segs": [{"utf8": w["w"], "tOffsetMs": 0}]} for w in ws]}

    def fake_captions(s_, vid, d):  # like the real one, writes a fresh file each call
        d.mkdir(parents=True, exist_ok=True)
        p = d / "caps.en-orig.json3"
        p.write_text(json.dumps(events))
        return p

    monkeypatch.setattr(ytdlp, "download_captions", fake_captions)
    monkeypatch.setattr(pipeline, "pick_video", lambda s_, db_, rng=None: Picked(
        {"name": "Test Show", "credit": "Test Show"}, "vid1", "A short episode", 700.0, "https://www.youtube.com/watch?v=vid"))

    def boom(*a, **k):
        raise AssertionError("must not run for a video that has captions")

    monkeypatch.setattr(transcribe, "run_transcription", boom)
    monkeypatch.setattr(ytdlp, "download_audio", boom)
    slices = []

    def fake_section_asr(s_, media, work):
        slices.append(media.name)
        return [{"w": f"w{i}", "s": i * 0.4, "e": i * 0.4 + 0.3} for i in range(130)]  # relative to slice start

    monkeypatch.setattr(transcribe, "transcribe_section", fake_section_asr)
    spans = [(3, 12), (30, 39), (60, 69), (90, 99)]  # sentences are 14 words, so 10 of them is about 54 seconds
    monkeypatch.setattr(pipeline, "collect_candidates", lambda *a, **k: [
        {"start_sentence": a_, "end_sentence": b_, "heading": f"Caption clip {a_}", "title": f"T{a_}", "description": "d",
         "context_line": "c", "tags": ["topic"], "category": c_, "hook_score": 9, "standalone_score": 8}
        for (a_, b_), c_ in zip(spans, ["funny", "informative", "deep", "funny"])])
    s.mode = "dry_run"
    day = today(s)
    assert runner.attempt(day) == "done"
    assert len(db.clips_for(day)) == 3 and len(slices) == 3, "Whisper runs once per chosen clip, not on the whole video"
    cached = json.loads((s.data_dir / "transcripts" / "vid1.json").read_text())
    assert cached["source"] == "captions" and len(cached["words"]) == 1820


def test_forced_rerun_after_finish_restores_transcript_and_never_reuploads(env):
    s, db, runner, calls = env
    day = today(s)
    assert runner.attempt(day) == "done"
    assert not (s.work_dir / day).exists()          # the finish step removed the work folder
    uploads = len(calls["uploads"])
    assert runner.attempt(day) == "done"            # steps are still marked done; used to crash on a missing file
    assert len(calls["uploads"]) == uploads, "already-uploaded clips must not be uploaded twice"


def test_missing_transcript_without_cache_redoes_only_audio_and_transcription(env):
    s, db, runner, calls = env
    day = today(s)
    assert runner.attempt(day) == "done"
    for f in (s.data_dir / "transcripts").glob("*.json"):
        f.unlink()
    uploads = len(calls["uploads"])
    assert runner.attempt(day) == "done"
    assert len(calls["uploads"]) == uploads         # clips stay as they were; nothing re-planned


def test_dry_run_keeps_clips_and_uploads_nothing(env, monkeypatch):
    s, db, runner, calls = env
    s.mode = "dry_run"
    day = today(s)
    assert runner.attempt(day) == "done"
    assert calls["uploads"] == []
    files = sorted((s.out_dir / day).glob("clip_*.mp4"))
    assert len(files) == 3
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                            str(files[0])], capture_output=True, text=True).stdout
    assert "1080,1920" in probe


def test_resume_after_crash_only_redoes_unfinished(env, monkeypatch):
    s, db, runner, calls = env
    day = today(s)
    real = upload.upload_video
    n = {"i": 0}

    def flaky(service, path, **kw):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("network down")
        return real(service, path, **kw)

    monkeypatch.setattr(upload, "upload_video", flaky)
    # every retry of clip 1 fails too, so the run needs another attempt
    monkeypatch.setattr(pipeline.Runner, "retry", lambda self, fn, label: fn())
    assert runner.attempt(day) == "retry"
    statuses = [c["status"] for c in db.clips_for(day)]
    assert statuses.count("uploaded") == 2 and statuses.count("failed") == 1
    assert db.get_run(day)["status"] == "pending" and db.get_run(day)["next_try_at"]
    picks_before = calls["picks"]
    assert runner.attempt(day) == "done"
    assert calls["picks"] == picks_before, "must resume the same video, not pick a new one"
    assert all(c["status"] == "uploaded" for c in db.clips_for(day))
    assert len(calls["uploads"]) == 3  # no clip uploaded twice


def test_upload_limit_stops_and_emails(env, monkeypatch):
    s, db, runner, calls = env

    def limited(service, path, **kw):
        raise UploadLimit("limit")

    monkeypatch.setattr(upload, "upload_video", limited)
    day = today(s)
    assert runner.attempt(day) == "done"
    assert "limit" in [k for k, _ in calls["emails"]]
    assert all(c["status"] == "failed" for c in db.clips_for(day))


def test_unusable_video_is_replaced(env, monkeypatch):
    s, db, runner, calls = env
    state = {"n": 0}

    def cands(*a, **k):
        state["n"] += 1
        return [] if state["n"] <= 2 else _cands()  # first video yields nothing (both passes)

    monkeypatch.setattr(pipeline, "collect_candidates", cands)
    day = today(s)
    assert runner.attempt(day) == "done"
    assert calls["picks"] == 2
    assert db.one("SELECT 1 FROM bad_videos WHERE video_id='vid1'")


def test_action_needed_emails_once_and_stops(env, monkeypatch):
    from autoyt.errors import ActionNeeded

    s, db, runner, calls = env

    def boom(*a, **k):
        raise ActionNeeded("Gemini key bad")

    monkeypatch.setattr(pipeline, "collect_candidates", boom)
    day = today(s)
    assert runner.attempt(day) == "failed"
    assert [k for k, _ in calls["emails"]] == ["action"]
    assert db.get_run(day)["status"] == "failed"
