import tempfile, os, json
from main import build_export_path, format_time, build_ffmpeg_command, build_sequence_dir, build_audio_extract_command, build_annotation_json_path, upsert_clip_annotation
from main import _normalize_filename, ProcessedDB


def test_build_export_path_first():
    assert build_export_path("/out", "clip", 1) == "/out/clip_001/clip_001.mp4"

def test_build_export_path_counter():
    assert build_export_path("/out", "clip", 42) == "/out/clip_042/clip_042.mp4"

def test_build_export_path_deep_counter():
    assert build_export_path("/out", "shot", 999) == "/out/shot_999/shot_999.mp4"

def test_build_export_path_sub():
    assert build_export_path("/out", "clip", 1, sub=0) == "/out/clip_001/clip_001_0.mp4"
    assert build_export_path("/out", "clip", 1, sub=2) == "/out/clip_001/clip_001_2.mp4"

def test_build_sequence_dir_sub():
    assert build_sequence_dir("/out", "clip", 1, sub=0) == "/out/clip_001/clip_001_0"
    assert build_sequence_dir("/out", "clip", 1, sub=1) == "/out/clip_001/clip_001_1"

def test_format_time_seconds():
    assert format_time(0.0) == "0:00.0"

def test_format_time_minutes():
    assert format_time(75.3) == "1:15.2"

def test_format_time_rounding():
    assert format_time(61.05) == "1:01.0"

def test_format_time_no_sixty_rollover():
    assert format_time(59.95) == "0:59.9"


def test_ffmpeg_command_no_resize():
    cmd = build_ffmpeg_command("/in/video.mp4", 12.5, "/out/clip_001.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd
    assert "-ss" in cmd
    assert str(12.5) in cmd
    assert "-t" in cmd
    assert "8" in cmd
    assert cmd[-1] == "/out/clip_001.mp4"
    assert "-vf" not in cmd

def test_ffmpeg_command_with_resize():
    cmd = build_ffmpeg_command("/in/video.mp4", 0.0, "/out/clip_001.mp4", short_side=256)
    assert "-vf" in cmd
    vf_value = cmd[cmd.index("-vf") + 1]
    assert "256" in vf_value
    assert "scale" in vf_value
    assert cmd[-1] == "/out/clip_001.mp4"


# --- _normalize_filename ---

def test_normalize_strips_extension():
    assert _normalize_filename("clip.mp4") == "clip"

def test_normalize_strips_resolution():
    assert _normalize_filename("clip_2160p.mp4") == "clip"

def test_normalize_strips_1080p():
    assert _normalize_filename("clip_1080p.mkv") == "clip"

def test_normalize_strips_multiple_tags():
    assert _normalize_filename("show_1080p_HDR.mkv") == "show"

def test_normalize_lowercases():
    assert _normalize_filename("MyVideo_4K.mp4") == "myvideo"

def test_normalize_collapses_separators():
    assert _normalize_filename("my__video--2160p.mp4") == "my_video"


# --- ProcessedDB ---

def test_db_add_and_find_exact():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 12.5, "/out/clip_001.mp4")
        assert db.find_similar("video.mp4") == "video.mp4"
    finally:
        os.unlink(path)

def test_db_find_similar_resolution_variant():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("episode_s01e01_2160p.mkv", 0.0, "/out/ep_001.mp4")
        assert db.find_similar("episode_s01e01_1080p.mkv") == "episode_s01e01_2160p.mkv"
    finally:
        os.unlink(path)

def test_db_find_similar_no_match():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("alpha.mp4", 0.0, "/out/alpha_001.mp4")
        assert db.find_similar("completely_different_zzzz.mp4") is None
    finally:
        os.unlink(path)

def test_db_disabled_survives_bad_path():
    db = ProcessedDB("/no/such/directory/8cut.db")
    db.add("x.mp4", 0.0, "/out/x_001.mp4")   # must not raise
    assert db.find_similar("x.mp4") is None

