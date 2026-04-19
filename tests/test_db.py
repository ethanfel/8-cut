import os
import tempfile

from core.db import ProcessedDB


def test_export_folders_excludes_scan_exports():
    """Scan-export-only folders should not appear when include_scan_exports=False."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        # Manual export
        db.add("a.mp4", 10.0, "/out/mp4_Intense/g1/clip.mp4", profile="test")
        # Scan export to different folder
        db.add("a.mp4", 20.0, "/out/mp4_ScanOnly/g1/clip.mp4", profile="test",
               scan_export=True)
        folders = db.get_export_folders("test")
        assert "mp4_Intense" in folders
        assert "mp4_ScanOnly" not in folders, "scan-only folder should be excluded"
        # With include_scan_exports=True, both should appear
        folders_all = db.get_export_folders("test", include_scan_exports=True)
        assert "mp4_ScanOnly" in folders_all
    finally:
        os.unlink(path)


def test_scan_result_history():
    """save_scan_results should keep multiple versions."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        # Save three versions (microsecond-precision timestamps avoid collisions)
        db.save_scan_results("v.mp4", "test", "MODEL_A", [(0, 8, 0.9)])
        db.save_scan_results("v.mp4", "test", "MODEL_A",
                             [(0, 8, 0.8), (10, 18, 0.7)])
        db.save_scan_results("v.mp4", "test", "MODEL_A", [(5, 13, 0.95)])
        versions = db.get_scan_versions("v.mp4", "test", "MODEL_A")
        assert len(versions) == 3
        # Most recent first
        assert versions[0]["count"] == 1   # latest: 1 region
        assert versions[1]["count"] == 2   # middle: 2 regions
        assert versions[2]["count"] == 1   # oldest: 1 region
        # get_scan_results returns latest version by default
        results = db.get_scan_results("v.mp4", "test")
        assert len(results.get("MODEL_A", [])) == 1
    finally:
        os.unlink(path)


def test_hard_negatives_source_model():
    """Hard negatives should store source_model."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add_hard_negatives("a.mp4", "test", [10.0, 20.0],
                              source_path="/a.mp4", source_model="HUBERT_XLARGE")
        rows = db.get_hard_negatives("test")
        assert len(rows) == 2
        assert all(r["source_model"] == "HUBERT_XLARGE" for r in rows)
    finally:
        os.unlink(path)


def test_training_data_skips_hard_negatives():
    """get_training_data with use_hard_negatives=False should skip them."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        # Create a source file that "exists" — use the temp db file itself
        db.add("a.mp4", 10.0, "/out/folder/g/clip.mp4", profile="test",
               source_path=path)
        db.add_hard_negatives("a.mp4", "test", [500.0], source_path=path)
        # With hard negatives
        data_with = db.get_training_data("test", "folder", use_hard_negatives=True)
        # Without hard negatives
        data_without = db.get_training_data("test", "folder", use_hard_negatives=False)
        assert len(data_with) >= 1
        # The "with" case should have the hard negative time in neg list
        neg_with = sum(len(vi[3]) for vi in data_with)
        neg_without = sum(len(vi[3]) for vi in data_without)
        assert neg_with > neg_without, "hard negatives should be excluded when use_hard_negatives=False"
    finally:
        os.unlink(path)


def test_delete_hard_negatives_by_ids():
    """delete_hard_negatives_by_ids should remove specific rows."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add_hard_negatives("a.mp4", "test", [10.0, 20.0, 30.0],
                              source_path="/a.mp4")
        rows = db.get_hard_negatives("test")
        assert len(rows) == 3
        # Delete first two
        db.delete_hard_negatives_by_ids([rows[0]["id"], rows[1]["id"]])
        remaining = db.get_hard_negatives("test")
        assert len(remaining) == 1
        assert remaining[0]["start_time"] == 30.0
    finally:
        os.unlink(path)
