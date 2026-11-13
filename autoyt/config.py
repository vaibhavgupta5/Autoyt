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
        # Intros and outros rarely hook anyone: a clip may not start in the first 90 s or the last 120 s
        # of the source, unless the model rated its hook 9+.
        "skip_intro_seconds": 90,
        "skip_outro_seconds": 120,
    },
    "source": {
        "min_minutes": 60,
        "max_minutes": 240,
        "cooldown_days": 2,
        "listing_refresh_days": 7,
        "max_pick_tries": 8,
        "max_listing_items": 3000,
        "max_height": 720,  # never download more than 720p: saves size and time
        # A video without YouTube captions needs Whisper over all of it. Above this length that costs
        # 20+ minutes of CPU, so such a video is skipped and another one picked instead.
        "max_whisper_minutes": 75,
    },
    "min_clips_per_video": 3,
    # Per-kind overrides of the `clip` block above. GTA6 gameplay is short-form: 20-45 s, no captions.
    "kinds": {
        "podcast": {"clip": {"min_seconds": 50.0, "max_seconds": 55.0}},
        # GTA news and breakdown beats are short; short clips loop and get watched to the end more often.
        "gta6_talk": {"clip": {"min_seconds": 25.0, "max_seconds": 45.0, "min_words": 55,
                               "max_music_ratio": 0.35}},
        "gta6_gameplay": {"clip": {"min_seconds": 20.0, "max_seconds": 45.0}},
    },
    # min/max videos per day once the ramp is over; talk_weight splits them between the two kinds.
    "gta6": {"min": 3, "max": 6, "talk_weight": 0.5, "ramp": [[0, 2], [4, 3]], "launch_date": "2026-11-19"},
    # Volume grows only when views do. Each level is clips per day; GTA levels are [min, max] (a random
    # count in that range). Moving up from level i needs the 7-day median of 72-hour views per clip to
    # reach thresholds[i]; falling under a third of the threshold below drops a level. A copyright
    # strike or claim in the last 7 days holds the level.
    "growth": {
        "podcast_levels": [3, 5, 7],
        "gta6_levels": [[2, 2], [3, 3], [3, 6]],
        "thresholds": [50, 150],
        "min_samples": 5,
        "min_days_at_level": 3,
        "shift_ratio": 3.0,          # a kind whose median is 3x the other's takes up to 2 of its daily slots
        "max_shift": 2,
    },
    # Batching spreads the day's work: several small runs at random times instead of one long one.
    "batching": {"max_clips_per_batch": 4, "window": ["08:00", "22:00"], "min_gap_hours": 2},
    "layout": {
        "width": 1080,
        "height": 1920,
        "top_height": 1150,
        "bottom_height": 770,
        "fps": 30,
        "font": "DejaVu Sans",
        "heading_size": 64,
        "heading_top": 120,
        "subtitle_size": 74,
        "subtitle_y": 620,       # captions stay inside the source panel, never on the seam
        "credit_seconds": 1.5,   # the credit shows briefly, then gets out of the way
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
    # Videos go public inside `publish_window`, in the audience's timezone (US evening by default), at least
    # `min_gap` minutes apart across every kind. `window_overrides` widens or moves the window on given dates
    # (GTA VI launch night).
    "slots": {"first_delay_minutes": 30, "target_span_hours": 20, "min_gap": 30, "max_gap": 120, "jitter_minutes": 8,
              "audience_tz": "America/New_York", "publish_window": ["11:00", "23:00"],
              "window_overrides": {"2026-11-18": ["11:00", "23:59"], "2026-11-19": ["00:05", "23:30"]}},
    # YouTube's bot check: back off the whole pipeline (hours, growing with each repeat) and email if it lasts.
    "bot_check": {"backoff_hours": [2, 4, 8], "alert_after_hours": 6},
    # Daily view counts per clip at these ages (days), used by the summary email, source weights and growth.
    "stats": {"checkpoints": [1, 3, 7], "min_clips_for_weights": 20},
    # auto = post one question comment per video after it goes public, if the YouTube login allows it
    # (needs `autoyt auth --extended` once); off = never.
    "engagement": {"comment": "auto"},
    # Keeps the laptop usable: renders and Whisper use a few threads, slices download two at a time.
    "resources": {"prefetch_workers": 2},
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
    whisper_threads: int = 4
    render_threads: int = 3
    pot_provider_url: str = ""    # bgutil PO-token server, e.g. http://bgutil-provider:4416
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
        whisper_threads=int(_env("WHISPER_THREADS", max(2, min(4, cpu // 2)))),
        render_threads=int(_env("RENDER_THREADS", max(2, min(3, cpu // 2)))),
        pot_provider_url=str(_env("POT_PROVIDER_URL", "")),
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
