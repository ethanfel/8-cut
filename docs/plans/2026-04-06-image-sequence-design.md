# Image Sequence Export Design

## Overview

Add lossless WebP image sequence export as an alternative to MP4. A format dropdown next to the Export button selects between "MP4" and "WebP sequence". All existing filter chain options (resize, portrait crop) apply to both formats.

## UI Changes

A `QComboBox(["MP4", "WebP sequence"])` is inserted immediately left of the Export button (between the Portrait dropdown and the counter label). No new button. Selection is persisted via `QSettings("8cut", "8cut")` key `"export_format"`.

## Output Path

WebP sequence output: `<folder>/<stem>_NNN/` directory, same counter logic as MP4.
Frames inside: `frame_0000.webp`, `frame_0001.webp`, …

`build_export_path` is MP4-only. A new `build_sequence_dir` function returns the directory path using the same `<stem>_NNN` counter pattern.

## ffmpeg Changes

`build_ffmpeg_command` gains `image_sequence: bool = False`.

When `True`:
- Output argument: `<dir>/frame_%04d.webp`
- Appends: `-vcodec libwebp -lossless 1 -compression_level 4`
- Filter chain (resize, portrait crop) unchanged

When `False`: unchanged behavior.

## ExportWorker

When `image_sequence=True`, `os.makedirs(output_dir, exist_ok=True)` before running ffmpeg. `_on_export_done` sets `_last_export_path` to the sequence directory path.

## Testing

New `test_utils.py` cases:
- `build_ffmpeg_command` with `image_sequence=True`: verify `-vcodec libwebp`, `-lossless 1`, `-compression_level 4`, output ends with `frame_%04d.webp`
- With `image_sequence=True` and `short_side=256`: verify `-vf` still present
- `build_sequence_dir`: verify path pattern matches `<stem>_NNN` directory form
