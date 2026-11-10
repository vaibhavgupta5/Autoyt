"""Local transcription with faster-whisper.

Two uses:
- full video, in resumable 30-minute pieces (fallback when YouTube has no captions)
- a single ~55 s clip slice, so burned-in captions are accurate even when the video was selected from
  YouTube's auto-captions (the fast path)
"""
from __future__ import annotations

import gc
import json
import logging
import os
import subprocess
import threading
from pathlib import Path

from .config import Settings
from .errors import SourceRejected

log = logging.getLogger("autoyt.transcribe")
CHUNK_SECONDS = 1800

_MODEL = None
_MODEL_NAME = None
_LOCK = threading.Lock()


def get_model(s: Settings, name: str | None = None):
    """One shared model per process, loaded on first use and freed by release_model()."""
    global _MODEL, _MODEL_NAME
    name = name or s.whisper_model
    with _LOCK:
        if _MODEL is None or _MODEL_NAME != name:
            from faster_whisper import WhisperModel  # lazy: heavy import

            log.info("loading whisper model %s", name)
            _MODEL = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=s.whisper_threads,
                                  download_root=str(s.data_dir / "models"))
            _MODEL_NAME = name
        return _MODEL


def release_model() -> None:
    global _MODEL, _MODEL_NAME
    with _LOCK:
        _MODEL, _MODEL_NAME = None, None
    gc.collect()


def split_audio(audio: Path, out_dir: Path) -> list[Path]:
    """Convert to 16 kHz mono wav pieces of 30 minutes."""
    for old in out_dir.glob("chunk_*.wav"):
        old.unlink()
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
           "-reset_timestamps", "1", str(out_dir / "chunk_%03d.wav")]
    subprocess.run(cmd, check=True, timeout=3600)
    chunks = sorted(out_dir.glob("chunk_*.wav"))
    if not chunks:
        raise RuntimeError("audio split produced no chunks")
    return chunks


def load_wav(path: Path):
    """Read a 16 kHz mono 16-bit wav into float32 samples.

    Passing samples to faster-whisper avoids its PyAV file decoder, whose version pairing is fragile.
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError(f"{path.name} must be 16 kHz mono 16-bit wav")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _words(segs, offset: float = 0.0) -> list[dict]:
    out = []
    for seg in segs:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                out.append({"w": t, "s": round(w.start + offset, 3), "e": round(w.end + offset, 3)})
    return out


def transcribe_section(s: Settings, media: Path, work_dir: Path) -> list[dict]:
    """Accurate word timestamps for one clip slice, relative to the slice start (0 = first frame)."""
    wav = work_dir / (media.stem + "_asr.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(wav)], check=True, timeout=300)
    try:
        model = get_model(s)
        segs, _ = model.transcribe(load_wav(wav), language="en", beam_size=1, vad_filter=True,
                                   word_timestamps=True, condition_on_previous_text=False)
        return _words(segs)
    finally:
        wav.unlink(missing_ok=True)


def transcribe_chunks(s: Settings, chunks: list[Path], out_dir: Path, model_name: str | None = None) -> dict:
    """Transcribe every chunk (skipping finished ones) and merge into one transcript."""
    model_name = model_name or s.whisper_model
    language = "en" if model_name.endswith(".en") else None
    words: list[dict] = []
    for i, chunk in enumerate(chunks):
        part = out_dir / f"tr_{i:03d}.json"
        if part.exists():
            words.extend(json.loads(part.read_text()))
            continue
        model = get_model(s, model_name)
        segs, info = model.transcribe(
            load_wav(chunk), language=language, beam_size=1, vad_filter=True,
            word_timestamps=True, condition_on_previous_text=False,
        )
        if language is None:
            if not str(info.language).startswith("en") or info.language_probability < 0.6:
                raise SourceRejected(f"audio language is {info.language} ({info.language_probability:.2f})")
            language = "en"
        chunk_words = _words(segs, offset=i * CHUNK_SECONDS)
        _save_json(part, chunk_words)
        words.extend(chunk_words)
        log.info("chunk %d/%d done (%d words)", i + 1, len(chunks), len(chunk_words))
    words.sort(key=lambda w: w["s"])
    duration = max((w["e"] for w in words), default=0.0)
    return {"language": language or "en", "source": "whisper", "duration": duration, "words": words}


def check_speech_density(tr: dict, min_wpm: float = 30.0, total_seconds: float | None = None) -> None:
    total = total_seconds or tr["duration"]
    if total <= 0:
        raise SourceRejected("no speech found")
    wpm = len(tr["words"]) / (total / 60)
    if wpm < min_wpm:
        raise SourceRejected(f"too little speech ({wpm:.0f} words/min)")


def run_transcription(s: Settings, audio: Path, work: Path, total_seconds: float | None = None) -> dict:
    tr_path = work / "transcript.json"
    if tr_path.exists():
        return json.loads(tr_path.read_text())
    chunks = sorted(work.glob("chunk_*.wav")) or split_audio(audio, work)
    tr = transcribe_chunks(s, chunks, work)
    check_speech_density(tr, total_seconds=total_seconds)
    _save_json(tr_path, tr)
    return tr
