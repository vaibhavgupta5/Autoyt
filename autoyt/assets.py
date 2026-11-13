"""Music and gameplay libraries: download once, reuse every day."""
from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

import requests

from . import ytdlp
from .config import Settings
from .db import Database
from .errors import SourceRejected
from .render import probe_duration

log = logging.getLogger("autoyt.assets")

# Text only, no links: descriptions carry no URLs. The CC BY licence normally asks for a link to the licence;
# the licence name is kept, and a link can be added back here if you want the strictest attribution.
CC_CREDIT = "{title} by Kevin MacLeod. Licensed under Creative Commons: By Attribution 4.0"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# ---------------------------------------------------------------- music
def measure_lufs(path: Path) -> float | None:
    """Integrated loudness of a track, so every track can be mixed at the same level."""
    p = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    m = re.findall(r"^\s+I:\s+(-?[\d.]+) LUFS", p.stderr, re.M)
    return float(m[-1]) if m else None


def fetch_music(s: Settings) -> list[Path]:
    cfg = s.yaml_file("music.yaml") or {}
    music_dir = s.assets_dir / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    credits_path = music_dir / "credits.json"
    credits = json.loads(credits_path.read_text()) if credits_path.exists() else {}
    got: list[Path] = []
    for t in cfg.get("tracks", []):
        dest = music_dir / f"{_slug(t['title'])}.mp3"
        if not dest.exists() or dest.stat().st_size < 100_000:
            log.info("downloading music: %s", t["title"])
            tmp = dest.with_suffix(".part")
            for attempt in range(5):  # incompetech.com can be slow; retry with longer waits
                try:
                    with requests.get(t["url"], stream=True, timeout=(20, 180)) as r:
                        r.raise_for_status()
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_content(1 << 20):
                                f.write(chunk)
                    tmp.replace(dest)
                    break
                except requests.RequestException as e:
                    log.warning("music %s attempt %d failed: %s", t["title"], attempt + 1, e)
                    tmp.unlink(missing_ok=True)
                    if attempt == 4:
                        raise
                    time.sleep(5 * (attempt + 1))
        lufs = credits.get(dest.name, {}).get("lufs") or measure_lufs(dest)
        credits[dest.name] = {"title": t["title"], "credit": CC_CREDIT.format(title=t["title"]), "lufs": lufs}
        credits_path.write_text(json.dumps(credits, indent=2))
        got.append(dest)
    return got


def choose_music(s: Settings, duration: float, rng: random.Random | None = None) -> dict | None:
    rng = rng or random
    music_dir = s.assets_dir / "music"
    tracks = sorted(music_dir.glob("*.mp3")) + sorted(music_dir.glob("*.m4a")) + sorted(music_dir.glob("*.wav"))
    if not tracks:
        return None
    credits_path = music_dir / "credits.json"
    credits = json.loads(credits_path.read_text()) if credits_path.exists() else {}
    path = rng.choice(tracks)
    length = probe_duration(path)
    loop = length < duration + 3
    start = 0.0 if loop else rng.uniform(0, length - duration - 3)
    meta = credits.get(path.name, {})
    return {"path": path, "start": start, "loop": loop, "title": meta.get("title", path.stem),
            "credit": meta.get("credit", ""), "lufs": meta.get("lufs")}


