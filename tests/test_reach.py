"""Reach addendum (R0-R6) and GTA routing: bot back-off, hooks, metadata, publish window, thumbnails,
stats feedback, growth levels, full-screen render, gameplay batches."""
import json
import random
import shutil
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from autoyt import pipeline, scheduler, segments, slots, stats, transcribe, upload, ytdlp
from autoyt.db import Database
from autoyt.errors import BotBlocked, SourceRejected
from autoyt.kinds import growth_level, shift_quota, today
from autoyt.pipeline import Runner
from autoyt.sources import Picked

from conftest import make_words
from test_pipeline import _fake_section, env  # noqa: F401  (fixture)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed")
BOT = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"


# ------------------------------------------------------------------ R0: bot check
def _cp(code, err="", out=""):
    return subprocess.CompletedProcess([], code, stdout=out, stderr=err)


def test_bot_text_raises_botblocked_and_other_errors_do_not(env_settings, monkeypatch):
    monkeypatch.setattr(ytdlp, "_run", lambda *a, **k: _cp(1, BOT))
    with pytest.raises(BotBlocked):
        ytdlp.video_info(env_settings, "abc")
    with pytest.raises(BotBlocked):
        ytdlp.download_captions(env_settings, "abc", env_settings.work_dir / "x")
    with pytest.raises(BotBlocked):
        ytdlp.download_audio(env_settings, "abc", env_settings.work_dir / "x")
    monkeypatch.setattr(ytdlp, "_run", lambda *a, **k: _cp(1, "ERROR: Private video"))
    with pytest.raises(SourceRejected):
        ytdlp.video_info(env_settings, "abc")


def test_po_token_provider_is_passed_to_every_call(env_settings):
    assert not any("bgutil" in a for a in ytdlp.base_args(env_settings))
    env_settings.pot_provider_url = "http://bgutil-provider:4416"
    args = ytdlp.base_args(env_settings)
    assert args[args.index("--extractor-args") + 1] == "youtubepot-bgutilhttp:base_url=http://bgutil-provider:4416"
    assert "--sleep-requests" in args


def test_bot_rejections_are_forgotten_on_open(tmp_path):
    db = Database(tmp_path / "s.db")
    db.mark_bad("v1", "cannot read video v1: Sign in to confirm you're not a bot")
    db.mark_bad("v2", "age restricted")
    assert Database(tmp_path / "s.db").bad_ids() == {"v2"}


def test_pick_never_blames_a_video_for_a_bot_check(env_settings, monkeypatch):
    from autoyt import sources
    (env_settings.config_dir / "sources.yaml").write_text(
        "- {name: P, url: u, permission: x}\n")
    db = Database(env_settings.db_path)
    db.replace_listing("P", [{"id": "v1", "title": "t", "duration": 5000.0}])
    db.kv_set("listing_at:P", str(time.time()))
    monkeypatch.setattr(sources, "check_video", lambda *a: (_ for _ in ()).throw(BotBlocked("bot")))
    with pytest.raises(BotBlocked):
        sources.pick_video(env_settings, db, random.Random(1))
    assert db.bad_ids() == set()


def test_bot_block_pauses_everything_without_using_attempts(env, monkeypatch):
    s, db, runner, calls = env
    s.min_free_disk_gb = 0
    monkeypatch.setattr(pipeline, "pick_video", lambda *a, **k: (_ for _ in ()).throw(BotBlocked(BOT)))
    key = today(s) + ":podcast"
    t0 = time.time()
    assert runner.attempt(key) == "retry"
    run = db.get_run(key)
    assert run["attempts"] == 0 and run["status"] == "pending"
    assert run["next_try_at"] >= t0 + 2 * 3600 - 5
    assert float(db.kv_get("bot_block_until")) >= t0 + 2 * 3600 - 5
    assert scheduler.tick(s, db, runner).startswith("blocked: youtube bot check")
    assert runner.attempt(key) == "retry"   # second block in a row backs off longer
    assert float(db.kv_get("bot_block_until")) >= t0 + 4 * 3600 - 5


# ------------------------------------------------------------------ R1: hooks
def _sentences_words():
    from autoyt.sentences import build_sentences
    words = make_words(200)
    return build_sentences(words, punctuated=True), words


