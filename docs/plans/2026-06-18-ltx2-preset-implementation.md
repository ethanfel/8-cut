# LTX-2 per-tab export mode — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a per-tab export pipeline mode (Foley | LTX-2) so the same videos can feed both an 8 s Foley dataset and a frame-exact, ÷32, 25 fps LTX-2 V2A dataset, with a "Duplicate as LTX-2" tab action.

**Architecture:** `core/ffmpeg.build_ffmpeg_command` gains optional `target_fps` / `snap32` / `frames` params (Foley path unchanged); a tiny `core/ltx2.py` holds the legal-frame math. `PlaylistWidget` gains `_mode`; the tab menu gains duplicate/convert actions; the length control + `_on_export` wiring switch on the active tab's mode. Soft preset — defaults are legal, everything stays editable.

**Tech Stack:** Python 3.11+, PyQt6, ffmpeg, pytest. Branch `ltx2-preset` (based on `tab-export-folder`). Design: `docs/plans/2026-06-18-ltx2-preset-design.md`.

---

## Conventions
- **Core (`core/ffmpeg.py`, `core/ltx2.py`) is real TDD** — pure functions tested in `tests/test_utils.py` style. Run: `LD_PRELOAD=/usr/lib/libstdc++.so.6 python -m pytest tests/test_utils.py -q` (the preload is needed because importing `main` pulls `mpv`; see `project_qt_test_env`). 3 pre-existing failures there are unrelated — don't count them.
- **GUI parts** verified by the offscreen structure test (`LD_PRELOAD=/usr/lib/libstdc++.so.6 QT_QPA_PLATFORM=offscreen python -m pytest tests/test_ui_structure.py -v`) plus a **manual launch** (`./8cut.sh`).
- Line numbers are starting anchors; locate by symbol. Commit per task. Co-author trailer on every commit:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

## Stage 1 — LTX-2 math (`core/ltx2.py`) [TDD]

### Task 1.1: legal-frame helpers
**Files:** Create `core/ltx2.py`; Test in `tests/test_utils.py` (append).

**Step 1 — failing tests** (append to `tests/test_utils.py`):
```python
from core.ltx2 import is_legal_frames, nearest_legal_frames, frames_for_duration, duration_for_frames, legal_frames

def test_ltx2_is_legal():
    assert is_legal_frames(201) and is_legal_frames(9) and is_legal_frames(25)
    assert not is_legal_frames(200) and not is_legal_frames(8)

def test_ltx2_nearest():
    assert nearest_legal_frames(200) == 201   # 200 -> nearest 8k+1
    assert nearest_legal_frames(196) == 193
    assert nearest_legal_frames(5) == 9        # floor at 9

def test_ltx2_duration_roundtrip():
    assert duration_for_frames(201, 25) == 201 / 25
    assert frames_for_duration(8.0, 25) == 201   # 200 -> 201

def test_ltx2_legal_series():
    s = legal_frames(min_f=9, max_f=33)
    assert s == [9, 17, 25, 33]
```
**Step 2 — run, expect ImportError/FAIL:** `LD_PRELOAD=/usr/lib/libstdc++.so.6 python -m pytest tests/test_utils.py -k ltx2 -q`

**Step 3 — implement `core/ltx2.py`:**
```python
"""LTX-2 frame-count math. Legal F satisfy F % 8 == 1 (8x temporal + 1)."""

def is_legal_frames(f: int) -> bool:
    return f >= 9 and f % 8 == 1

def legal_frames(min_f: int = 9, max_f: int = 1000) -> list[int]:
    start = max(9, min_f + ((1 - min_f) % 8))   # first 8k+1 >= min_f
    return list(range(start, max_f + 1, 8))

def nearest_legal_frames(f: int) -> int:
    if f <= 9:
        return 9
    low = ((f - 1) // 8) * 8 + 1
    high = low + 8
    return low if (f - low) <= (high - f) else high

def duration_for_frames(frames: int, fps: float) -> float:
    return frames / fps

def frames_for_duration(duration: float, fps: float) -> int:
    return nearest_legal_frames(round(duration * fps))
```
**Step 4 — run, expect PASS** (same command). **Step 5 — commit:** `feat: LTX-2 legal-frame helpers (core/ltx2.py)`.

---

## Stage 2 — ffmpeg pipeline params [TDD]

