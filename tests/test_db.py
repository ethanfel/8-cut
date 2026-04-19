import os
import tempfile
import time

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
        # Save three versions with small delays so timestamps differ
        db.save_scan_results("v.mp4", "test", "MODEL_A", [(0, 8, 0.9)])
        time.sleep(1.1)
        db.save_scan_results("v.mp4", "test", "MODEL_A",
                             [(0, 8, 0.8), (10, 18, 0.7)])
        time.sleep(1.1)
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