def test_db_get_markers_returns_sorted():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 30.0, "/out/clip_002.mp4")
        db.add("video.mp4", 10.0, "/out/clip_001.mp4")
        db.add("video.mp4", 50.0, "/out/clip_003.mp4")
        markers = db.get_markers("video.mp4")
        assert len(markers) == 3
        assert markers[0] == (10.0, 1, "/out/clip_001.mp4")
        assert markers[1] == (30.0, 2, "/out/clip_002.mp4")
        assert markers[2] == (50.0, 3, "/out/clip_003.mp4")
    finally:
        os.unlink(path)

def test_db_get_markers_fuzzy_match():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("show_2160p.mkv", 5.0, "/out/s_001.mp4")
        markers = db.get_markers("show_1080p.mkv")
        assert len(markers) == 1
        assert markers[0][0] == 5.0
        assert markers[0][2] == "/out/s_001.mp4"
    finally:
        os.unlink(path)

def test_db_get_markers_no_match():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        markers = db.get_markers("nothing.mp4")
        assert markers == []
    finally:
        os.unlink(path)

def test_db_get_markers_disabled():
    db = ProcessedDB("/no/such/directory/8cut.db")
    assert db.get_markers("x.mp4") == []

def test_ffmpeg_command_portrait_only():
    cmd = build_ffmpeg_command(
        "/in/video.mp4", 0.0, "/out/clip.mp4",
        portrait_ratio="9:16", crop_center=0.5,
    )
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop" in vf
    assert "9" in vf
    assert "scale" not in vf
    assert cmd[-1] == "/out/clip.mp4"

def test_ffmpeg_command_portrait_and_resize():
    cmd = build_ffmpeg_command(
        "/in/video.mp4", 0.0, "/out/clip.mp4",
        short_side=256, portrait_ratio="9:16", crop_center=0.5,
    )
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop" in vf
    assert "scale" in vf
    assert vf.index("crop") < vf.index("scale")
    assert cmd[-1] == "/out/clip.mp4"

def test_ffmpeg_command_portrait_off():
    cmd = build_ffmpeg_command("/in/video.mp4", 0.0, "/out/clip.mp4")
    assert "-vf" not in cmd

# --- build_audio_extract_command ---

def test_audio_extract_output_path():
    cmd = build_audio_extract_command("/in/v.mp4", 0.0, "/out/clip_001")
    assert cmd[-1] == "/out/clip_001.wav"

def test_audio_extract_no_video():
    cmd = build_audio_extract_command("/in/v.mp4", 0.0, "/out/clip_001")
    assert "-vn" in cmd

def test_audio_extract_lossless_codec():
    cmd = build_audio_extract_command("/in/v.mp4", 0.0, "/out/clip_001")
    assert "-c:a" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"

def test_audio_extract_timing():
    cmd = build_audio_extract_command("/in/v.mp4", 12.5, "/out/clip_001")
    assert "-ss" in cmd
    assert cmd[cmd.index("-ss") + 1] == "12.5"
    assert "-t" in cmd
    assert cmd[cmd.index("-t") + 1] == "8"


def test_build_sequence_dir_basic():
    assert build_sequence_dir("/out", "clip", 1) == "/out/clip_001/clip_001"

def test_build_sequence_dir_counter():
    assert build_sequence_dir("/out", "clip", 42) == "/out/clip_042/clip_042"

def test_ffmpeg_command_image_sequence():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/seq_001", image_sequence=True)
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libwebp"
    assert "-quality" in cmd
    assert cmd[-1] == "/out/seq_001/frame_%04d.webp"

def test_ffmpeg_command_image_sequence_with_resize():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/seq_001", image_sequence=True, short_side=256)
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale" in vf
    assert cmd[-1] == "/out/seq_001/frame_%04d.webp"

def test_ffmpeg_command_image_sequence_no_audio():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/seq_001", image_sequence=True)
    assert "-an" in cmd
    assert "-c:a" not in cmd
    assert "aac" not in cmd


def test_annotation_json_path():
    assert build_annotation_json_path("/out") == "/out/dataset.json"

