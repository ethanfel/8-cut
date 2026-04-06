# Portrait Crop Design

## Overview

Export the 8s clip as portrait video by cropping a vertical window from the landscape source. The user picks a portrait aspect ratio from a dropdown and positions the crop window by clicking on the video or a crop bar below the timeline. Settings persist between relaunches.

## UI

- **Ratio dropdown (`QComboBox`)** — options: `Off`, `9:16`, `4:5`, `1:1`. Added to the export row between the Short side field and the next-file label. Persisted via `QSettings` key `portrait_ratio`.
- **`CropBarWidget`** — a thin (~16px) horizontal bar placed between the timeline and the playback controls. Hidden when ratio is "Off". Shows the full frame width as a dark bar with the selected crop window highlighted in a lighter color. Clicking repositions the crop center. Shown/hidden based on ratio selection.

## CropBarWidget

- `set_source_ratio(w: int, h: int)` — called after file load; stores `w/h` to compute crop window width as fraction of bar.
- `set_portrait_ratio(ratio: str | None)` — updates the crop window proportion and repaints.
- `set_crop_center(frac: float)` — updates highlighted position and repaints.
- `mousePressEvent` — emits `crop_changed = pyqtSignal(float)` with clamped x fraction.
- Crop window fraction = `portrait_w / source_w` where `portrait_w = source_h * (num/den)`. Clamped so window never exceeds bar bounds.

## MpvWidget click

`MpvWidget` gains `crop_clicked = pyqtSignal(float)` emitted on `mousePressEvent` with `event.position().x() / self.width()`.

## MainWindow wiring

- `_crop_center: float = 0.5` — current crop center, persisted via `QSettings` key `crop_center`.
- `_on_crop_click(frac: float)` — shared slot for both `MpvWidget.crop_clicked` and `CropBarWidget.crop_changed`. Clamps, stores, saves to QSettings, updates bar.
- `_after_load` — calls `self._crop_bar.set_source_ratio(w, h)` using mpv dimensions after file load.
- `_on_portrait_ratio_changed` — shows/hides `CropBarWidget`, updates bar ratio, saves to QSettings.
- `_on_export` — reads ratio and crop center, passes to `build_ffmpeg_command`.

## ffmpeg filter chain

`build_ffmpeg_command` gains `portrait_ratio: str | None = None` and `crop_center: float = 0.5`.

Crop filter expressions (ffmpeg evaluates `iw`/`ih` at runtime — no need to know source dimensions ahead of time):

| Ratio | Crop width expression |
|-------|----------------------|
| 9:16  | `ih*9/16`            |
| 4:5   | `ih*4/5`             |
| 1:1   | `ih`                 |

X offset: `max(0\\,min((iw-CW)*C\\,iw-CW))` where `CW` is the crop width expression and `C` is crop center (0–1).

Full crop filter: `crop=CW:ih:X:0`

When both portrait crop and short-side resize are active, filters chain as a single `-vf` value: `crop=...,scale=...` (crop first, then scale).

## Persistence

| QSettings key    | Value              |
|------------------|--------------------|
| `portrait_ratio` | `"Off"` / `"9:16"` / `"4:5"` / `"1:1"` |
| `crop_center`    | float string, default `"0.5"` |
