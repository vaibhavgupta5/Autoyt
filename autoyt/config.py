"""Settings: built-in defaults < config/settings.yaml < environment variables."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "clip": {
        "min_seconds": 50.0,
        "max_seconds": 55.0,
        "pad_start": 0.15,
        "pad_end": 0.35,
        "window_minutes": 15,          # small windows give the model less to skip over
        "window_overlap_seconds": 45,
        "candidates_per_window": 3,
        "min_score": 10,               # hook + standalone out of 20; filters weak picks in code
        "max_gap_seconds": 3.5,
        "min_words": 80,
        "min_gap_between_clips": 5.0,
        "max_clips_per_15min": 3,
    },
    "source": {
        "min_minutes": 60,
        "max_minutes": 240,
        "cooldown_days": 2,
        "listing_refresh_days": 7,
        "max_pick_tries": 8,
        "max_listing_items": 3000,
        "max_height": 720,  # never download more than 720p: saves size and time
    },
    "min_clips_per_video": 3,
    "layout": {
        "width": 1080,
        "height": 1920,
        "top_height": 1150,
        "bottom_height": 770,
        "fps": 30,
        "font": "DejaVu Sans",
        "heading_size": 78,
        "subtitle_size": 74,
        "crf": 23,
        "preset": "veryfast",
    },
    # music_lufs: loudness every music track is brought to (speech is -14, so music sits 10 LU under it,
    # then ducks further while speech plays). music_db is only the fallback for an unmeasured track.
    "audio": {"music_lufs": -24, "music_db": -24, "speech_lufs": -14},
    "banned_words": [
        "suicide", "kill yourself", "self-harm", "self harm", "rape",
        "terrorist", "child abuse", "genocide",
    ],
    # (days since first upload, clips per day) - the last entry whose days <= age wins
    "ramp": [[0, 3], [7, 5], [14, 8]],
    "slots": {"first_delay_minutes": 30, "target_span_hours": 20, "min_gap": 45, "max_gap": 180, "jitter_minutes": 8},
    "retry": {"step_delays": [60, 300, 900], "run_attempts_per_day": 4, "run_retry_minutes": 60},
    "retention": {"dry_run_days": 3, "work_days": 1},
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env(name: str, default: Any = None) -> Any:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    home: Path
    cfg: dict
    mode: str = "dry_run"  # dry_run | private | scheduled
    clips_per_day: int = 8
    ramp_up: bool = True
    transcript_source: str = "auto"  # auto = YouTube captions first, Whisper if unavailable | captions | whisper
    whisper_model: str = "small.en"
    whisper_threads: int = 8
    min_battery: int = 40
    min_free_disk_gb: float = 15.0
    tz: str = "Asia/Kolkata"
    poll_minutes: int = 15
    delete_after_upload: bool = True
    paused: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email: str = ""
    ytdlp_args: list[str] = field(default_factory=list)
    power_dir: str = "/sys/class/power_supply"

    # ---- paths ----
    @property
    def data_dir(self) -> Path: return self.home / "data"
    @property
    def work_dir(self) -> Path: return self.data_dir / "work"
    @property
    def out_dir(self) -> Path: return self.data_dir / "output"
    @property
    def assets_dir(self) -> Path: return self.home / "assets"
    @property
    def config_dir(self) -> Path: return self.home / "config"
    @property
    def secrets_dir(self) -> Path: return self.home / "secrets"
    @property
    def db_path(self) -> Path: return self.data_dir / "state.db"
    @property
    def pause_file(self) -> Path: return self.data_dir / "PAUSE"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.work_dir, self.out_dir, self.assets_dir / "music",
                  self.assets_dir / "gameplay", self.secrets_dir):
            p.mkdir(parents=True, exist_ok=True)

    def yaml_file(self, name: str) -> Any:
        p = self.config_dir / name
        if not p.exists():
            return None
        return yaml.safe_load(p.read_text()) or None


def load_settings(home: Path | None = None) -> Settings:
    home = Path(home or _env("AUTOYT_HOME", Path.cwd())).resolve()
    user_cfg: dict = {}
    sp = home / "config" / "settings.yaml"
    if sp.exists():
        user_cfg = yaml.safe_load(sp.read_text()) or {}
    cfg = _deep_merge(DEFAULTS, user_cfg)

    mode = str(_env("AUTOYT_MODE", "dry_run")).strip().lower()
    if mode not in ("dry_run", "private", "scheduled", "public"):
        raise ValueError(f"AUTOYT_MODE must be dry_run, scheduled, public or private (got {mode!r})")

    cpu = os.cpu_count() or 4
    s = Settings(
        home=home,
        cfg=cfg,
        mode=mode,
        clips_per_day=int(_env("CLIPS_PER_DAY", 8)),
        ramp_up=_env_bool("RAMP_UP", True),
        transcript_source=str(_env("TRANSCRIPT_SOURCE", "auto")).strip().lower(),
        whisper_model=str(_env("WHISPER_MODEL", "small.en")),
        whisper_threads=int(_env("WHISPER_THREADS", max(2, min(8, cpu // 2)))),
        min_battery=int(_env("MIN_BATTERY_PERCENT", 40)),
        min_free_disk_gb=float(_env("MIN_FREE_DISK_GB", 15)),
        tz=str(_env("TZ", "Asia/Kolkata")),
        poll_minutes=int(_env("POLL_MINUTES", 15)),
        delete_after_upload=_env_bool("DELETE_AFTER_UPLOAD", True),
        gemini_api_key=str(_env("GEMINI_API_KEY", "")),
        gemini_model=str(_env("GEMINI_MODEL", "gemini-3.5-flash-lite")),
        smtp_host=str(_env("SMTP_HOST", "")),
        smtp_port=int(_env("SMTP_PORT", 587)),
        smtp_username=str(_env("SMTP_USERNAME", "")),
        smtp_password=str(_env("SMTP_PASSWORD", "")),
        smtp_from=str(_env("SMTP_FROM", "")),
        notify_email=str(_env("NOTIFY_EMAIL", "")),
        ytdlp_args=str(_env("YTDLP_ARGS", "")).split(),
        power_dir=str(_env("POWER_SUPPLY_DIR", "/sys/class/power_supply")),
    )
    return s
