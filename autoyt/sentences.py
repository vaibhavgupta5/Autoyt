"""Group word timestamps into numbered sentences."""
from __future__ import annotations

import re

_END = re.compile(r"[.!?]+[\"')\]]*$")


def build_sentences(words: list[dict], gap_split: float = 1.2, max_words: int = 45,
                    punctuated: bool = True) -> list[dict]:
    """Auto-captions have no punctuation, so for them sentences are cut at short pauses (0.45 s) instead.

    Measured on a real 96-minute podcast: about three quarters of units end on a genuine pause and the
    median unit is about a spoken sentence long.
    """
    if not punctuated:
        gap_split, max_words = 0.45, 30
    """Each sentence: {i, start, end, text, w0, w1} where w0/w1 index into `words` (inclusive)."""
    sentences: list[dict] = []
    cur: list[int] = []

    def flush() -> None:
        if not cur:
            return
        ws = [words[k] for k in cur]
        sentences.append({
            "i": len(sentences), "start": ws[0]["s"], "end": ws[-1]["e"],
            "text": " ".join(w["w"] for w in ws), "w0": cur[0], "w1": cur[-1],
        })
        cur.clear()

    for k, w in enumerate(words):
        if cur and w["s"] - words[cur[-1]]["e"] > gap_split:
            flush()
        cur.append(k)
        if _END.search(w["w"]) or len(cur) >= max_words:
            flush()
    flush()
    return sentences


def fmt_time(t: float) -> str:
    t = int(t)
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
