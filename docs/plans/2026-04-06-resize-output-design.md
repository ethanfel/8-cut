# Resize Output Design

## Overview

Add a user-configurable short-side resize to every exported clip. When set, ffmpeg scales the output so the shorter dimension equals the specified pixel value, preserving aspect ratio. When blank, output is native resolution.

## UI

A labeled `QLineEdit` ("Short side:") with placeholder `"px (opt.)"` added to the export row, immediately before the Export button. Value is optional — blank means no resize.

## Persistence

`QSettings("8cut", "8cut")` stores the value under key `resize_short_side`. The field is pre-filled on startup and saved on every text change.

## ffmpeg Command Change

`build_ffmpeg_command` gains an optional `short_side: int | None = None` parameter. When set, it appends:

```
-vf scale='if(lt(iw,ih),N,-2)':'if(lt(iw,ih),-2,N)'
```

Where `N` is the short-side value. This selects width for portrait videos and height for landscape videos. `-2` maintains aspect ratio with even-pixel rounding (required by libx264).

No `-vf` flag is added when `short_side` is `None`.

## Parsing

`MainWindow._on_export` reads the field, strips whitespace, and attempts `int()` conversion. Any non-positive or non-numeric value is treated as no resize (passes `None` to `build_ffmpeg_command`).

## Testing

- `test_ffmpeg_command_no_resize` — existing test, verify no `-vf` in output
- `test_ffmpeg_command_with_resize` — pass `short_side=256`, verify `-vf` and the scale expression appear in the command
