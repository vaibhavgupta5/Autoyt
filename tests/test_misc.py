import random
import wave

import pytest
import yaml

from autoyt import sources, ytdlp
from autoyt.config import load_settings
from autoyt.db import Database
from autoyt.errors import SourceRejected
from autoyt.transcribe import load_wav


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOYT_HOME", str(tmp_path))
    st = load_settings()
    st.ensure_dirs()
    st.config_dir.mkdir(exist_ok=True)
    return st


def _write_sources(s, entries):
    (s.config_dir / "sources.yaml").write_text(yaml.safe_dump(entries))


def test_quality_is_capped_at_720p(s):
    for audio in (True, False):
        fmt = ytdlp.video_format(s, audio)
        assert "height<=720" in fmt and "1080" not in fmt and "2160" not in fmt


def test_load_wav(tmp_path):
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((b"\x00\x40" * 16000))  # 0.5 amplitude for 1 s
    a = load_wav(p)
    assert a.shape == (16000,) and abs(float(a[0]) - 0.5) < 0.01
    with wave.open(str(tmp_path / "bad.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 100)
    with pytest.raises(RuntimeError):
        load_wav(tmp_path / "bad.wav")


def _fmt(w, h, tbr=2000, vcodec="avc1.4d401f"):
    return {"format_id": f"{w}x{h}", "width": w, "height": h, "tbr": tbr, "vcodec": vcodec}


def test_pick_format_uses_short_side_so_portrait_720p_is_not_downgraded():
    portrait = {"formats": [_fmt(360, 640), _fmt(480, 854), _fmt(720, 1280), _fmt(1080, 1920),
                            {"format_id": "audio", "vcodec": "none"}]}
    f = ytdlp.pick_format(portrait, 720)
    assert (f["width"], f["height"]) == (720, 1280)  # a plain height<=720 cap would have picked 360x640
    landscape = {"formats": [_fmt(640, 360), _fmt(1280, 720), _fmt(1920, 1080)]}
    assert ytdlp.pick_format(landscape, 720)["height"] == 720
    low_only = {"formats": [_fmt(360, 640), _fmt(480, 854)]}
    f = ytdlp.pick_format(low_only, 720)
    assert min(f["width"], f["height"]) < 700  # caller rejects this: not true 720p
    assert ytdlp.pick_format({"formats": []}, 720) is None


@pytest.mark.parametrize("size", ["720x1280", "404x720", "1280x720"])
def test_gameplay_prep_always_yields_1080x770_without_failing(s, size):
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg needed")
    from autoyt.assets import prep_gameplay

    raw = s.data_dir / f"raw_{size}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw)], check=True)
    out = s.data_dir / f"prep_{size}.mp4"
    prep_gameplay(s, raw, out, 0, 2)
    dims = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                          capture_output=True, text=True).stdout.strip()
    assert dims == "1080,770"


def test_search_results_must_be_about_the_game():
    import re

    from autoyt.assets import _title_ok

    excl = re.compile("react|tutorial", re.I)
    assert _title_ok("Subway Surfers Gameplay 9:16", excl, "subway")
    assert not _title_ok("Minecraft Parkour Gameplay NO COPYRIGHT (Vertical)", excl, "subway")  # the real mistake
    assert not _title_ok("Subway Surfers tutorial", excl, "subway")
    assert _title_ok("Temple Run 2 High Score", excl, "temple run")
    assert _title_ok("anything", excl, None)


def test_gameplay_filter_choices():
    from autoyt.assets import gameplay_filter

    assert "crop=1080:770:0:" in gameplay_filter(720, 1280, 1080, 770)       # sharp portrait: fill width, lower part
    assert "boxblur" in gameplay_filter(404, 720, 1080, 770)                 # low-res portrait: whole game over blur
    assert "boxblur" not in gameplay_filter(1280, 720, 1080, 770)            # wide: plain cover


def test_sixty_second_gameplay_file_fits_a_55s_clip(s):
    import json

    from autoyt.assets import choose_gameplay

    gdir = s.assets_dir / "gameplay" / "subway_surfers"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "a.mp4").write_bytes(b"x")
    (s.assets_dir / "gameplay" / "index.json").write_text(json.dumps(
        [{"game": "subway_surfers", "file": "subway_surfers/a.mp4", "duration": 60.0, "video_id": "a", "title": "t"}]))
    for seed in range(20):
        g = choose_gameplay(s, None, 55.0, random.Random(seed))
        assert g and 0 <= g["start"] <= 4.8 and g["start"] + 55.0 <= 60.0  # never runs past the end
    g = choose_gameplay(s, None, 61.0)  # a clip longer than the file: the file is looped, never cut short
    assert g and g["loop"] is True and g["start"] == 0.0


