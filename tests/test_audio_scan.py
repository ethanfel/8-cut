import tempfile, os
import numpy as np
from core.audio_scan import scan_video, load_classifier, default_model_path


def test_scan_video_no_model_returns_empty():
    """scan_video with no model should return empty list."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        import soundfile as sf
        sf.write(vid.name, np.random.randn(16000 * 20).astype(np.float32) * 0.1, 16000)
    try:
        regions = scan_video(vid.name, model=None)
        assert regions == []
    finally:
        os.unlink(vid.name)


def test_load_classifier_missing_returns_none():
    assert load_classifier("/no/such/model.joblib") is None


def test_default_model_path_contains_profile():
    path = default_model_path("test_profile")
    assert "test_profile" in path
    assert path.endswith(".joblib")


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
