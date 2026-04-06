from main import build_export_path, format_time, build_ffmpeg_command


def test_build_export_path_first():
    assert build_export_path("/out", "clip", 1) == "/out/clip_001.mp4"

def test_build_export_path_counter():
    assert build_export_path("/out", "clip", 42) == "/out/clip_042.mp4"

def test_build_export_path_deep_counter():
    assert build_export_path("/out", "shot", 999) == "/out/shot_999.mp4"

def test_format_time_seconds():
    assert format_time(0.0) == "0:00.0"

def test_format_time_minutes():
    assert format_time(75.3) == "1:15.2"

def test_format_time_rounding():
    assert format_time(61.05) == "1:01.0"

def test_format_time_no_sixty_rollover():
    assert format_time(59.95) == "0:59.9"


def test_ffmpeg_command():
    cmd = build_ffmpeg_command("/in/video.mp4", 12.5, "/out/clip_001.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd
    assert str(12.5) in cmd
    assert "-t" in cmd
    assert "8" in cmd
    assert cmd[-1] == "/out/clip_001.mp4"