# ------------------------------------------------------------- gameplay
def _detect_crop(raw: Path) -> tuple[int, int, int, int] | None:
    """Black bars around the game (a landscape video with a portrait game inside). None if no bars found."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-ss", "8", "-i", str(raw), "-t", "15",
                        "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", p.stderr)
    if not crops:
        return None
    w, h, x, y = (int(v) for v in Counter(crops).most_common(1)[0][0])
    return (w, h, x, y) if w >= 240 and h >= 240 else None


PREP_VERSION = 2  # bump to make every stored gameplay file be fetched and prepared again


def _detect_bars(raw: Path, samples: int = 6) -> tuple[int, int, int, int] | None:
    """Box (w, h, x, y) of the real picture inside black OR blurred bars. None if there are no bars.

    cropdetect only sees black. Blurred bars (a blurred copy of the game filling the frame) are not
    black but are nearly featureless, so each row/column's detail (std of horizontal/vertical
    differences), averaged over a few frames, is tiny there and large inside the game picture.
    """
    import numpy as np

    W0, H0 = _probe_size(raw)
    dur = max(1.0, probe_duration(raw))
    sw = 360
    sh = int(round(H0 * sw / W0 / 2)) * 2
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(raw), "-vf",
                        f"fps={samples}/{dur:.2f},scale={sw}:{sh},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=300)
    frame = sw * sh
    n = len(p.stdout) // frame
    if p.returncode != 0 or n == 0:
        return None
    f = np.frombuffer(p.stdout[: n * frame], dtype=np.uint8).reshape(n, sh, sw).astype(np.float32)
    # second differences: blur flattens them to ~0.6 (codec noise), real game detail stays >= 2.5
    row = np.abs(f[:, :, 2:] - 2 * f[:, :, 1:-1] + f[:, :, :-2]).mean(axis=(0, 2))
    col = np.abs(f[:, 2:, :] - 2 * f[:, 1:-1, :] + f[:, :-2, :]).mean(axis=(0, 1))
    col = np.concatenate([[col[0]], col, [col[-1]]]) if col.size == sw - 2 else col

    def span(prof) -> tuple[int, int]:
        thr = 0.3 * float(np.percentile(prof, 90))
        idx = np.where(prof > thr)[0]
        return (int(idx[0]), int(idx[-1]) + 1) if idx.size else (0, len(prof))

    y0, y1 = span(row)
    x0, x1 = span(col)
    # Bars come in pairs on ONE axis (top/bottom or left/right). Crop only the axis with the bigger bars,
    # so smooth sky or road at the other edges of the game is never mistaken for a bar.
    if 1 - (y1 - y0) / sh >= 1 - (x1 - x0) / sw:
        x0, x1 = 0, sw
    else:
        y0, y1 = 0, sh
    if y0 < 4 and y1 > sh - 4 and x0 < 4 and x1 > sw - 4:
        return None
    sx, sy = W0 / sw, H0 / sh
    x, y = int(x0 * sx) // 2 * 2, int(y0 * sy) // 2 * 2
    w, h = int((x1 - x0) * sx) // 2 * 2, int((y1 - y0) * sy) // 2 * 2
    return (w, h, x, y) if w >= 240 and h >= 240 else None


def _probe_size(path: Path) -> tuple[int, int]:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                       capture_output=True, text=True, timeout=60)
    w, h = p.stdout.strip().split("x")[:2]
    return int(w), int(h)


def gameplay_filter(w: int, h: int, W: int, B: int, focus: float = 0.68) -> str:
    """ffmpeg filter chain that fits game content of size w x h into a W x B panel without ugly stretching.

    - wide content: plain cover crop
    - portrait content that is sharp enough (upscale <= 1.6x): fill the width and keep the lower part of
      the frame, where the runner is, instead of cropping from the middle
    - low-resolution portrait content: show the whole game at panel height over a blurred copy of itself
    """
    if w / h >= (W / B) * 0.9:
        return f"scale={W}:{B}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{B}"
    up = W / w
    if up <= 1.6:
        sh = int(round(h * up / 2)) * 2
        y = int(max(0, min(sh - B, sh * focus - B / 2)))
        return f"scale={W}:{sh}:flags=lanczos,crop={W}:{B}:0:{y}"
    return (f"split=2[a][b];[a]scale={W}:{B}:force_original_aspect_ratio=increase,crop={W}:{B},"
            f"boxblur=22:2,eq=brightness=-0.1[bg];[b]scale=-2:{B}:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:0")


def prep_gameplay(s: Settings, raw: Path, dest: Path, start: float = 0.0, length: float | None = None,
                  focus: float = 0.68, min_content_side: int = 0) -> None:
    """One muted 1080x770 / 30 fps version of [start, start+length], so daily renders stay cheap."""
    lay = s.cfg["layout"]
    W, B = lay["width"], lay["bottom_height"]
    try:
        crop = _detect_bars(raw)
    except Exception as e:  # noqa: BLE001
        log.info("bar detection failed (%s); trying cropdetect", e)
        crop = None
    crop = crop or _detect_crop(raw)
    if min_content_side:
        cw, ch = (crop[0], crop[1]) if crop else _probe_size(raw)
        if min(cw, ch) < min_content_side:
            raise ValueError(f"real picture is only {cw}x{ch} inside its bars")
    w, h = (crop[0], crop[1]) if crop else _probe_size(raw)
    chain = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
    vf = chain + gameplay_filter(w, h, W, B, focus) + ",fps=30,setsar=1,format=yuv420p"
    tmp = dest.with_suffix(".part.mp4")
    cut = ["-ss", f"{start:.0f}"] + (["-t", f"{length:.0f}"] if length else [])
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *cut, "-i", str(raw), "-vf", vf, "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-movflags", "+faststart",
                    "-threads", "4", str(tmp)], check=True, timeout=1800)
    tmp.replace(dest)


def _title_ok(title: str, exclude: re.Pattern, must_match: str | None) -> bool:
    """Search results must actually be about the game (a search for Subway Surfers can return Minecraft)."""
    if exclude.search(title):
        return False
    return not must_match or bool(re.search(must_match, title, re.I))


def _load_index(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def _evaluate(s: Settings, c: dict, cache: dict) -> dict | None:
    """Look at the video's real formats. None unless a true 720p-class stream exists (short side >= 700)."""
    if c["id"] in cache:
        return cache[c["id"]]
    res = None
    try:
        info = ytdlp.video_info(s, c["id"])
        f = ytdlp.pick_format(info, int(s.cfg["source"]["max_height"]))
        if f and min(f["width"], f["height"]) >= 700:
            res = {"format_id": f["format_id"], "w": f["width"], "h": f["height"], "tbr": f.get("tbr")}
        else:
            got = f"{f['width']}x{f['height']}" if f else "nothing usable"
            log.info("skipping %s (%s): best quality is %s", c["id"], c["title"][:50], got)
    except SourceRejected as e:
        log.info("skipping %s: %s", c["id"], e)
    cache[c["id"]] = res
    return res


