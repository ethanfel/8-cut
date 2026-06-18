# LTX-2 per-tab export mode — Design

**Goal:** Add an export *pipeline mode* to each file-list tab — **Foley** (current behavior) or **LTX-2** — so the same source videos can feed both a Foley dataset (8 s clips) and an LTX-2 V2A dataset (frame-exact, ÷32, 25 fps) without the two ever mixing.

**Depends on:** the per-tab export folder feature (branch `tab-export-folder`) — this design extends that per-tab state. Implementation branch `ltx2-preset` is based on it.

**Scope:** soft preset (no hard enforcement — defaults are LTX-2-legal but every control stays editable). `core/` gains optional pipeline params; Foley path is byte-for-byte unchanged.

---

## LTX-2 constraints (why this exists)

LTX-2 (32× spatial VAE, 8× temporal + 1) requires, for a clip:
- **W and H each divisible by 32.**
- **Frame count F such that `F % 8 == 1`** → 9, 17, 25, … 201, … (transformer seq-len ∝ `(W/32)·(H/32)·((F−1)/8+1)`).
- **fps** only sets real duration `F/fps`; for V2A it fixes the paired-audio length and audio↔motion sync, so it must be **consistent across the dataset and equal to the inference `frame_rate`**. Target: **25 fps**.
- V2A video is frozen conditioning → low spatial res (384–512) is fine and cheaper.

Note: 8 s @ 25 fps = 200 frames, and `200 % 8 == 0` → **8 s is not legal**. Nearest legal: F=193 (7.72 s) or **F=201 (8.04 s)**.

---

## Model: per-tab mode

Each tab (`PlaylistWidget`) gains `_mode ∈ {"foley","ltx2"}`, persisted alongside `_dest_folder`/`_pinned`/`_tab_folder` in `_save_playlist_tabs`/`_load_playlist_tabs`. Default `"foley"` → existing tabs load unchanged. The **active tab's mode drives the export pipeline and the length control.**

### Tab context menu (`_DeckTabBar`/`_PlaylistTabBar`)
- **Duplicate as LTX-2** — headline action: clone the tab's file list + separators into a new tab; set `mode="ltx2"`; derive a separate export folder `"<dest_folder>_ltx2"`; load LTX-2 default geometry. Lets you spin an LTX-2 dataset off a Foley working set.
- **Duplicate tab** — clone keeping the same mode.
- **LTX-2 mode** — checkable, flips an existing tab between foley/ltx2.
- Tab label shows a small **`[LTX2]`** badge when `mode=="ltx2"`.

## What `ltx2` mode changes (soft — still editable)

| Aspect | Foley | LTX-2 |
|--------|-------|-------|
| Clip length | Duration spinbox (seconds) | **Frame-count F** control stepping the legal series (9, 17, …, 201, …); shows `= F/25 s` |
| Output fps | inherits source | **forced 25 fps** (resample; preserves duration/sync) |
| Output W×H | short-side resize → even long side | **center-cropped to ÷32** on both axes (no aspect distortion; loses ≤31 px/side); resize default **512** |
| Frame exactness | duration-based | exactly **F** frames (`-frames:v F`) |

Defaults loaded on convert: resize **512**, **F = 201** (≈8.04 s, mirrors the 8 s Foley clips), ratio as set. All editable afterward.

## Pipeline (`core/ffmpeg.build_ffmpeg_command`)

Add optional params; Foley calls pass none → identical output to today:
- `target_fps: float | None` — when set, append `fps={target_fps}` filter and `-r {target_fps}`.
- `snap32: bool` — when true, after the scale append a centered crop to the nearest lower multiple of 32 on each axis: `crop=trunc(iw/32)*32:trunc(ih/32)*32`.
- Frame-exact length: caller computes `duration = F/target_fps` and passes `-frames:v F` on the video output so the clip has exactly F frames; audio extract uses the same `F/target_fps` duration so V2A pairing stays aligned.

Filter order: portrait-crop (aspect) → scale (short side, ÷32 default) → snap32 crop → fps. The snap32 center-crop runs after scaling so the ÷32 trim is on final pixels.

## UI wiring (`MainWindow`)

- The length spinbox area swaps with the active tab's mode: Foley shows *Duration (s)*; LTX-2 shows *Frames (F)* with a live `= s @25fps` readout. Switching tabs (or toggling mode) reconfigures it; uses the existing `_sync_folder_field_to_tab`-style sync hook on tab change.
- `_on_export` / `_start_export_batch`: when the active tab is `ltx2`, pass `target_fps=25`, `snap32=True`, and frame-exact length to the ffmpeg builder; otherwise unchanged.
- The mismatch guardrail (just added) and per-tab folder continue to apply.

## Persistence & migration
`_mode` added to each tab's saved JSON (default `"foley"` when absent). No DB changes. Existing sessions load every tab as Foley → zero behavior change until a tab is converted.

## What this does NOT do
- No hard enforcement: you can set an illegal F or non-÷32 resize manually; the pipeline still crops to ÷32 and uses whatever F you pick (the *control* defaults/steps keep you legal, but nothing blocks you).
- No motion interpolation on fps resample (frame drop/dup only); keep sources native 25 fps where possible.
- No change to Foley exports, the scan pipeline, or the DB schema.
- No automatic re-export of existing clips into LTX-2 — you cut LTX-2 clips in the converted tab.