def test_render_loops_a_short_gameplay_file(s):
    from pathlib import Path

    from autoyt.render import build_command

    cmd = build_command(s, section=Path("a.mp4"), ass_name="a.ass", out_tmp=Path("o.mp4"), duration=52.0,
                        gameplay={"path": Path("g.mp4"), "start": 0.0, "loop": True}, music=None)
    i = cmd.index("-stream_loop")
    assert cmd[i + 1] == "-1" and cmd.index("g.mp4") > i
    plain = build_command(s, section=Path("a.mp4"), ass_name="a.ass", out_tmp=Path("o.mp4"), duration=52.0,
                          gameplay={"path": Path("g.mp4"), "start": 3.0}, music=None)
    assert "-stream_loop" not in plain


def test_sources_require_permission(s):
    _write_sources(s, [{"name": "X", "url": "https://youtube.com/@x"}])
    with pytest.raises(ValueError, match="permission"):
        sources.load_sources(s)


def test_listing_filters_length_title_and_live(s, monkeypatch):
    db = Database(s.db_path)
    src = {"name": "Show", "url": "https://youtube.com/@show", "tabs": ["videos", "streams"],
           "title_include": ["Ep\\."], "permission": "x"}
    entries = [
        {"id": "a", "title": "Ep. 1 full", "duration": 5000},          # ok (83 min)
        {"id": "b", "title": "Ep. 2 short", "duration": 600},          # too short
        {"id": "c", "title": "Ep. 3 huge", "duration": 99999},         # too long
        {"id": "d", "title": "Trailer", "duration": 5000},             # title filter
        {"id": "e", "title": "Ep. 5 live", "duration": 5000, "live_status": "is_live"},
        {"id": "f", "title": "Ep. 6", "duration": None},               # unknown length
    ]
    monkeypatch.setattr(ytdlp, "list_flat", lambda st, url, limit: entries if url.endswith("/videos") else [])
    assert sources.refresh_listing(s, db, src, force=True) == 1
    assert [r["video_id"] for r in db.listing("Show")] == ["a"]


def test_caption_parser_gives_word_timing_and_visible_pauses(tmp_path):
    import json

    from autoyt.captions import build_transcript
    from autoyt.sentences import build_sentences

    ev = [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello", "tOffsetMs": 0},
                                                           {"utf8": " there", "tOffsetMs": 400},
                                                           {"utf8": " friend", "tOffsetMs": 900}]},
          {"tStartMs": 3000, "dDurationMs": 100, "segs": [{"utf8": "\n"}]},                      # newline marker
          {"tStartMs": 1900, "dDurationMs": 1500, "segs": [{"utf8": "friend", "tOffsetMs": 0}]},  # rolling repeat
          {"tStartMs": 9000, "dDurationMs": 1000, "segs": [{"utf8": "after", "tOffsetMs": 0},
                                                           {"utf8": " pause", "tOffsetMs": 500}]}]
    p = tmp_path / "c.json3"
    p.write_text(json.dumps({"events": ev}))
    tr = build_transcript(p)
    ws = tr["words"]
    assert [w["w"] for w in ws] == ["hello", "there", "friend", "after", "pause"]   # the repeat is dropped
    assert ws[0]["s"] == 1.0 and ws[1]["s"] == 1.4 and ws[2]["s"] == 1.9
    assert all(w["e"] > w["s"] for w in ws) and tr["source"] == "captions"
    assert ws[2]["e"] - ws[2]["s"] <= 0.9, "a long pause must stay visible as a gap, not be absorbed into the word"
    sents = build_sentences(ws, punctuated=False)           # no punctuation in auto-captions: cut at pauses
    assert len(sents) == 2 and sents[0]["text"] == "hello there friend" and sents[1]["text"] == "after pause"


def test_links_are_stripped_and_tags_cleaned():
    from autoyt.validate import clean_tags, strip_links

    assert strip_links("watch https://youtu.be/abc?t=5 now, or www.site.com/x and spam.io today") == "watch now, or and today"
    assert strip_links("Kevin MacLeod (incompetech.com)") == "Kevin MacLeod"
    assert clean_tags(["Jake Paul", "#Boxing", "https://x.com/a", "a", "jake paul", "x" * 40, "MMA & UFC", None, 5]) \
        == ["jake paul", "boxing", "mma & ufc"]
    assert clean_tags("not a list") == []
    assert len(clean_tags([f"tag{i}" for i in range(20)])) == 8