def _fetch_one(s: Settings, game: str, c: dict, ev: dict, clip_s: float, skip_min: float, focus: float) -> dict:
    root = s.assets_dir / "gameplay"
    gdir = root / game
    gdir.mkdir(parents=True, exist_ok=True)
    raw_dir = s.data_dir / "tmp_gameplay"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_base = raw_dir / f"{game}_{c['id']}"
    try:
        # Only `clip_seconds` of the chosen format (video only). Section mode runs at about real time, fine
        # for one minute, and no exact cut is needed because prep re-encodes anyway.
        start = min(skip_min * 60, max(0.0, c["duration"] - clip_s - 10))
        p = ytdlp._run(s, ["-f", ev["format_id"], "--no-playlist",
                           "--download-sections", f"*{start:.0f}-{start + clip_s + 5:.0f}",
                           "-o", str(raw_base) + ".%(ext)s",
                           f"https://www.youtube.com/watch?v={c['id']}"], timeout=1200)
        raws = [f for f in raw_dir.glob(raw_base.name + ".*") if f.suffix not in (".part", ".ytdl")]
        if p.returncode != 0 or not raws:
            raise RuntimeError(ytdlp._fail_text(p))
        dest = gdir / f"{c['id']}.mp4"
        prep_gameplay(s, raws[0], dest, start=0.0, length=clip_s, focus=focus, min_content_side=600)
        dur = probe_duration(dest)
        expected = min(clip_s, c["duration"] - start)  # a 21 s Short legitimately gives 21 s
        if dur < expected - 2:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"only {dur:.0f}s of footage came back, expected about {expected:.0f}s")
        return {"game": game, "file": f"{game}/{dest.name}", "video_id": c["id"], "title": c["title"],
                "prep": PREP_VERSION,
                "duration": dur, "source_size": f"{ev['w']}x{ev['h']}",
                "source_url": f"https://www.youtube.com/watch?v={c['id']}"}
    finally:
        for f in raw_dir.glob(raw_base.name + ".*"):
            f.unlink(missing_ok=True)