### Task 2.1: `target_fps`, `snap32`, `frames` in `build_ffmpeg_command`
**Files:** Modify `core/ffmpeg.py:74` (`build_ffmpeg_command`); Test `tests/test_utils.py`.

**Step 1 — failing tests:**
```python
def test_ffmpeg_ltx2_fps_and_frames():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/c.mp4",
                               short_side=512, target_fps=25, frames=201)
    assert "-r" in cmd and cmd[cmd.index("-r")+1] == "25"
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v")+1] == "201"
    vf = cmd[cmd.index("-vf")+1]
    assert "fps=25" in vf

def test_ffmpeg_ltx2_snap32_crop():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/c.mp4",
                               short_side=512, snap32=True)
    vf = cmd[cmd.index("-vf")+1]
    assert "crop=trunc(iw/32)*32:trunc(ih/32)*32" in vf

def test_ffmpeg_foley_unchanged():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/c.mp4", short_side=256)
    assert "-r" not in cmd and "-frames:v" not in cmd
    assert "crop=trunc" not in cmd[cmd.index("-vf")+1]
```
**Step 2 — run, expect FAIL** (unexpected kwargs). 

**Step 3 — implement:** add params `target_fps: float | None = None, snap32: bool = False, frames: int | None = None` to the signature. After the scale filter (and before the VAAPI block), append:
```python
    if snap32:
        filters.append("crop=trunc(iw/32)*32:trunc(ih/32)*32")
    if target_fps is not None:
        filters.append(f"fps={target_fps:g}")
```
Add output flags: after `-t duration` (or near the encoder args, before `output_path`), when `target_fps` set add `cmd += ["-r", f"{target_fps:g}"]`; when `frames` set add `cmd += ["-frames:v", str(frames)]` (video frame cap — exact F). Ensure ordering keeps `-vf` before outputs. Keep `fps`/`snap32` filters out of the `image_sequence=False` vs `True` branches consistently (they apply to both; webp seq also benefits from fps/÷32).

**Step 4 — run, expect PASS.** Also run full `tests/test_utils.py` (the 3 pre-existing failures only). **Step 5 — commit:** `feat: LTX-2 ffmpeg params (target_fps, snap32, frames)`.

### Task 2.2: audio extract honors frame-exact duration
**Files:** `core/ffmpeg.py:145` (`build_audio_extract_command`) — confirm it takes a duration; if it derives from a fixed 8 s, add a `duration` param so the `.wav` for an LTX-2 webp sequence is exactly `F/25 s`. Add a test mirroring `test_audio_extract_timing` asserting the `-t` value equals `frames/fps`. Commit: `fix: audio extract duration for LTX-2 frame-exact clips`.

---

## Stage 3 — per-tab `_mode`

### Task 3.1: attribute + persistence + migration
**Files:** `main.py` — `PlaylistWidget.__init__` (~3409, next to `_dest_folder`); `_save_playlist_tabs` (~5271); `_load_playlist_tabs` (~5315).
- Add `self._mode: str = "foley"` in `PlaylistWidget.__init__`.
- `_save_playlist_tabs`: add `"mode": pw._mode` to each tab dict.
- `_load_playlist_tabs`: after creating each pw, `pw._mode = t.get("mode", "foley")`.
- `_add_playlist_tab`: new tabs default `_mode="foley"` (already via init).

**Verify:** structure test passes; add `test_tab_mode_defaults_foley` (construct, assert each `_pws[i]._mode == "foley"`). Commit: `feat: per-tab export mode attribute (foley default)`.

---

## Stage 4 — tab menu: duplicate / convert / toggle

### Task 4.1: menu actions + label badge
**Files:** `main.py` — `_PlaylistTabBar.contextMenuEvent` (~3300) add items; new handlers in `MainWindow`; tab-title rendering.
- Add to the tab context menu: **"Duplicate tab"**, **"Duplicate as LTX-2"**, and a checkable **"LTX-2 mode"** (checked when `pw._mode=="ltx2"`). Emit new signals (e.g. `duplicate_requested(idx, as_ltx2: bool)`, `mode_toggle_requested(idx)`) like the existing `pin_toggle_requested`.
- `MainWindow._on_duplicate_tab(idx, as_ltx2)`: build a new tab via `_add_playlist_tab(label=…, files=list(src._paths), separators=sorted(src._separators_before), select=True)`; set `pw._dest_folder = src._dest_folder + ("_ltx2" if as_ltx2 else "")`; `pw._mode = "ltx2" if as_ltx2 else src._mode`; if ltx2, apply LTX-2 defaults (Stage 5 hook); `_save_playlist_tabs()`; refresh.
- `MainWindow._on_tab_mode_toggle(idx)`: flip `pw._mode`; if now ltx2, apply LTX-2 defaults; `_save_playlist_tabs()`; re-sync controls (Stage 5).
- Label badge: when adding/refreshing a tab whose `_mode=="ltx2"`, show `f"{label} [LTX2]"` (or set a distinct color) — apply in `_refresh_layout`/`_add_playlist_tab` title set.