def test_description_has_no_links_and_hashtags_come_from_tags():
    from autoyt.pipeline import Runner

    r = {"context_line": "Context http://a.example.com", "description": "Desc www.b.example.org",
         "category": "funny", "tags": '["jake paul", "boxing", "ufc"]'}
    run = {"credit": "Full Send Podcast (Nelk Boys)", "video_title": "Big episode https://y.example.com"}
    d = Runner._description(r, run, "Rocket Power by Kevin MacLeod. Licensed under Creative Commons: By Attribution 4.0")
    assert "http" not in d and "www" not in d and ".com" not in d and ".org" not in d
    assert "Clip from Full Send Podcast (Nelk Boys): Big episode" in d and "Rocket Power by Kevin MacLeod" in d
    tags_line = d.strip().splitlines()[-1]
    assert tags_line.startswith("#shorts #funny #fullsendpodcast") and tags_line.count("#") <= 5
    assert " " not in tags_line.replace(" #", "#").strip("# ").replace("#", "")  # each hashtag is one word
    t = Runner._tags(r, run)
    assert t[:3] == ["jake paul", "boxing", "ufc"] and "shorts" in t and "full send podcast" in t
    assert sum(len(x) + 3 for x in t) <= 450


def test_music_is_brought_to_a_common_loudness(s):
    import re
    from pathlib import Path

    from autoyt.render import build_command

    def vol(lufs):
        cmd = build_command(s, section=Path("a.mp4"), ass_name="a.ass", out_tmp=Path("o.mp4"), duration=52.0,
                            gameplay=None, music={"path": Path("m.mp3"), "start": 0.0, "lufs": lufs})
        return float(re.search(r"volume=([\d.]+)", cmd[cmd.index("-filter_complex") + 1]).group(1))

    loud, quiet, unknown = vol(-8.0), vol(-17.0), vol(None)
    assert loud < quiet, "a loud track must be turned down more than a quiet one"
    assert abs(20 * __import__("math").log10(loud) - (-24 - -8.0)) < 0.1      # lands at -24 LUFS
    assert abs(20 * __import__("math").log10(quiet) - (-24 - -17.0)) < 0.1
    assert abs(20 * __import__("math").log10(unknown) - (-24)) < 0.1           # fallback fixed gain


def test_archive_day_lets_a_day_run_again_and_keeps_uploaded_clips_tracked(s):
    db = Database(s.db_path)
    db.ensure_run("2026-01-01")
    db.update_run("2026-01-01", status="done", source="Show", video_id="abc")
    db.add_clips("2026-01-01", [{"start": 1.0, "end": 53.0, "duration": 52.0, "heading": "h", "title": "t",
                                 "description": "d", "category": "funny", "score": 15, "tags": ["x"]}])
    db.mark_step("2026-01-01", "select")
    assert db.archive_day("2026-01-01") == "2026-01-01#1"
    assert db.get_run("2026-01-01") is None and db.get_run("2026-01-01#1")["video_id"] == "abc"
    assert db.clips_for("2026-01-01") == [] and len(db.clips_for("2026-01-01#1")) == 1
    assert not db.step_done("2026-01-01", "select") and db.step_done("2026-01-01#1", "select")
    db.ensure_run("2026-01-01")
    assert db.archive_day("2026-01-01") == "2026-01-01#2"


def test_upload_visibility_in_each_mode(tmp_path):
    from autoyt.upload import upload_video

    seen = {}

    class Req:
        def next_chunk(self):
            return None, {"id": "vid123"}

    class Videos:
        def insert(self, part, body, media_body):
            seen["body"] = body
            return Req()

    class Svc:
        def videos(self):
            return Videos()

    f = tmp_path / "c.mp4"
    f.write_bytes(b"x" * 100)
    kw = dict(title="t", description="d", tags=["a"], category="funny")
    # immediate public
    assert upload_video(Svc(), f, publish_at=None, privacy="public", **kw) == "vid123"
    assert seen["body"]["status"]["privacyStatus"] == "public" and "publishAt" not in seen["body"]["status"]
    # scheduled: YouTube needs private + publishAt, and the video turns public at that time
    upload_video(Svc(), f, publish_at="2030-01-01T10:00:00.000Z", privacy="public", **kw)
    st = seen["body"]["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"] == "2030-01-01T10:00:00.000Z"
    # the old default is still private
    upload_video(Svc(), f, publish_at=None, **kw)
    assert seen["body"]["status"]["privacyStatus"] == "private"
    assert seen["body"]["status"]["selfDeclaredMadeForKids"] is False


