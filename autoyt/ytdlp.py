"""Thin wrapper around the yt-dlp CLI (public videos only: no cookies, no login)."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .config import Settings
from .errors import SourceRejected

log = logging.getLogger("autoyt.ytdlp")

COMMON = ["--no-warnings", "--retries", "10", "--fragment-retries", "10", "--socket-timeout", "30"]


def _run(s: Settings, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    cmd = ["yt-dlp", *COMMON, *s.ytdlp_args, *args]
    log.debug("yt-dlp %s", " ".join(args))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def pick_format(info: dict, cap: int = 720) -> dict | None:
    """Best video-only format whose SHORT side is <= cap.

    A height cap alone is wrong for portrait video: a 720p portrait video is 720 wide and 1280 tall, so
    `height<=720` would silently fall back to its 360p version.
    """
    best = None
    for f in info.get("formats") or []:
        w, h = f.get("width"), f.get("height")
        vcodec = str(f.get("vcodec") or "none")
        if not w or not h or vcodec == "none":
            continue
        short = min(w, h)
        if short > cap:
            continue
        key = (short, vcodec.startswith("avc"), f.get("tbr") or 0)
        if best is None or key > best[0]:
            best = (key, f)
    return best[1] if best else None


def video_format(s: Settings, audio: bool) -> str:
    """yt-dlp format string capped at the configured height (default 720p)."""
    h = int(s.cfg["source"]["max_height"])
    if audio:
        return f"bv*[height<={h}]+ba/b[height<={h}]/w"
    return f"bv*[height<={h}]/b[height<={h}]/w"


BOT_HINT = (" [YouTube is rate-limiting this IP ('confirm you're not a bot'). It usually clears within hours; "
            "the run retries by itself. If it keeps happening, see README > Troubleshooting.]")


def _fail_text(p: subprocess.CompletedProcess) -> str:
    text = (p.stderr or p.stdout or "").strip()[-600:]
    if "not a bot" in text:
        text += BOT_HINT
    return text


def list_flat(s: Settings, url: str, limit: int) -> list[dict]:
    """Flat listing of a channel tab. Returns [] if the tab does not exist."""
    p = _run(s, ["--flat-playlist", "--dump-json", "--playlist-end", str(limit), url], timeout=900)
    rows = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows and p.returncode != 0:
        log.warning("listing failed for %s: %s", url, _fail_text(p))
    return rows


def video_info(s: Settings, video_id: str) -> dict:
    p = _run(s, ["--dump-single-json", "--no-playlist", "--skip-download",
                 f"https://www.youtube.com/watch?v={video_id}"], timeout=180)
    if p.returncode != 0 or not p.stdout.strip():
        raise SourceRejected(f"cannot read video {video_id}: {_fail_text(p)}")
    return json.loads(p.stdout)


def download_captions(s: Settings, video_id: str, dest_dir: Path) -> Path | None:
    """YouTube's auto-generated English captions as json3 (word-level timing). None if there are none."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("caps.*"):
        old.unlink()
    p = _run(s, ["--skip-download", "--write-auto-subs", "--sub-langs", "en-orig,en", "--sub-format", "json3",
                 "-o", str(dest_dir / "caps.%(ext)s"), f"https://www.youtube.com/watch?v={video_id}"], timeout=240)
    files = sorted(dest_dir.glob("caps.*.json3"))
    if not files:
        if p.returncode != 0:
            log.info("no captions for %s: %s", video_id, _fail_text(p)[-200:])
        return None
    return next((f for f in files if "en-orig" in f.name), files[0])


def download_audio(s: Settings, video_id: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("audio.*"):
        old.unlink()
    p = _run(s, ["-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist", "-N", "4",
                 "-o", str(dest_dir / "audio.%(ext)s"),
                 f"https://www.youtube.com/watch?v={video_id}"], timeout=3600)
    files = sorted(dest_dir.glob("audio.*"))
    files = [f for f in files if f.suffix not in (".part", ".ytdl")]
    if p.returncode != 0 or not files:
        raise RuntimeError(f"audio download failed: {_fail_text(p)}")
    return files[0]


def download_section(s: Settings, video_id: str, start: float, end: float, out_file: Path) -> Path:
    """Download only [start, end] seconds at up to max_height (720p), cut exactly at the boundaries."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    for old in out_file.parent.glob(out_file.stem + ".*"):
        old.unlink()
    p = _run(s, [
        "-f", video_format(s, audio=True),
        "--merge-output-format", "mp4",
        "--download-sections", f"*{start:.2f}-{end:.2f}",
        "--force-keyframes-at-cuts",
        "--no-playlist", "-N", "4",
        "-o", str(out_file.with_suffix(".%(ext)s")),
        f"https://www.youtube.com/watch?v={video_id}",
    ], timeout=1800)
    candidates = [f for f in out_file.parent.glob(out_file.stem + ".*") if f.suffix not in (".part", ".ytdl")]
    if p.returncode != 0 or not candidates:
        raise RuntimeError(f"section download failed: {_fail_text(p)}")
    got = candidates[0]
    if got != out_file:
        got.rename(out_file)
    return out_file