def fetch_gameplay(s: Settings) -> list[dict]:
    """Build the gameplay library. Only true 720p-class videos are accepted, portrait ones first."""
    cfg = s.yaml_file("gameplay.yaml") or {}
    clip_s = float(cfg.get("clip_seconds", 60))
    skip_min = float(cfg.get("skip_minutes", 5))
    focus = float(cfg.get("focus", 0.68))
    min_dur = float(cfg.get("min_source_minutes", 3)) * 60
    excl = re.compile(cfg.get("exclude_title", r"react|reaction|tutorial|how to|tips|tricks|mod apk|hack|glitch|review"), re.I)
    root = s.assets_dir / "gameplay"
    index_path = root / "index.json"
    index = _load_index(index_path)
    for game, gcfg in (cfg.get("games") or {}).items():
        have = [e for e in index if e["game"] == game and (root / e["file"]).exists()]
        need = int(gcfg.get("count", 3)) - len(have)
        if need <= 0:
            log.info("%s: already have %d videos", game, len(have))
            continue
        known = {e["video_id"] for e in have}
        cands: list[dict] = []
        for u in gcfg.get("urls", []):
            for e in ytdlp.list_flat(s, u, 1):
                cands.append({"id": e["id"], "title": e.get("title", ""), "duration": e.get("duration") or 600})
        for q in gcfg.get("queries", []):
            for e in ytdlp.list_flat(s, f"ytsearch20:{q}", 20):
                dur = e.get("duration") or 0
                if min_dur <= dur <= 3 * 3600 and _title_ok(e.get("title", ""), excl, gcfg.get("title_must_match")):
                    cands.append({"id": e["id"], "title": e.get("title", ""), "duration": dur})
        cache: dict = {}
        tried = 0
        for portrait_only in (True, False):  # portrait videos are natively sharp, so they come first
            for c in cands:
                if need <= 0 or tried >= 25:
                    break
                if c["id"] in known:
                    continue
                ev = _evaluate(s, c, cache)
                if not ev or (portrait_only and ev["h"] <= ev["w"]):
                    continue
                known.add(c["id"])
                tried += 1
                try:
                    log.info("downloading %s: %s (%s, %dx%d)", game, c["title"][:60], c["id"], ev["w"], ev["h"])
                    index.append(_fetch_one(s, game, c, ev, clip_s, skip_min, focus))
                    index_path.write_text(json.dumps(index, indent=2))
                    need -= 1
                except Exception as e:  # noqa: BLE001
                    log.warning("gameplay %s failed: %s", c["id"], e)
        if need > 0:
            log.warning("%s: still missing %d videos; re-run later or add `urls:` in config/gameplay.yaml", game, need)
    return index


def ensure_library(s: Settings) -> int:
    """Daily upkeep: drop files prepared by an older version (bar-crop fix), then fill any gaps.

    Each file is one minute of video, so a full rebuild is about 6 small downloads. Returns files fetched.
    """
    if not (s.yaml_file("gameplay.yaml") or {}).get("games"):
        return 0
    root = s.assets_dir / "gameplay"
    index_path = root / "index.json"
    index = _load_index(index_path)
    keep = [e for e in index if int(e.get("prep", 1)) >= PREP_VERSION and (root / e["file"]).exists()]
    stale = [e for e in index if e not in keep and (root / e["file"]).exists()]
    if stale:
        log.info("gameplay library: re-preparing %d older files", len(stale))
        # Old files are renamed aside, not deleted, until their replacements exist: a failed download
        # (bot check, network) must never leave the podcast clips without any gameplay.
        for e in stale:
            (root / e["file"]).rename((root / e["file"]).with_suffix(".old.mp4"))
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(keep, indent=2))
    before = len(keep)
    try:
        new = fetch_gameplay(s)
    finally:
        cur = _load_index(index_path)
        games = {e["game"] for e in cur}
        for e in stale:
            old = (root / e["file"]).with_suffix(".old.mp4")
            if e["game"] in games:
                old.unlink(missing_ok=True)
            elif old.exists():
                old.rename(root / e["file"])
                cur.append(e)
        index_path.write_text(json.dumps(cur, indent=2))
    return max(0, len(new) - before)


def choose_gameplay(s: Settings, db: Database | None, duration: float,
                    rng: random.Random | None = None) -> dict | None:
    rng = rng or random
    root = s.assets_dir / "gameplay"
    index = [e for e in _load_index(root / "index.json") if (root / e["file"]).exists()]
    if not index:
        return None
    entry = rng.choice(index)
    if entry["duration"] < duration + 0.5:  # short source (e.g. a 21 s Short): render loops it
        return {"path": root / entry["file"], "start": 0.0, "game": entry["game"], "loop": True}
    limit = max(0.0, entry["duration"] - duration - 0.3)  # a 60 s file leaves a few seconds of wiggle
    recent = db.recent_gameplay(entry["file"]) if db else []
    start = rng.uniform(0, limit)
    for _ in range(12):
        cand = rng.uniform(0, limit)
        if all(abs(cand - r) > duration for r in recent):
            start = cand
            break
    if db:
        db.add_gameplay_usage(entry["file"], start)
    return {"path": root / entry["file"], "start": start, "game": entry["game"]}