def test_all_four_modes_are_accepted_and_junk_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOYT_HOME", str(tmp_path))
    for m in ("dry_run", "scheduled", "public", "private"):
        monkeypatch.setenv("AUTOYT_MODE", m)
        assert load_settings().mode == m
    monkeypatch.setenv("AUTOYT_MODE", "everything")
    with pytest.raises(ValueError, match="AUTOYT_MODE"):
        load_settings()


def test_gemini_reply_parser_is_tolerant():
    from autoyt.select_clips import _clip_schema, _parse_json

    good = '{"clips":[{"start_sentence":1,"end_sentence":9}]}'
    assert len(_parse_json(good)) == 1
    assert len(_parse_json("```json\n" + good + "\n```")) == 1                 # code fences
    assert len(_parse_json(good + ' {"stray": true}')) == 1                    # trailing junk (seen for real)
    assert len(_parse_json("Sure! Here you go: " + good)) == 1                 # chatty prefix
    assert _parse_json('[{"start_sentence":1}]') == [{"start_sentence": 1}]    # bare list
    with pytest.raises(ValueError):
        _parse_json('{"clips": [{"start_sentence": ')                          # truly broken -> caller retries
    schema = _clip_schema()
    item = schema["properties"]["clips"]["items"]
    assert item["properties"]["category"]["enum"] == ["funny", "informative", "deep"]
    assert set(item["required"]) >= {"start_sentence", "end_sentence", "heading", "category"}


def test_gemini_drops_schema_if_api_rejects_it(s, monkeypatch):
    import requests

    from autoyt import select_clips as sc

    s.gemini_api_key = "k"
    sent = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, str(body)

        def json(self):
            return self._b

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append("responseSchema" in json["generationConfig"])
        if sent[-1]:
            return R(400, {"error": {"message": "Invalid JSON payload: responseSchema unsupported"}})
        return R(200, {"candidates": [{"content": {"parts": [{"text": '{"clips":[{"start_sentence":1}]}'}]}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(sc.time, "sleep", lambda x: None)
    assert sc.call_gemini(s, "p") == [{"start_sentence": 1}]
    assert sent == [True, False]  # tried with the schema, then without


def test_channel_with_no_matches_is_not_relisted_every_time(s, monkeypatch):
    db = Database(s.db_path)
    src = {"name": "Empty", "url": "https://youtube.com/@e", "tabs": ["videos"], "permission": "x"}
    calls = []
    monkeypatch.setattr(ytdlp, "list_flat", lambda st, url, limit: calls.append(url) or [])
    assert sources.refresh_listing(s, db, src) == 0
    assert sources.refresh_listing(s, db, src) == 0
    assert len(calls) == 1, "second call must use the remembered listing time, not hit YouTube again"
    sources.refresh_listing(s, db, src, force=True)
    assert len(calls) == 2


def test_pick_skips_used_bad_and_rejected(s, monkeypatch):
    db = Database(s.db_path)
    _write_sources(s, [{"name": "Show", "url": "https://youtube.com/@show", "permission": "x"}])
    db.replace_listing("Show", [{"id": i, "title": f"Ep {i}", "duration": 5000} for i in "abcd"])
    db.mark_used("a", "Show")
    db.mark_bad("b", "members only")
    seen = []

    def fake_check(st, src, vid):
        seen.append(vid)
        if vid == "c":
            raise SourceRejected("age restricted")
        return {"title": "T", "duration": 5000}

    monkeypatch.setattr(sources, "check_video", fake_check)
    p = sources.pick_video(s, db, random.Random(1))
    assert p.video_id == "d"
    assert "a" not in seen and "b" not in seen
    assert "c" in db.bad_ids()  # a rejected video is never offered again


def test_disabled_source_is_not_picked(s, monkeypatch):
    db = Database(s.db_path)
    _write_sources(s, [{"name": "One", "url": "u1", "permission": "x"}, {"name": "Two", "url": "u2", "permission": "x"}])
    for n in ("One", "Two"):
        db.replace_listing(n, [{"id": n[0], "title": "t", "duration": 5000}])
    db.disable_source("One", "copyright")
    monkeypatch.setattr(sources, "check_video", lambda st, src, vid: {"title": "T", "duration": 5000})
    for seed in range(5):
        assert sources.pick_video(s, db, random.Random(seed)).source["name"] == "Two"
