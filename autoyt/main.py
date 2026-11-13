"""Command line: `autoyt daemon` is what the container runs."""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import assets, scheduler, subtitles, transcribe, upload
from .config import load_settings
from .db import Database
from .errors import ActionNeeded
from .logutil import setup_logging
from .notify import send_email, smtp_configured
from .kinds import KIND_LABEL, growth_level, kind_of, plan_day
from .pipeline import Runner, clips_target, today
from .render import probe_duration, render_clip
from .sources import load_sources, refresh_listing


def cmd_daemon(s, a):
    scheduler.daemon(s)


def cmd_run(s, a):
    """Run a day's batches now, one after another (or a single batch when --day is a full run key)."""
    db = Database(s.db_path)
    day = a.day or today(s)
    keys = [day] if ":" in day else [b.key for b in plan_day(s, db, day=day)]
    results = []
    runner = Runner(s, db)
    for key in keys:
        if a.fresh and db.get_run(key):
            print(f"archived the earlier run of {key} as {db.archive_day(key)}; starting a new one")
        elif a.force:
            db.ensure_run(key)
            db.update_run(key, status="pending", attempts=0, next_try_at=None, error=None)
            db.execute("DELETE FROM notified WHERE day=?", (key,))
        run = db.get_run(key)
        if run and run["status"] in ("done", "skipped") and not (a.fresh or a.force):
            print(f"{key}: already {run['status']}")
            results.append(run["status"])
            continue
        res = runner.attempt(key)
        print(f"{key}: {res}")
        results.append(res)
    return 0 if all(r in ("done", "skipped") for r in results) else 1


def cmd_status(s, a):
    db = Database(s.db_path)
    lv = {g: growth_level(s, db, g) for g in ("podcast", "gta6")}
    print(f"mode={s.mode} podcast clips/day={clips_target(s, db)} growth levels={lv} "
          f"first_upload_day={db.kv_get('first_upload_day')}")
    block = float(db.kv_get("bot_block_until", "0") or 0)
    if block > time.time():
        print(f"YouTube bot check: backing off until {time.strftime('%Y-%m-%d %H:%M', time.localtime(block))}")
    for b in plan_day(s, db):
        r = db.get_run(b.key)
        print(f"plan {b.key:28} {KIND_LABEL.get(b.kind, b.kind):14} x{b.clips}  due "
              f"{time.strftime('%H:%M', time.localtime(b.due))}  {r['status'] if r else 'not started'}")
    for r in db.query("SELECT * FROM runs ORDER BY updated_at DESC LIMIT 10"):
        print(f"{r['day']:28} {r['status']:8} {KIND_LABEL.get(kind_of(r['day']), ''):14} attempts={r['attempts']}  "
              f"{r['source'] or '-'}  {(r['video_title'] or '')[:40]}")
        for c in db.clips_for(r["day"]):
            print(f"    {c['idx']:2} {c['status']:9} {(c['category'] or '-'):11} {(c['title'] or '')[:50]} "
                  f"{c['youtube_id'] or ''} {c['publish_at'] or ''}")
        if r["error"]:
            print(f"    error: {r['error'][:150]}")
    dis = db.disabled_sources()
    if dis:
        print("disabled sources:", dis)


def cmd_auth(s, a):
    upload.run_auth(s, bind=a.bind, port=a.port, extended=a.extended)


def cmd_stats(s, a):
    from . import stats
    db = Database(s.db_path)
    if s.mode != "dry_run":
        n = stats.fetch(s, db, upload.get_service(s))
        print(f"recorded {n} new checkpoint rows")
    print(stats.summary_block(s, db))


def cmd_test_email(s, a):
    if not smtp_configured(s):
        print("SMTP_HOST, SMTP_FROM and NOTIFY_EMAIL must be set in .env")
        return 1
    ok = send_email(s, "Test email", "If you can read this, AutoYT email notifications work.")
    print("sent" if ok else "FAILED (see log above)")
    return 0 if ok else 1


def cmd_check_sources(s, a):
    db = Database(s.db_path)
    dis = db.disabled_sources()
    for src in load_sources(s):
        state = "disabled-by-strike" if src["name"] in dis else ("enabled" if src["enabled"] else "off")
        n = refresh_listing(s, db, src, force=True)
        print(f"{src['name']:24} {state:18} eligible public videos: {n}")


def cmd_fetch_assets(s, a):
    for p in assets.fetch_music(s):
        print("music:", p.name)


