"""Gameplay segment choice without a transcript: loud, lively moments of a gameplay video.

Gameplay has no speech worth transcribing, so segments are picked by audio energy: a loud,
changing passage is almost always an action moment (crash, explosion, chase), while a quiet flat
passage is a menu, a loading screen or a pause. The original audio is kept, so there is no extra
music on these clips.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("autoyt.segments")

# On-screen hooks and titles. `{game}` is the source's `game_label` ("GTA 5" before the GTA VI launch,
# "GTA 6" after), so GTA V footage is never sold as GTA 6.
HOOKS = (
    "Wait for the end",
    "{game} players know this pain",
    "The craziest {game} moment",
    "Nobody expected this in {game}",
    "This {game} clip is unreal",
    "POV: one normal day in {game}",
    "{game} physics never miss",
    "Only in {game}",
)
QUESTIONS = (
    "Would you have made it out of that one?",
    "What's the craziest thing that happened to you in {game}?",
    "Rate this moment out of 10",
    "Clean or lucky?",
    "What would you have done here?",
)
GAME_TAGS = {
    "GTA 5": ["gta 5", "gta v", "gta online", "gta 5 gameplay", "grand theft auto 5"],
    "GTA 6": ["gta 6", "gta vi", "gta 6 gameplay", "grand theft auto 6", "gta 6 clips"],
}


def gameplay_meta(game: str, rng: random.Random) -> dict:
    """Heading, title, tags and a comment question for one gameplay clip."""
    hook = rng.choice(HOOKS).format(game=game)
    title = f"{hook} ({game} gameplay)" if game not in hook else hook
    return {"heading": hook, "title": title[:90], "description": f"{game} gameplay moment.",
            "context_line": "", "category": None, "tags": GAME_TAGS.get(game, [game.lower()]),
            "comment_question": rng.choice(QUESTIONS).format(game=game)}


def build_windows(energy: list[float], hop: float, total: float, n: int, clip_min: float,
                  clip_max: float, skip_s: float, rng: random.Random) -> list[dict]:
    """Pick up to ``n`` non-overlapping windows centred on the loudest, liveliest moments."""
    if not energy or n <= 0 or total <= 0:
        return []
    k = max(1, int(round(5.0 / max(hop, 0.1))))          # ~5 s smoothing
    smooth: list[float] = []
    for i in range(len(energy)):
        lo = max(0, i - k + 1)
        chunk = energy[lo:i + 1]
        smooth.append(sum(chunk) / len(chunk))
    lo_i, hi_i = int(skip_s / hop), max(int(skip_s / hop) + 1, int(total / hop))
    usable = smooth[lo_i:hi_i] or smooth   # loudness bar from the usable part only (not intro/outro)
    peak = max(usable) or 1.0
    mean = sum(usable) / len(usable)
    cands: list[dict] = []
    for i, v in enumerate(smooth):
        t = i * hop
        if t < skip_s or t > total:
            continue
        left = smooth[i - 1] if i else -1.0
        right = smooth[i + 1] if i + 1 < len(smooth) else -1.0
        if v < left or v < right or v < mean:
            continue
        dur = min(clip_max, max(clip_min, rng.uniform(clip_min, clip_max)))
        start = max(skip_s, min(total - dur, t - dur / 2))
        if start < skip_s - 0.01 or start + dur > total + 0.01:
            continue
        cands.append({"start": round(start, 2), "end": round(start + dur, 2),
                      "duration": round(dur, 2), "score": round(v / peak, 4)})
    cands.sort(key=lambda c: -c["score"])
    chosen: list[dict] = []
    for c in cands:
        if len(chosen) >= n:
            break
        if any(c["start"] < o["end"] and o["start"] < c["end"] for o in chosen):
            continue
        chosen.append(c)
    return chosen


def energy_profile(s: Settings, audio: Path, hop: float = 1.0) -> list[float]:
    """Per-``hop`` RMS loudness of a decoded audio file (mono, 16 kHz)."""
    import numpy as np

    from .transcribe import load_wav

    samples = load_wav(audio)
    step = max(1, int(16000 * hop))
    out: list[float] = []
    for i in range(0, max(0, samples.size - step + 1), step):
        chunk = samples[i:i + step]
        out.append(float(np.sqrt(np.mean(np.square(chunk)))))
    return out


def select_segments(s: Settings, db: Database, video_id: str, duration: float, n: int,
                    clip: dict, rng: random.Random) -> list[dict]:
    """Download the video's audio, measure energy and return the best gameplay windows."""
    from . import transcribe, ytdlp

    work = s.work_dir / "segments" / video_id
    work.mkdir(parents=True, exist_ok=True)
    audio = ytdlp.download_audio(s, video_id, work)
    try:
        chunks = transcribe.split_audio(audio, work)
        energy: list[float] = []
        for c in chunks:  # 30-minute pieces, in order: the profile covers the whole video
            energy += energy_profile(s, c)
        # End cards and outro music are loud too: never pick from the last 8% (at least 45 s) of the video.
        usable = max(0.0, duration - max(45.0, duration * 0.08))
        windows = build_windows(energy, hop=1.0, total=usable, n=n,
                               clip_min=clip["min_seconds"], clip_max=clip["max_seconds"],
                               skip_s=min(60.0, duration * 0.05), rng=rng)
    finally:
        audio.unlink(missing_ok=True)
        for f in work.glob("*.wav"):
            f.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass
    log.info("%s: %d gameplay segments from %.0f s of audio", video_id, len(windows), duration)
    return windows
