import tempfile, os
import numpy as np
from core.audio_scan import build_profile, _extract_mfcc, scan_video


def _make_wav(path: str, duration: float = 8.0, sr: int = 22050):
    """Create a short sine-wave WAV file for testing."""
    import soundfile as sf
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(path, audio, sr)


def test_extract_mfcc_returns_1d_vector():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _make_wav(f.name)
    try:
        vec = _extract_mfcc(f.name)
        assert vec.shape == (40,)
        assert not np.isnan(vec).any()
    finally:
        os.unlink(f.name)


def test_build_profile_single_clip():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _make_wav(f.name)
    try:
        profile = build_profile([f.name])
        assert "mean_vector" in profile
        assert "clip_vectors" in profile
        assert profile["mean_vector"].shape == (40,)
        assert len(profile["clip_vectors"]) == 1
    finally:
        os.unlink(f.name)


def test_build_profile_multiple_clips():
    paths = []
    try:
        for i in range(3):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            freq = 440 + i * 200
            import soundfile as sf
            t = np.linspace(0, 8.0, 22050 * 8, endpoint=False)
            sf.write(f.name, 0.5 * np.sin(2 * np.pi * freq * t), 22050)
            paths.append(f.name)
            f.close()

        profile = build_profile(paths)
        assert len(profile["clip_vectors"]) == 3
        assert profile["mean_vector"].shape == (40,)
    finally:
        for p in paths:
            os.unlink(p)


def test_build_profile_skips_missing_files():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _make_wav(f.name)
    try:
        profile = build_profile([f.name, "/no/such/file.wav"])
        assert len(profile["clip_vectors"]) == 1
    finally:
        os.unlink(f.name)


def test_build_profile_empty_returns_none():
    result = build_profile([])
    assert result is None


def test_scan_video_finds_matching_region():
    """A video made of the same sine wave as the reference should match."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        _make_wav(ref.name, duration=8.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        _make_wav(vid.name, duration=20.0)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="average", threshold=0.5, hop=1.0)
        assert len(regions) > 0
        for start, end, score in regions:
            assert abs((end - start) - 8.0) < 1e-9
            assert score >= 0.5
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)


def test_scan_video_nearest_mode():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        _make_wav(ref.name, duration=8.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        _make_wav(vid.name, duration=20.0)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="nearest", threshold=0.5, hop=1.0)
        assert len(regions) > 0
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)


def test_scan_video_high_threshold_no_match():
    """Different frequencies with very high threshold should not match."""
    import soundfile as sf
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        t = np.linspace(0, 8.0, 22050 * 8, endpoint=False)
        sf.write(ref.name, 0.5 * np.sin(2 * np.pi * 440 * t), 22050)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        # White noise — very different from sine wave
        sf.write(vid.name, np.random.randn(22050 * 20).astype(np.float32) * 0.1, 22050)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="average", threshold=0.99, hop=1.0)
        assert len(regions) == 0
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)


def test_db_get_all_export_paths():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        from core.db import ProcessedDB
        db = ProcessedDB(path)
        db.add("a.mp4", 10.0, "/out/a_001.mp4", profile="test")
        db.add("b.mp4", 20.0, "/out/b_001.mp4", profile="test")
        db.add("c.mp4", 30.0, "/out/c_001.mp4", profile="other")
        paths = db.get_all_export_paths("test")
        assert set(paths) == {"/out/a_001.mp4", "/out/b_001.mp4"}
    finally:
        os.unlink(path)
