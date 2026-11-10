"""YouTube's own auto-captions as a fast transcript (seconds instead of 10-30 minutes of Whisper).

They are good enough to FIND clips. The burned-in captions are redone with Whisper on just the chosen
55-second slices, so on-screen text stays accurate.
"""
from __future__ import annotations

import json
from pathlib import Path


def parse_json3(path: Path) -> list[dict]:
    """json3 events -> [{'w', 's', 'e'}]. Auto-captions carry per-word offsets (tOffsetMs)."""
    data = json.loads(Path(path).read_text())
    raw: list[dict] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        t0 = ev.get("tStartMs", 0)
        ev_end = (t0 + ev.get("dDurationMs", 0)) / 1000
        for sg in segs:
            tokens = [t for t in (sg.get("utf8") or "").replace("\n", " ").split()
                      if not (t.startswith("[") and t.endswith("]")) and set(t) != {">"}]  # [Music], [Applause], >>
            if not tokens:
                continue
            start = (t0 + sg.get("tOffsetMs", 0)) / 1000
            for k, tok in enumerate(tokens):  # normally one word per seg; spread if several
                raw.append({"w": tok, "s": start + k * 0.25, "_ev_end": ev_end})
    raw.sort(key=lambda w: w["s"])

    words: list[dict] = []
    for w in raw:  # rolling captions can repeat a word
        if words and words[-1]["w"] == w["w"] and abs(words[-1]["s"] - w["s"]) < 0.2:
            continue
        words.append(w)

    for i, w in enumerate(words):
        # YouTube gives only word START times. Estimate how long the word is spoken (about 0.12 s plus
        # 0.055 s a letter) so the silence after it shows up as a gap; that is how pauses, and with them
        # sentence ends, are found. Never longer than the time until the next word.
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["_ev_end"]
        spoken = 0.12 + 0.055 * len(w["w"])
        w["e"] = round(max(min(nxt, w["s"] + spoken), w["s"] + 0.05), 3)
        w["s"] = round(w["s"], 3)
        del w["_ev_end"]
    return words


def build_transcript(path: Path) -> dict:
    words = parse_json3(path)
    return {"language": "en", "source": "captions", "words": words,
            "duration": max((w["e"] for w in words), default=0.0)}