def test_clip_in_intro_or_outro_is_rejected_unless_the_hook_is_great(env_settings):
    from autoyt.kinds import clip_cfg
    from autoyt.validate import build_clip
    sents, words = _sentences_words()
    cfg = dict(clip_cfg(env_settings, "podcast"), source_duration=words[-1]["e"])
    cand = {"start_sentence": 1, "end_sentence": 10, "heading": "A real hook", "title": "t",
            "category": "funny", "hook_score": 7, "standalone_score": 8}
    assert build_clip(sents, words, cand, cfg, []) is None          # starts in the first 90 s
    assert build_clip(sents, words, dict(cand, hook_score=9), cfg, [])  # unless the hook is great
    mid = dict(cand, start_sentence=60, end_sentence=70)
    late = dict(cand, start_sentence=len(sents) - 12, end_sentence=len(sents) - 2)
    assert build_clip(sents, words, mid, cfg, [])
    assert build_clip(sents, words, late, cfg, []) is None           # ends in the last 120 s


def test_loop_score_only_breaks_ties(env_settings):
    from autoyt.kinds import clip_cfg
    from autoyt.validate import build_clip
    sents, words = _sentences_words()
    cfg = clip_cfg(env_settings, "podcast")
    c = {"start_sentence": 60, "end_sentence": 70, "heading": "h ok", "title": "t", "category": "deep",
         "hook_score": 8, "standalone_score": 8}
    a = build_clip(sents, words, dict(c, loop_score=10), cfg, [])
    b = build_clip(sents, words, c, cfg, [])
    assert 0 < a["score"] - b["score"] <= 0.5


def test_prompt_word_budget_and_gta_launch_phase():
    from autoyt.select_clips import system_for
    talk = system_for("gta6_talk", {"min_seconds": 25, "max_seconds": 45}, "2026-11-12")
    assert "25-45 second" in talk and "62-114 spoken words" in talk
    assert "7 days from launch" in talk and "GTA 6" in talk and "trailer" in talk.lower()
    assert "launched 3 days ago" in system_for("gta6_talk", None, "2026-11-22")
    pod = system_for("podcast")
    assert "50-55 second" in pod and "GTA" not in pod and "loop_score" in pod


# ------------------------------------------------------------------ R2: metadata
def test_gta_talk_metadata_is_search_first():
    r = {"kind": "gta6_talk", "title": "The map is bigger than you think #shorts", "category": "informative",
         "tags": json.dumps(["gta 6 map", "leonida"]), "context_line": "", "description": "d"}
    run = {"credit": "Mrbossftw", "video_title": "GTA 6 news"}
    assert Runner._title(r) == "GTA 6: The map is bigger than you think"
    assert Runner._title(dict(r, title="GTA 6 map is huge")) == "GTA 6 map is huge"
    assert Runner._hashtags(r, run).startswith("#GTA6 #GTAVI #leonida")
    assert Runner._tags(r, run)[:2] == ["gta 6", "gta vi"]


def test_gta5_footage_is_never_labelled_gta6():
    rng = random.Random(3)
    for _ in range(30):
        m = segments.gameplay_meta("GTA 5", rng)
        assert "GTA 6" not in m["heading"] + m["title"] + m["comment_question"]
    r = {"kind": "gta6_gameplay", "title": m["title"], "category": None, "tags": json.dumps(m["tags"]),
         "context_line": "", "description": m["description"]}
    run = {"credit": "GTA Series Videos", "video_title": "GTA Online mission"}
    assert Runner._hashtags(r, run).startswith("#GTA5 #GTAOnline")
    assert "gta 6" not in Runner._tags(r, run) and "none" not in Runner._tags(r, run)
    r6 = dict(r, tags=json.dumps(segments.gameplay_meta("GTA 6", rng)["tags"]))
    assert Runner._hashtags(r6, run).startswith("#GTA6 #GTAVI")


def test_podcast_metadata_unchanged_apart_from_title():
    r = {"title": "Big moment", "category": "funny", "tags": "[]", "context_line": "", "description": ""}
    run = {"credit": "Some Show", "video_title": "x"}
    assert Runner._title(r) == "Big moment"
    assert Runner._hashtags(r, run).startswith("#shorts #funny #someshow")


