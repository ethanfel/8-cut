import tempfile, os
import numpy as np
from core.audio_scan import build_profile, _extract_mfcc


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
        assert vec.shape == (20,)
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
        assert profile["mean_vector"].shape == (20,)
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
        assert profile["mean_vector"].shape == (20,)
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