def cmd_fetch_gameplay(s, a):
    assets.ensure_library(s)
    for e in assets._load_index(s.assets_dir / "gameplay" / "index.json"):
        print(f"gameplay: {e['game']:16} {e['file']}  {e['duration'] / 60:.0f} min  {e['title'][:60]}")


def cmd_monitor(s, a):
    from . import monitor
    db = Database(s.db_path)
    found = monitor.check_uploaded(s, db, upload.get_service(s), today(s))
    print("\n".join(found) or "nothing wrong found")


def cmd_cleanup(s, a):
    Runner(s, Database(s.db_path)).maintenance(today(s))
    print("cleanup done")


def cmd_render_clip(s, a):
    """Manual test: cut [start,end] from a local video and render it with captions and heading."""
    src = Path(a.input)
    dur = a.end - a.start
    work = s.work_dir / "manual"
    work.mkdir(parents=True, exist_ok=True)
    section = work / "section.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(a.start), "-t", str(dur), "-i", str(src),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(section)], check=True)
    wav = work / "chunk_000.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(section), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(wav)], check=True)
    for old in work.glob("tr_*.json"):
        old.unlink()
    tr = {"words": []}
    if not a.no_captions:
        tr = transcribe.transcribe_chunks(s, [wav], work, model_name=a.model or s.whisper_model)
        transcribe.release_model()
    rel = [] if a.no_captions else subtitles.clip_relative_words(tr["words"], 0, dur)
    full = a.layout in ("full", "full_fit")
    ass = subtitles.build_ass(rel, a.heading, a.credit, dur, s.cfg["layout"], captions=not a.no_captions,
                              full=full)
    game = None if full else assets.choose_gameplay(s, None, dur)
    music = None if full else assets.choose_music(s, dur)
    out = Path(a.output)
    render_clip(s, section=section, ass_text=ass, out_path=out, duration=dur, gameplay=game, music=music,
                layout_name=a.layout)
    print(f"wrote {out} ({probe_duration(out):.1f}s) gameplay={'yes' if game else 'placeholder'} music={'yes' if music else 'none'}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="autoyt")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="run forever: one run per day, with retries").set_defaults(fn=cmd_daemon)
    r = sub.add_parser("run", help="run today's job now")
    r.add_argument("--force", action="store_true", help="reset today's state first (resumes the same video)")
    r.add_argument("--fresh", action="store_true", help="archive today's earlier run and start a brand-new one")
    r.add_argument("--day", help="a day (all its batches) or one run key such as 2026-11-12:gta6_talk")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status", help="show recent runs").set_defaults(fn=cmd_status)
    au = sub.add_parser("auth", help="log in to YouTube once")
    au.add_argument("--bind", default="0.0.0.0")
    au.add_argument("--port", type=int, default=8765)
    au.add_argument("--extended", action="store_true",
                    help="also allow question comments and retention stats (YouTube Analytics)")
    au.set_defaults(fn=cmd_auth)
    sub.add_parser("test-email").set_defaults(fn=cmd_test_email)
    sub.add_parser("check-sources", help="count eligible public videos per channel").set_defaults(fn=cmd_check_sources)
    sub.add_parser("fetch-assets", help="download background music").set_defaults(fn=cmd_fetch_assets)
    sub.add_parser("fetch-gameplay", help="download or repair the gameplay library").set_defaults(fn=cmd_fetch_gameplay)
    sub.add_parser("monitor", help="check uploaded videos for rejections").set_defaults(fn=cmd_monitor)
    sub.add_parser("stats", help="fetch view counts and print the performance summary").set_defaults(fn=cmd_stats)
    sub.add_parser("cleanup").set_defaults(fn=cmd_cleanup)
    rc = sub.add_parser("render-clip", help="manual render test from a local file")
    rc.add_argument("--input", required=True)
    rc.add_argument("--start", type=float, default=0.0)
    rc.add_argument("--end", type=float, default=52.0)
    rc.add_argument("--heading", default="This is a test heading")
    rc.add_argument("--credit", default="Test Channel")
    rc.add_argument("--layout", default="fit_blur", choices=["fit_blur", "center_crop", "full", "full_fit"])
    rc.add_argument("--no-captions", action="store_true", help="heading and credit only (gameplay)")
    rc.add_argument("--model")
    rc.add_argument("--output", default="data/manual_clip.mp4")
    rc.set_defaults(fn=cmd_render_clip)

    a = p.parse_args(argv)
    setup_logging()
    s = load_settings()
    s.ensure_dirs()
    try:
        return a.fn(s, a) or 0
    except ActionNeeded as e:
        print(f"ACTION NEEDED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
