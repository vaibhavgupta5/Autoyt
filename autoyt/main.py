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
from .pipeline import Runner, clips_target, today
from .render import probe_duration, render_clip
from .sources import load_sources, refresh_listing


def cmd_daemon(s, a):
    scheduler.daemon(s)


def cmd_run(s, a):
    db = Database(s.db_path)
    day = a.day or today(s)
    if a.fresh and db.get_run(day):
        print(f"archived the earlier run of {day} as {db.archive_day(day)}; starting a new one")
    elif a.force:
        db.ensure_run(day)
        db.update_run(day, status="pending", attempts=0, next_try_at=None, error=None)
        db.execute("DELETE FROM notified WHERE day=?", (day,))
    result = Runner(s, db).attempt(day)
    print(f"result: {result}")
    return 0 if result == "done" else 1


def cmd_status(s, a):
    db = Database(s.db_path)
    print(f"mode={s.mode} clips/day target now={clips_target(s, db)} first_upload_day={db.kv_get('first_upload_day')}")
    for r in db.query("SELECT * FROM runs ORDER BY day DESC LIMIT 7"):
        print(f"{r['day']}  {r['status']:8} attempts={r['attempts']}  {r['source'] or '-'}  {(r['video_title'] or '')[:50]}")
        for c in db.clips_for(r["day"]):
            print(f"    {c['idx']:2} {c['status']:9} {c['category']:11} {c['title'][:50]} {c['youtube_id'] or ''}")
        if r["error"]:
            print(f"    error: {r['error'][:150]}")
    dis = db.disabled_sources()
    if dis:
        print("disabled sources:", dis)


def cmd_auth(s, a):
    upload.run_auth(s, bind=a.bind, port=a.port)


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
    for e in assets.fetch_gameplay(s):
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
    tr = transcribe.transcribe_chunks(s, [wav], work, model_name=a.model or s.whisper_model)
    transcribe.release_model()
    rel = subtitles.clip_relative_words(tr["words"], 0, dur)
    ass = subtitles.build_ass(rel, a.heading, a.credit, dur, s.cfg["layout"])
    db = Database(s.db_path)
    game = assets.choose_gameplay(s, None, dur)
    music = assets.choose_music(s, dur)
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
    r.add_argument("--day")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status", help="show recent runs").set_defaults(fn=cmd_status)
    au = sub.add_parser("auth", help="log in to YouTube once")
    au.add_argument("--bind", default="0.0.0.0")
    au.add_argument("--port", type=int, default=8765)
    au.set_defaults(fn=cmd_auth)
    sub.add_parser("test-email").set_defaults(fn=cmd_test_email)
    sub.add_parser("check-sources", help="count eligible public videos per channel").set_defaults(fn=cmd_check_sources)
    sub.add_parser("fetch-assets", help="download background music").set_defaults(fn=cmd_fetch_assets)
    sub.add_parser("fetch-gameplay", help="download gameplay library").set_defaults(fn=cmd_fetch_gameplay)
    sub.add_parser("monitor", help="check uploaded videos for rejections").set_defaults(fn=cmd_monitor)
    sub.add_parser("cleanup").set_defaults(fn=cmd_cleanup)
    rc = sub.add_parser("render-clip", help="manual render test from a local file")
    rc.add_argument("--input", required=True)
    rc.add_argument("--start", type=float, default=0.0)
    rc.add_argument("--end", type=float, default=52.0)
    rc.add_argument("--heading", default="This is a test heading")
    rc.add_argument("--credit", default="Test Channel")
    rc.add_argument("--layout", default="fit_blur", choices=["fit_blur", "center_crop"])
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
