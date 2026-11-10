"""ffmpeg: split-screen vertical clip with captions, heading, ducked music and gameplay."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import Settings

log = logging.getLogger("autoyt.render")


def probe_duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                        "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=60)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def _top_filter(layout_name: str, layout: dict, crop: list[float] | None, zoom: float) -> str:
    W, T = layout["width"], layout["top_height"]
    fps = layout["fps"]
    tail = f"setsar=1,fps={fps},format=yuv420p"
    if layout_name == "center_crop":
        return (f"[0:v]crop=w='min(iw,ih*{W}/{T})':h='min(ih,iw*{T}/{W})',scale={W}:{T}:flags=lanczos,{tail}[top]")
    if layout_name == "custom" and crop:
        x, y, w, h = crop
        return (f"[0:v]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={W}:{T}:force_original_aspect_ratio=increase,"
                f"crop={W}:{T},{tail}[top]")
    # fit_blur (default for multi-person podcasts): whole frame over a blurred copy of itself
    zoom_f = f"crop=iw/{zoom}:ih/{zoom}," if zoom and zoom > 1 else ""
    bw, bh = W // 4, (T // 4) // 2 * 2  # blur a quarter-size copy then scale up: same look, far less CPU
    return (f"[0:v]{zoom_f}split=2[a][b];"
            f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=7:2,"
            f"scale={W}:{T}:flags=bilinear,eq=brightness=-0.12[bg];"
            f"[b]scale={W}:{T}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}[top]")


def build_command(s: Settings, *, section: Path, ass_name: str, out_tmp: Path, duration: float,
                  gameplay: dict | None, music: dict | None, layout_name: str = "fit_blur",
                  crop: list[float] | None = None, zoom: float = 1.0) -> list[str]:
    lay = s.cfg["layout"]
    W, B = lay["width"], lay["bottom_height"]
    fps = lay["fps"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(section)]
    if gameplay:
        loop = ["-stream_loop", "-1"] if gameplay.get("loop") else []  # file shorter than the clip: repeat it
        cmd += [*loop, "-ss", f"{gameplay['start']:.2f}", "-t", f"{duration:.2f}", "-i", str(gameplay["path"])]
    else:
        cmd += ["-f", "lavfi", "-i", f"color=c=0x1c1c24:s={W}x{B}:r={fps}:d={duration:.2f}"]
    if music:
        if music.get("loop"):
            cmd += ["-stream_loop", "-1"]
        cmd += ["-ss", f"{music['start']:.2f}", "-i", str(music["path"])]

    graph = [
        _top_filter(layout_name, lay, crop, zoom),
        f"[1:v]fps={fps},scale={W}:{B}:force_original_aspect_ratio=increase,crop={W}:{B},setsar=1,format=yuv420p[bot]",
        "[top][bot]vstack=inputs=2[st]",
        f"[st]ass={ass_name}[v]",
    ]
    lufs = s.cfg["audio"]["speech_lufs"]
    base = f"[0:a]aresample=48000,loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000"
    if music:
        # Bring every track to the same loudness (music_lufs), then the sidechain ducks it further while
        # someone is talking. Falls back to a fixed gain for a track whose loudness was never measured.
        audio_cfg = s.cfg["audio"]
        lufs = music.get("lufs")
        gain_db = (audio_cfg["music_lufs"] - lufs) if lufs is not None else audio_cfg["music_db"]
        vol = 10 ** (gain_db / 20)
        fade_out = max(0.0, duration - 2.5)
        graph += [
            f"{base},asplit=2[sp][sc]",
            f"[2:a]aresample=48000,volume={vol:.4f},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out:.2f}:d=2.5[mu0]",
            "[mu0][sc]sidechaincompress=threshold=0.04:ratio=6:attack=15:release=350[mu]",
            "[sp][mu]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[a]",
        ]
    else:
        graph.append(f"{base},alimiter=limit=0.95[a]")

    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", lay["preset"], "-crf", str(lay["crf"]),
            "-maxrate", "6M", "-bufsize", "12M",  # high-motion gameplay otherwise hits 8 Mbps (50 MB a clip)
            "-pix_fmt", "yuv420p",
            "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
            "-threads", str(max(2, s.whisper_threads)), "-f", "mp4", str(out_tmp)]
    return cmd


def render_clip(s: Settings, *, section: Path, ass_text: str, out_path: Path, duration: float,
                gameplay: dict | None, music: dict | None, layout_name: str = "fit_blur",
                crop: list[float] | None = None, zoom: float = 1.0) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent
    ass_name = out_path.stem + ".ass"
    (work / ass_name).write_text(ass_text, encoding="utf-8")
    tmp = out_path.with_name(out_path.stem + ".tmp.mp4")
    cmd = build_command(s, section=section.resolve(), ass_name=ass_name, out_tmp=tmp.resolve(),
                        duration=duration, gameplay=gameplay, music=music, layout_name=layout_name,
                        crop=crop, zoom=zoom)
    log.info("rendering %s", out_path.name)
    p = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg render failed: {p.stderr[-800:]}")
    os.replace(tmp, out_path)
    (work / ass_name).unlink(missing_ok=True)
    return out_path


def first_frame(video: Path, jpg: Path) -> Path:
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
                        "-frames:v", "1", "-q:v", "2", str(jpg)], capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or not jpg.exists():
        raise RuntimeError(f"thumbnail extraction failed: {p.stderr[-300:]}")
    return jpg