**Verify:** manual launch — right-click a tab → Duplicate as LTX-2 creates a `[LTX2]` tab with `_ltx2` folder; toggle works. Structure test still green. Commit: `feat: tab duplicate / Duplicate-as-LTX-2 / mode toggle + [LTX2] badge`.

---

## Stage 5 — length control swap + export wiring

### Task 5.1: length control reflects active tab mode
**Files:** `main.py` — the clip-length widgets (`_spn_clip_dur` ~4051 area) + the tab-change sync hook (`_on_tab_changed` / `_sync_folder_field_to_tab` neighbor).
- Add a frames spinbox `_spn_frames` (min 9, singleStep 8 → always 8k+1; suffix " f"; tooltip live `= F/25 s`). Default 201.
- Add `_apply_mode_to_controls()`: if active tab `ltx2` → show `_spn_frames` (+ "Frames" label), hide the seconds Duration control, default resize 512 if unset; else show Duration (seconds), hide frames. Call it from `_on_tab_changed`, after `_on_duplicate_tab`/`_on_tab_mode_toggle`, and once after `_load_playlist_tabs`.
- A small label shows `= {F/25:.2f}s @25fps` updating on `_spn_frames.valueChanged`.

### Task 5.2: route LTX-2 params through export
**Files:** `main.py` — `_on_export` (~7317) + `ExportWorker` construction (~7484) + `_update_next_label`.
- When the active tab's `_mode=="ltx2"`: compute `frames = self._spn_frames.value()`; `fps = 25`; `duration = frames / fps`; pass `target_fps=25, snap32=True, frames=frames, duration=duration` through to `ExportWorker` → `build_ffmpeg_command`. Default `short_side` to 512 if 0/None in ltx2.
- Foley path: unchanged (no new params).
- `ExportWorker.__init__`/`run`: thread the new params (default None/False) into `build_ffmpeg_command`.

**Verify (manual, authoritative):** in an LTX-2 tab, export → inspect an output clip: `ffprobe` shows **25 fps, exactly F frames, W&H ÷32**; a Foley tab still exports 8 s/source-fps unchanged. Structure test green; full `pytest tests/test_utils.py` (3 pre-existing fails only). Commit: `feat: route LTX-2 (25fps, ÷32 crop, F frames) through export for ltx2 tabs`.

---

## Stage 6 — finalize
- **Task 6.1:** Full regression — `pytest tests/test_ui_structure.py` + `tests/test_utils.py` separately; manual: Foley export unchanged, LTX-2 export legal (ffprobe), duplicate/convert, persistence across relaunch, guardrail + per-tab folder still work.
- **Task 6.2:** Changelog (`main.py` CHANGELOG, bump APP_VERSION) + README note (per-tab LTX-2 mode). Commit `docs: changelog + README for LTX-2 export mode`.
- **Task 6.3:** Hand off branch (depends on `tab-export-folder`; merge that first, then this).

## Risks
| Risk | Mitigation |
|------|-----------|
| `-frames:v` vs `-t` interaction yields F±1 frames | Set both `-t F/fps` and `-frames:v F`; verify exact count with ffprobe in 5.2. |
| `fps` filter + HW (VAAPI) filter ordering | Place `fps`/`snap32` among CPU filters before the VAAPI hwupload block; test a HW-encoder build if available. |
| Length-control swap leaves stale state across tab switches | `_apply_mode_to_controls()` called on every tab change + mode toggle + load. |
| Depends on unmerged `tab-export-folder` | Branch is based on it; land that branch first. |

## NOT in scope
Hard enforcement (illegal F/resize allowed manually), motion-interpolated fps, auto re-export of existing Foley clips, DB schema changes, scan-pipeline changes.