# ------------------------------------------------------------------ R3: publish window
def test_publish_slots_land_in_the_audience_window_spaced_and_ordered(env_settings):
    db = Database(env_settings.db_path)
    tz = ZoneInfo(env_settings.cfg["slots"]["audience_tz"])
    env_settings.cfg["slots"]["window_overrides"] = {}
    got = [slots.next_slot(env_settings, db, 13, random.Random(i)) for i in range(13)]
    ts = [datetime.strptime(g, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc) for g in got]
    assert ts == sorted(ts)
    for a, b in zip(ts, ts[1:]):
        assert (b - a).total_seconds() >= 30 * 60 - 1
    for t in ts:
        local = t.astimezone(tz)
        assert 11 <= local.hour < 23 or (local.hour == 23 and local.minute == 0)


def test_window_override_moves_launch_night(env_settings):
    tz = ZoneInfo("America/New_York")
    env_settings.cfg["slots"]["window_overrides"] = {"2026-11-19": ["00:05", "23:30"]}
    t = datetime(2026, 11, 18, 23, 40, tzinfo=tz).timestamp()
    got = slots.fit(env_settings, t, t, random.Random(1))
    local = datetime.fromtimestamp(got, tz)
    assert local.date().isoformat() == "2026-11-19" and local.hour == 0


# ------------------------------------------------------------------ R4: thumbnails
def test_thumbnail_403_is_reported_as_forbidden(tmp_path):
    import httplib2
    from googleapiclient.errors import HttpError

    class Svc:
        def thumbnails(self):
            return self

        def set(self, **k):
            raise HttpError(httplib2.Response({"status": 403}), b'{"error":{"errors":[{"reason":"forbidden"}]}}')

    jpg = tmp_path / "t.jpg"
    jpg.write_bytes(b"\xff\xd8\xff")
    st: dict = {}
    assert upload.set_thumbnail(Svc(), "vid", jpg, st) is False and st["forbidden"]


@needs_ffmpeg
def test_thumbnails_stop_for_a_week_after_a_403(env, monkeypatch):
    s, db, runner, calls = env
    tries = []

    def forbidden(svc, vid, jpg, st=None):
        tries.append(vid)
        st["forbidden"] = True
        return False

    monkeypatch.setattr(upload, "set_thumbnail", forbidden)
    assert runner.attempt(today(s)) == "done"
    assert len(tries) == 1 and float(db.kv_get("thumbs_forbidden_until")) > time.time() + 6 * 86400


# ------------------------------------------------------------------ R5: stats
class FakeYT:
    def __init__(self, views):
        self.views, self.comments = views, []

    def videos(self):
        return self

    def list(self, part, id):
        self._ids = id.split(",")
        return self

    def commentThreads(self):
        return self

    def insert(self, part, body):
        self.comments.append(body["snippet"]["videoId"])
        return self

    def execute(self):
        if getattr(self, "_ids", None):
            ids, self._ids = self._ids, None
            return {"items": [{"id": i, "statistics": {"viewCount": str(self.views.get(i, 0)),
                                                     "likeCount": "1", "commentCount": "0"}} for i in ids]}
        return {}


