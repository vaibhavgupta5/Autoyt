"""Quality checks on a rendered clip before it may be uploaded."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def check_clip(path: Path, cfg: dict, layout: dict) -> list[str]:
    """Return a list of problems. Empty list means the clip passes."""
    problems: list[str] = []
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                        str(path)], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return [f"unreadable file: {p.stderr[-200:]}"]
    info = json.loads(p.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not video:
        return ["no video stream"]
    if (video["width"], video["height"]) != (layout["width"], layout["height"]):
        problems.append(f"size {video['width']}x{video['height']}")
    duration = float(info["format"]["duration"])
    if not (cfg["min_seconds"] - 0.6 <= duration <= cfg["max_seconds"] + 0.6):
        problems.append(f"duration {duration:.1f}s")
    if not audio:
        problems.append("no audio stream")
        return problems

    q = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                        "-af", "volumedetect", "-vf", "blackdetect=d=1.5:pix_th=0.08", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", q.stderr)
    if m and float(m.group(1)) < -45:
        problems.append(f"nearly silent (mean {m.group(1)} dB)")
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", q.stderr))
    if black >= 3.0:
        problems.append(f"{black:.1f}s of black frames")
    return problems
