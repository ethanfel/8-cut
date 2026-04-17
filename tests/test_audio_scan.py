import tempfile, os
import numpy as np
from core.audio_scan import build_profile, _extract_features, scan_video, _similarity


def _make_wav(path: str, duration: float = 8.0, sr: int = 16000, freq: float = 440.0):
    """Create a short sine-wave WAV file for testing."""
    import soundfile as sf
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * freq * t)
    sf.write(path, audio, sr)


def test_extract_features_returns_62d_vector():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _make_wav(f.name)
    try:
        vec = _extract_features(f.name)
        assert vec.shape == (62,)
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
        assert profile["mean_vector"].shape == (62,)
        assert len(profile["clip_vectors"]) == 1
    finally:
        os.unlink(f.name)


def test_build_profile_multiple_clips():
    paths = []
    try:
        for i in range(3):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            _make_wav(f.name, freq=440 + i * 200)
            paths.append(f.name)
            f.close()

        profile = build_profile(paths)
        assert len(profile["clip_vectors"]) == 3
        assert profile["mean_vector"].shape == (62,)
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


def test_similarity_identical_is_one():
    a = np.array([1.0, 2.0, 3.0])
    assert abs(_similarity(a, a) - 1.0) < 1e-9


def test_similarity_distant_is_low():
    a = np.zeros(62)
    b = np.ones(62) * 100
    assert _similarity(a, b) < 0.01


def test_scan_video_finds_matching_region():
    """A video made of the same sine wave as the reference should match."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        _make_wav(ref.name, duration=8.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        _make_wav(vid.name, duration=20.0)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="average", threshold=0.01, hop=1.0)
        assert len(regions) > 0
        for start, end, score in regions:
            assert abs((end - start) - 8.0) < 0.1
            assert score >= 0.01
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
        regions = scan_video(vid.name, profile, mode="nearest", threshold=0.01, hop=1.0)
        assert len(regions) > 0
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)


def test_scan_video_high_threshold_no_match():
    """Different frequencies with very high threshold should not match."""
    import soundfile as sf
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        _make_wav(ref.name, duration=8.0, freq=440)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        # White noise — very different from sine wave
        sf.write(vid.name, np.random.randn(16000 * 20).astype(np.float32) * 0.1, 16000)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="average", threshold=0.5, hop=1.0)
        assert len(regions) == 0
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)


def test_scan_video_same_vs_different_discrimination():
    """Same-frequency match should score higher than cross-frequency."""
    import soundfile as sf
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        _make_wav(ref.name, duration=8.0, freq=440)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as same:
        _make_wav(same.name, duration=10.0, freq=440)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as diff:
        # White noise
        sf.write(diff.name, np.random.randn(16000 * 10).astype(np.float32) * 0.1, 16000)
    try:
        profile = build_profile([ref.name])
        same_regions = scan_video(same.name, profile, mode="average", threshold=0.0, hop=1.0)
        diff_regions = scan_video(diff.name, profile, mode="average", threshold=0.0, hop=1.0)
        # Same-audio scores should be higher than noise scores
        best_same = max(r[2] for r in same_regions)
        best_diff = max(r[2] for r in diff_regions)
        assert best_same > best_diff
    finally:
        os.unlink(ref.name)
        os.unlink(same.name)
        os.unlink(diff.name)


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