def _uploaded(db, key, kind, source, views_by_id, age_days=4, category="funny"):
    db.ensure_run(key)
    db.update_run(key, source=source, status="done")
    rows = [{"start": 0, "end": 30, "duration": 30, "heading": "h", "title": f"t{v}", "description": "",
             "category": category, "score": 10, "comment_question": "Which one?"} for v in views_by_id]
    db.add_clips(key, rows, kind=kind)
    pub = datetime.fromtimestamp(time.time() - age_days * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for c, vid in zip(db.clips_for(key), views_by_id):
        db.update_clip(c["id"], status="uploaded", youtube_id=vid, publish_at=pub)


def test_stats_checkpoints_weights_and_summary(env_settings):
    db = Database(env_settings.db_path)
    env_settings.cfg["stats"]["min_clips_for_weights"] = 4
    _uploaded(db, "2026-11-01:podcast", "podcast", "Good", ["a", "b", "c"])
    _uploaded(db, "2026-11-02:podcast", "podcast", "Bad", ["d", "e", "f"])
    yt = FakeYT({"a": 900, "b": 700, "c": 800, "d": 5, "e": 9, "f": 3})
    assert stats.fetch(env_settings, db, yt) == 12          # 6 clips x checkpoints 1 and 3
    assert stats.fetch(env_settings, db, yt) == 0           # never twice
    m = stats.source_multipliers(env_settings, db)
    assert m["Good"] > 1.5 and m["Bad"] == 0.25
    text = stats.summary_block(env_settings, db)
    assert "By source" in text and "Good" in text and "900" in text


def test_comments_only_with_the_scope_and_after_publishing(env_settings):
    db = Database(env_settings.db_path)
    _uploaded(db, "2026-11-01:podcast", "podcast", "S", ["a"])
    _uploaded(db, "2026-11-02:podcast", "podcast", "S", ["z"], age_days=-1)   # not public yet
    yt = FakeYT({})
    assert stats.post_due_comments(env_settings, db, yt) == 0   # login lacks the comment scope
    (env_settings.secrets_dir / "token.json").write_text(json.dumps({"scopes": [stats.SCOPE_COMMENTS]}))
    assert stats.post_due_comments(env_settings, db, yt) == 1 and yt.comments == ["a"]
    assert stats.post_due_comments(env_settings, db, yt) == 0   # once per video


# ------------------------------------------------------------------ R6: growth
def _views(db, kind, values, key):
    ids = [f"{key}-{i}" for i in range(len(values))]
    _uploaded(db, key, kind, "S", ids)
    for i, v in zip(ids, values):
        db.add_stat(i, 3, v, 0, 0)


def test_growth_goes_up_only_on_views_and_holds_on_a_strike(env_settings):
    db = Database(env_settings.db_path)
    day = today(env_settings)
    assert growth_level(env_settings, db, "podcast", day) == 0
    _views(db, "podcast", [80, 90, 60, 70, 100], "2026-11-05:podcast")
    db.kv_set("level:podcast", json.dumps({"level": 0, "since": "2026-11-01", "evaluated": "x"}))
    db.disable_source("S", "claim on v")
    assert growth_level(env_settings, db, "podcast", day) == 0              # strike: hold
    db.execute("DELETE FROM disabled_sources")
    db.kv_set("level:podcast", json.dumps({"level": 0, "since": "2026-11-01", "evaluated": "x"}))
    assert growth_level(env_settings, db, "podcast", day) == 1              # median 80 >= 50
    assert growth_level(env_settings, db, "podcast", day) == 1              # once a day


def test_growth_drops_a_level_in_a_slump(env_settings):
    db = Database(env_settings.db_path)
    _views(db, "gta6_talk", [1, 2, 3, 2, 1], "2026-11-05:gta6_talk")
    db.kv_set("level:gta6", json.dumps({"level": 2, "since": "2026-11-01", "evaluated": "x"}))
    assert growth_level(env_settings, db, "gta6", today(env_settings)) == 1


def test_quota_shifts_toward_the_kind_that_gets_views(env_settings):
    db = Database(env_settings.db_path)
    _views(db, "podcast", [3, 4, 2, 5, 3], "2026-11-05:podcast")
    _views(db, "gta6_talk", [300, 400, 200, 500, 300], "2026-11-05:gta6_talk")
    assert shift_quota(env_settings, db, 5, 3) == (3, 5)


# ------------------------------------------------------------------ render, captions, audio guard
def test_full_layout_has_no_gameplay_panel(env_settings):
    from pathlib import Path

    from autoyt.render import build_command
    cmd = build_command(env_settings, section=Path("a.mp4"), ass_name="a.ass", out_tmp=Path("o.mp4"),
                        duration=30.0, gameplay=None, music=None, layout_name="full")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "vstack" not in graph and "[1:v]" not in graph and "scale=1080:1920" in graph
    assert "ass=a.ass" in graph and cmd.count("-i") == 1


def test_captions_anchor_and_short_credit():
    from autoyt.config import DEFAULTS
    from autoyt.subtitles import build_ass
    lay = DEFAULTS["layout"]
    words = [{"w": "hello", "s": 0.2, "e": 0.5}, {"w": "world", "s": 0.6, "e": 0.9}]
    ass = build_ass(words, "Heading", "Credit", 20.0, lay)
    assert "pos(540,620)" in ass and "pos(540,1110)" not in ass        # not on the seam any more
    assert "Dialogue: 1,0:00:00.00,0:00:01.50,Credit" in ass
    full = build_ass(words, "Heading", "Credit", 20.0, lay, full=True)
    assert "pos(540,1240)" in full
    none = build_ass(words, "HOOK", "Credit", 20.0, lay, captions=False)
    assert "Dialogue: 2" not in none and "HOOK" in none
    assert "Dialogue: 1,0:00:00.00,0:00:20.00,Head" in none            # heading from frame one


@needs_ffmpeg
def test_music_ratio_tells_a_tone_from_speech_like_audio(tmp_path):
    from autoyt.validate import music_ratio
    tone, talk = tmp_path / "tone.wav", tmp_path / "talk.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=8", str(tone)], check=True)
    # noise switched on and off ~4 times a second, like syllables and pauses
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=8:a=0.4",
                    "-af", "volume='if(lt(mod(t,0.25),0.13),1,0.02)':eval=frame", str(talk)], check=True)
    assert music_ratio(tone) > 0.8
    assert music_ratio(talk) < 0.2


