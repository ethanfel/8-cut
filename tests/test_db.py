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
