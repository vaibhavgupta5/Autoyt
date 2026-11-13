"""The code-side referee. The AI proposes; this decides what is allowed."""
from __future__ import annotations

import logging
import re

log = logging.getLogger("autoyt.validate")
CATEGORIES = ("funny", "informative", "deep")


_LINK = re.compile(r"https?://[^\s)\]]+|www\.[^\s)\]]+|\b[\w-]+\.(?:com|net|org|io|tv|gg|co|me|ly|be)\b[^\s)\]]*", re.I)


def strip_links(text: str) -> str:
    """Descriptions carry no links: remove URLs and bare domains the model may have written."""
    t = _LINK.sub("", str(text or ""))
    t = re.sub(r"\(\s*\)|\[\s*\]", "", t)           # brackets left empty by the removal
    return re.sub(r"\s+", " ", t).strip()


def clean_tags(raw, limit: int = 8) -> list[str]:
    """Search keywords: lowercase, no hashtags, no links, 2-30 characters, unique."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str):  # a null or a number from the model is not a tag
            continue
        t = strip_links(t).lower().lstrip("#").strip()
        t = re.sub(r"[^a-z0-9 '&-]", "", t).strip()
        if 2 <= len(t) <= 30 and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _clean_heading(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'“”‘’").strip()
    words = text.split(" ")
    if len(words) > 8:
        text = " ".join(words[:8])
    return text.rstrip(",;:-– ")


def _has_banned(text: str, banned: list[str]) -> bool:
    low = text.lower()
    return any(b.lower() in low for b in banned)


def fit_window(sentences: list[dict], words: list[dict], s: int, e: int, cfg: dict) -> dict | None:
    """Find start/end so the padded clip lasts between min_seconds and max_seconds.

    Prefers ending on a sentence boundary. Falls back to a word boundary.
    """
    lo, hi = cfg["min_seconds"], cfg["max_seconds"]
    ps, pe = cfg["pad_start"], cfg["pad_end"]

    def dur(st: float, en: float) -> float:
        return (en + pe) - (st - ps)

    last_sentence = len(sentences) - 1
    for ds in (0, -1, 1, -2, 2):  # try nudging the start by whole sentences
        si = s + ds
        if not (0 <= si <= last_sentence):
            continue
        st = sentences[si]["start"]
        options = [k for k in range(si, last_sentence + 1) if lo <= dur(st, sentences[k]["end"]) <= hi]
        if options:
            k = min(options, key=lambda x: abs(x - e))
            return {"w0": sentences[si]["w0"], "w1": sentences[k]["w1"], "clean": True}
        w0 = sentences[si]["w0"]
        best, best_key = None, None
        for wi in range(w0, len(words)):
            d = dur(st, words[wi]["e"])
            if d > hi:
                break
            if d < lo:
                continue
            punct = bool(re.search(r"[.!?,]$", words[wi]["w"]))
            key = (0 if punct else 1, abs(d - (lo + hi) / 2))
            if best_key is None or key < best_key:
                best, best_key = wi, key
        if best is not None:
            return {"w0": w0, "w1": best, "clean": best_key[0] == 0}
    return None


def build_clip(sentences, words, cand: dict, cfg: dict, banned: list[str]) -> dict | None:
    try:
        s, e = int(cand["start_sentence"]), int(cand["end_sentence"])
    except (KeyError, TypeError, ValueError):
        return None
    if s > e:
        s, e = e, s
    if not (0 <= s < len(sentences) and 0 <= e < len(sentences)):
        return None
    category = str(cand.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        return None
    heading = _clean_heading(strip_links(cand.get("heading")))
    if len(heading) < 3:
        return None
    title = strip_links(cand.get("title") or heading)[:90] or heading
    description = strip_links(cand.get("description"))[:300]
    context = strip_links(cand.get("context_line"))[:200]
    tags = clean_tags(cand.get("tags"))

    fit = fit_window(sentences, words, s, e, cfg)
    if not fit:
        return None
    w0, w1 = fit["w0"], fit["w1"]
    seg = words[w0:w1 + 1]
    if len(seg) < cfg["min_words"]:
        return None
    if any(seg[i + 1]["s"] - seg[i]["e"] > cfg["max_gap_seconds"] for i in range(len(seg) - 1)):
        return None
    text = " ".join(w["w"] for w in seg)
    if _has_banned(" ".join([heading, title, description, context, text]), banned):
        return None
    start = max(0.0, seg[0]["s"] - cfg["pad_start"])
    end = seg[-1]["e"] + cfg["pad_end"]
    try:
        hook = float(cand.get("hook_score", 5))
        score = hook + float(cand.get("standalone_score", 5))
    except (TypeError, ValueError):
        hook, score = 5.0, 10.0
    total = float(cfg.get("source_duration") or 0)
    if total and hook < 9:  # intros and outros: greetings, sponsor reads, "subscribe" - rarely a hook
        if start < float(cfg.get("skip_intro_seconds", 0)) or end > total - float(cfg.get("skip_outro_seconds", 0)):
            return None
    try:  # a loopable ending only breaks ties, it never outranks a better clip
        score += max(0.0, min(10.0, float(cand.get("loop_score") or 0))) * 0.05
    except (TypeError, ValueError):
        pass
    if score < cfg.get("min_score", 0):  # the model's own honesty check, enforced in code
        return None
    if not fit["clean"]:
        score -= 3
    return {"start": round(start, 2), "end": round(end, 2), "duration": round(end - start, 2),
            "heading": heading, "title": title, "description": description, "context_line": context,
            "tags": tags, "category": category, "score": score, "text": text, "w0": w0, "w1": w1,
            "comment_question": strip_links(cand.get("comment_question"))[:200]}


def pick_best(clips: list[dict], n: int, cfg: dict) -> list[dict]:
    """Greedy by score: no overlaps, spread across the video, mix categories, no repeated headings."""
    gap = cfg["min_gap_between_clips"]
    per_bucket = cfg["max_clips_per_15min"]

    def conflicts(c: dict, chosen: list[dict]) -> bool:
        for o in chosen:
            if c["start"] < o["end"] + gap and o["start"] < c["end"] + gap:
                return True
        return False

    def bucket_full(c: dict, chosen: list[dict]) -> bool:
        b = int(c["start"] // 900)
        return sum(1 for o in chosen if int(o["start"] // 900) == b) >= per_bucket

    ranked = sorted(clips, key=lambda c: -c["score"])
    seen_headings: set[str] = set()
    chosen: list[dict] = []
    for relax_buckets in (False, True):
        for c in ranked:
            if len(chosen) >= n:
                break
            if c in chosen or c["heading"].lower() in seen_headings or conflicts(c, chosen):
                continue
            if not relax_buckets and bucket_full(c, chosen):
                continue
            chosen.append(c)
            seen_headings.add(c["heading"].lower())

    # make sure at least two categories appear when the pool allows it
    if chosen and len({c["category"] for c in chosen}) < 2:
        other = next((c for c in ranked if c["category"] != chosen[0]["category"]
                      and c["heading"].lower() not in seen_headings
                      and not conflicts(c, chosen[:-1])), None)
        if other and len(chosen) == n:
            chosen[-1] = other
        elif other:
            chosen.append(other)

    # order: best first, but avoid the same category twice in a row
    remaining = sorted(chosen, key=lambda c: -c["score"])
    ordered: list[dict] = []
    while remaining:
        prev = ordered[-1]["category"] if ordered else None
        nxt = next((c for c in remaining if c["category"] != prev), remaining[0])
        ordered.append(nxt)
        remaining.remove(nxt)
    return ordered


def validate_and_pick(sentences: list[dict], words: list[dict], candidates: list[dict],
                      n: int, cfg: dict, banned: list[str]) -> list[dict]:
    built = []
    for cand in candidates:
        c = build_clip(sentences, words, cand, cfg, banned)
        if c:
            built.append(c)
    log.info("%d of %d candidates passed validation", len(built), len(candidates))
    return pick_best(built, n, cfg)


def music_ratio(media) -> float:
    """Share (0..1) of a clip's 1-second frames that sound like music or a steady tone rather than speech.

    Speech is bursty: syllables and pauses make its loudness swing several times a second, so the
    coefficient of variation of 50 ms loudness inside one second is high (typically > 0.5). Music beds,
    trailer scores and tones are steady (< 0.3). Silent frames are ignored. A cheap heuristic: no model,
    one ffmpeg decode of the 25-45 s slice.
    """
    import subprocess
    import tempfile
    from pathlib import Path

    import numpy as np

    from .transcribe import load_wav

    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "m.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(wav)], check=True, timeout=120)
        x = load_wav(wav)
    sub, per = 800, 20  # 50 ms pieces, 20 per second
    n = x.size // (sub * per)
    if n == 0:
        return 0.0
    rms = np.sqrt(np.mean(np.square(x[: n * sub * per].reshape(n, per, sub)), axis=2))
    loud = rms.mean(axis=1) > 0.01
    if not loud.any():
        return 0.0
    cv = rms.std(axis=1) / np.maximum(rms.mean(axis=1), 1e-6)
    return float(((cv < 0.3) & loud).sum() / loud.sum())