@needs_ffmpeg
def test_blur_bars_are_cropped_away(env_settings):
    from autoyt.assets import _detect_bars
    raw = env_settings.data_dir / "bars.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=720x680:r=30:d=2",
                    "-vf", "split[a][b];[a]scale=720:1280,boxblur=20:3[bg];[bg][b]overlay=0:300",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw)], check=True)
    w, h, x, y = _detect_bars(raw)
    assert abs(y - 300) <= 16 and abs(h - 680) <= 32 and w >= 700


# ------------------------------------------------------------------ GTA batches end to end
def _gameplay_env(env, monkeypatch, label="GTA 5"):
    s, db, runner, calls = env
    (s.config_dir / "sources.yaml").write_text(
        f"- {{name: G, url: u, permission: x, kind: gta6_gameplay, game_label: '{label}'}}\n")
    monkeypatch.setattr(pipeline, "pick_video", lambda *a, **k: Picked(
        {"name": "G", "credit": "GTA Clips"}, "gv1", "GTA V chaos", 1800.0, "https://youtu.be/gv1"))
    monkeypatch.setattr(segments, "select_segments", lambda *a, **k: [
        {"start": 100.0, "end": 125.0, "duration": 25.0, "score": 1.0},
        {"start": 400.0, "end": 422.0, "duration": 22.0, "score": 0.8}])
    monkeypatch.setattr(transcribe, "transcribe_section",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Whisper for gameplay")))
    return s, db, runner, calls


@needs_ffmpeg
def test_gameplay_batch_full_screen_no_captions_gaming_category(env, monkeypatch):
    s, db, runner, calls = _gameplay_env(env, monkeypatch)
    key = today(s) + ":gta6_gameplay"
    assert runner.attempt(key) == "done"
    clips = db.clips_for(key)
    assert len(clips) == 2 and all(c["kind"] == "gta6_gameplay" and c["category"] is None for c in clips)
    assert all(u["category"] == "gaming" for u in calls["uploads"])
    assert all("GTA 6" not in u["title"] and "#GTA5" in u["description"] for u in calls["uploads"])


def test_kind_with_no_sources_is_skipped_not_failed(env, monkeypatch):
    s, db, runner, calls = env
    monkeypatch.setattr(pipeline, "pick_video", lambda *a, **k: (_ for _ in ()).throw(SourceRejected("no enabled sources")))
    key = today(s) + ":gta6_talk"
    assert runner.attempt(key) == "skipped"
    assert db.get_run(key)["status"] == "skipped"


@needs_ffmpeg
def test_talk_clip_with_music_like_audio_is_dropped(env, monkeypatch):
    s, db, runner, calls = env      # the fake sections carry a steady sine tone: "music"
    key = today(s) + ":gta6_talk"
    assert runner.attempt(key) == "done"
    assert calls["uploads"] == []
    assert all(c["status"] == "deleted" and "music" in c["error"] for c in db.clips_for(key))


def test_full_fit_layout_keeps_more_of_the_frame_over_a_blur(env_settings):
    from pathlib import Path

    from autoyt.render import build_command
    cmd = build_command(env_settings, section=Path("a.mp4"), ass_name="a.ass", out_tmp=Path("o.mp4"),
                        duration=30.0, gameplay=None, music=None, layout_name="full_fit")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "boxblur" in graph and "scale=-2:1152" in graph and "overlay" in graph and cmd.count("-i") == 1
