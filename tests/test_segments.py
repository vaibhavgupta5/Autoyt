"""Audio-energy gameplay segment selector (no transcript, no speech needed)."""
import random


def test_windows_center_on_peaks_and_never_overlap():
    from autoyt.segments import build_windows

    energy = [0.0] * 600
    for peak in (120, 300, 480):
        for i in range(peak - 3, peak + 4):
            energy[i] = 1.0
    w = build_windows(energy, hop=1.0, total=600.0, n=3, clip_min=20, clip_max=30,
                      skip_s=60, rng=random.Random(1))
    assert len(w) == 3
    assert all(20 <= x["duration"] <= 30 for x in w)
    assert w[0]["score"] >= w[-1]["score"]
    for a, b in zip(w, w[1:]):
        assert b["end"] <= a["start"] or a["end"] <= b["start"]


def test_windows_respect_skip_and_bounds():
    from autoyt.segments import build_windows

    energy = [1.0] * 100
    w = build_windows(energy, 1.0, 100.0, 5, 10, 20, skip_s=40, rng=random.Random(2))
    assert w
    assert all(x["start"] >= 40 and x["end"] <= 100 for x in w)


def test_windows_beat_flat_energy():
    """A flat, quiet stretch before a loud peak must not win over the peak."""
    from autoyt.segments import build_windows

    energy = [0.05] * 200 + [1.0] * 20 + [0.05] * 200
    w = build_windows(energy, 1.0, 420.0, 1, 20, 30, skip_s=0, rng=random.Random(3))
    assert len(w) == 1 and w[0]["start"] <= 205 <= w[0]["end"]


def test_outro_and_intro_are_never_picked(env_settings, monkeypatch):
    import random
    from pathlib import Path

    from autoyt import segments, transcribe, ytdlp
    loud_end = [0.1] * 400 + [1.0] * 37          # 437 s video whose loudest part is the end card
    def fake_audio(s, v, d):
        (d / "a.m4a").write_bytes(b"x")
        return d / "a.m4a"

    monkeypatch.setattr(ytdlp, "download_audio", fake_audio)
    monkeypatch.setattr(transcribe, "split_audio", lambda a, d: [Path("c0.wav")])
    monkeypatch.setattr(segments, "energy_profile", lambda s, c: loud_end)
    w = segments.select_segments(env_settings, None, "v", 437.0, 1, {"min_seconds": 20, "max_seconds": 30},
                                 random.Random(1))
    assert w and all(x["end"] <= 437 - 45 and x["start"] >= 21 for x in w)