def test_upsert_creates_file():
    with tempfile.TemporaryDirectory() as d:
        clip = os.path.join(d, "clip_001.mp4")
        upsert_clip_annotation(d, clip, "dog barking")
        with open(os.path.join(d, "dataset.json")) as f:
            entries = json.load(f)
        assert len(entries) == 1
        assert entries[0]["label"] == "dog barking"
        assert entries[0]["path"] == clip

def test_upsert_appends_new_clips():
    with tempfile.TemporaryDirectory() as d:
        upsert_clip_annotation(d, os.path.join(d, "clip_001.mp4"), "dog barking")
        upsert_clip_annotation(d, os.path.join(d, "clip_002.mp4"), "cat meowing")
        with open(os.path.join(d, "dataset.json")) as f:
            entries = json.load(f)
        assert len(entries) == 2

def test_upsert_replaces_existing():
    with tempfile.TemporaryDirectory() as d:
        clip = os.path.join(d, "clip_001.mp4")
        upsert_clip_annotation(d, clip, "dog barking")
        upsert_clip_annotation(d, clip, "cat meowing")
        with open(os.path.join(d, "dataset.json")) as f:
            entries = json.load(f)
        assert len(entries) == 1
        assert entries[0]["label"] == "cat meowing"

def test_upsert_empty_label_skips():
    with tempfile.TemporaryDirectory() as d:
        upsert_clip_annotation(d, os.path.join(d, "clip_001.mp4"), "")
        assert not os.path.exists(os.path.join(d, "dataset.json"))

def test_upsert_missing_folder_creates_it():
    with tempfile.TemporaryDirectory() as d:
        nested = os.path.join(d, "subdir", "deep")
        upsert_clip_annotation(nested, os.path.join(nested, "clip_001.mp4"), "dog barking")
        assert os.path.exists(os.path.join(nested, "dataset.json"))

def test_db_stores_label_and_category():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 0.0, "/out/clip_001.mp4", label="dog barking", category="Animal")
        row = db._con.execute(
            "SELECT label, category FROM processed WHERE filename = ?", ("video.mp4",)
        ).fetchone()
        assert row == ("dog barking", "Animal")
    finally:
        os.unlink(path)


def test_db_get_group_returns_all_sub_clips():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 10.0, "/out/clip_001/clip_001_0.mp4")
        db.add("video.mp4", 10.0, "/out/clip_001/clip_001_1.mp4")
        db.add("video.mp4", 10.0, "/out/clip_001/clip_001_2.mp4")
        group = db.get_group("/out/clip_001/clip_001_0.mp4")
        assert len(group) == 3
        assert "/out/clip_001/clip_001_0.mp4" in group
        assert "/out/clip_001/clip_001_2.mp4" in group
    finally:
        os.unlink(path)


def test_db_get_group_isolates_by_start_time():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 10.0, "/out/clip_001/clip_001_0.mp4")
        db.add("video.mp4", 10.0, "/out/clip_001/clip_001_1.mp4")
        db.add("video.mp4", 30.0, "/out/clip_002/clip_002_0.mp4")
        group = db.get_group("/out/clip_001/clip_001_0.mp4")
        assert len(group) == 2
    finally:
        os.unlink(path)


def test_db_delete_group_removes_all():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 10.0, "/out/clip_001/clip_001_0.mp4")
        db.add("video.mp4", 10.0, "/out/clip_001/clip_001_1.mp4")
        db.add("video.mp4", 30.0, "/out/clip_002/clip_002_0.mp4")
        deleted = db.delete_group("/out/clip_001/clip_001_0.mp4")
        assert len(deleted) == 2
        # clip_002 should still exist
        markers = db.get_markers("video.mp4")
        assert len(markers) == 1
        assert markers[0][0] == 30.0
    finally:
        os.unlink(path)


def test_db_get_group_disabled():
    db = ProcessedDB("/no/such/directory/8cut.db")
    assert db.get_group("/out/clip_001.mp4") == []


def test_db_delete_group_disabled():
    db = ProcessedDB("/no/such/directory/8cut.db")
    assert db.delete_group("/out/clip_001.mp4") == []
