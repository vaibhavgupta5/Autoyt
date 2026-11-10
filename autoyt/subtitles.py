"""ASS subtitle file: heading box (from frame one) and word-by-word highlighted captions."""
from __future__ import annotations

import re


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    sec, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def wrap_heading(text: str, max_chars: int = 17, max_lines: int = 3) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(",.;:") + "..."
    return lines or [""]


def group_words(words: list[dict], max_words: int = 3, max_chars: int = 18, gap: float = 0.6) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            too_long = len(" ".join(x["w"] for x in cur + [w])) > max_chars
            ended = bool(re.search(r"[.!?]$", cur[-1]["w"]))
            if len(cur) >= max_words or too_long or ended or w["s"] - cur[-1]["e"] > gap:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def _display(word: str) -> str:
    return _esc(re.sub(r"[,.;:]+$", "", word)).upper()


def build_ass(clip_words: list[dict], heading: str, credit: str, duration: float, layout: dict) -> str:
    """clip_words use times relative to the clip start."""
    W, H = layout["width"], layout["height"]
    top_h = layout["top_height"]
    font = layout["font"]
    hs, ss = layout["heading_size"], layout["subtitle_size"]
    sub_y = top_h - 40  # sits just above the seam between the two videos

    lines = wrap_heading(_esc(heading))
    line_h = int(hs * 1.22)
    box_h = len(lines) * line_h + 56
    box_top, box_x, box_w = 140, 60, W - 120
    heading_text = "\\N".join(lines)
    credit_y = box_top + box_h + 14

    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{font},{ss},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1",
        f"Style: Head,{font},{hs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,70,70,0,1",
        f"Style: Credit,{font},34,&H00E6E6E6,&H00E6E6E6,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,7,64,64,0,1",
        f"Style: Box,{font},20,&H00000000,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    end = _ts(duration)
    out.append(f"Dialogue: 0,{_ts(0)},{end},Box,,0,0,0,,"
               f"{{\\an7\\pos({box_x},{box_top})\\1c&H000000&\\1a&H55&\\bord0\\shad0\\p1}}"
               f"m 0 0 l {box_w} 0 {box_w} {box_h} 0 {box_h}{{\\p0}}")
    out.append(f"Dialogue: 1,{_ts(0)},{end},Head,,0,0,0,,"
               f"{{\\an5\\pos({W // 2},{box_top + box_h // 2})}}{heading_text}")
    if credit:
        out.append(f"Dialogue: 1,{_ts(0)},{end},Credit,,0,0,0,,"
                   f"{{\\an7\\pos({box_x + 6},{credit_y})\\1a&H40&}}via {_esc(credit)}")

    groups = group_words(clip_words)
    for gi, g in enumerate(groups):
        group_end = g[-1]["e"] + 0.12
        if gi + 1 < len(groups):
            group_end = min(group_end, groups[gi + 1][0]["s"])
        for wi, w in enumerate(g):
            start = w["s"]
            stop = g[wi + 1]["s"] if wi + 1 < len(g) else group_end
            if stop <= start:
                stop = start + 0.08
            parts = []
            for wj, x in enumerate(g):
                t = _display(x["w"])
                parts.append(f"{{\\1c&H00E5FF&}}{t}{{\\1c&HFFFFFF&}}" if wj == wi else t)
            out.append(f"Dialogue: 2,{_ts(start)},{_ts(min(stop, duration))},Sub,,0,0,0,,"
                       f"{{\\an5\\pos({W // 2},{sub_y})}}{' '.join(parts)}")
    return "\n".join(out) + "\n"


def clip_relative_words(words: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    dur = clip_end - clip_start
    out = []
    for w in words:
        if w["e"] <= clip_start or w["s"] >= clip_end:
            continue
        out.append({"w": w["w"], "s": max(0.0, w["s"] - clip_start), "e": min(dur, w["e"] - clip_start)})
    return out
