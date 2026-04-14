# Keyframe Crop Modes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend crop keyframes to snapshot ratio and random crop flags, so different sub-clips in a batch inherit different crop settings based on timeline position.

**Architecture:** Widen the keyframe tuple from `(time, center)` to `(time, center, ratio, rand_portrait, rand_square)`. All existing keyframe code paths (set, delete, clear, render, apply-at-export, preview-on-scrub) are updated to carry and use the new fields. Diamond rendering on the timeline uses color to indicate which random flags are set.

**Tech Stack:** Python, PyQt6 (QPainter for diamonds)

---

### Task 1: Add a helper to resolve the effective keyframe at a given time

The keyframe lookup pattern (iterate sorted list, take latest where `kt <= t + 0.05`) is repeated 3 times in main.py. Extract it as a pure function so we can test it and reuse it cleanly with the new wider tuple.

**Files:**
- Modify: `main.py:48-53` (module-level functions area)
- Test: `tests/test_utils.py`

**Step 1: Write the failing tests**

Add to `tests/test_utils.py`:

```python
from main import resolve_keyframe

def test_resolve_keyframe_empty():
    assert resolve_keyframe([], 5.0) is None

def test_resolve_keyframe_before_first():
    kfs = [(3.0, 0.5, None, False, False)]
    assert resolve_keyframe(kfs, 1.0) is None

def test_resolve_keyframe_exact():
    kfs = [(2.0, 0.3, "9:16", True, False)]
    assert resolve_keyframe(kfs, 2.0) == (2.0, 0.3, "9:16", True, False)

def test_resolve_keyframe_between():
    kfs = [
        (1.0, 0.2, None, False, False),
        (5.0, 0.8, "1:1", False, True),
    ]
    assert resolve_keyframe(kfs, 3.0) == (1.0, 0.2, None, False, False)

def test_resolve_keyframe_after_last():
    kfs = [
        (1.0, 0.2, None, False, False),
        (5.0, 0.8, "1:1", False, True),
    ]
    assert resolve_keyframe(kfs, 10.0) == (5.0, 0.8, "1:1", False, True)

def test_resolve_keyframe_tolerance():
    kfs = [(4.0, 0.5, None, True, True)]
    # 4.0 <= 3.96 + 0.05 = 4.01, so it should match
    assert resolve_keyframe(kfs, 3.96) == (4.0, 0.5, None, True, True)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_utils.py -k resolve_keyframe -v`
Expected: FAIL (ImportError — function does not exist yet)

**Step 3: Write the implementation**

Add to `main.py` after the `format_time` function (around line 53):

```python
def resolve_keyframe(
    keyframes: list[tuple[float, float, str | None, bool, bool]],
    t: float,
    tolerance: float = 0.05,
) -> tuple[float, float, str | None, bool, bool] | None:
    """Return the latest keyframe at or before *t*, or None."""
    result = None
    for kf in keyframes:
        if kf[0] <= t + tolerance:
            result = kf
        else:
            break
    return result
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_utils.py -k resolve_keyframe -v`
Expected: 6 PASS

**Step 5: Commit**

```bash
git add main.py tests/test_utils.py
git commit -m "feat: add resolve_keyframe helper for widened keyframe tuples"
```

---

### Task 2: Widen keyframe tuple and update storage

Change `_crop_keyframes` from `list[tuple[float, float]]` to `list[tuple[float, float, str | None, bool, bool]]` in both `TimelineWidget` and `MainWindow`. Update `set_crop_keyframes` signature.

**Files:**
- Modify: `main.py:735` (TimelineWidget._crop_keyframes)
- Modify: `main.py:786-787` (TimelineWidget.set_crop_keyframes)
- Modify: `main.py:1755` (MainWindow._crop_keyframes)

**Step 1: Update TimelineWidget**

At line 735, change:
```python
self._crop_keyframes: list[tuple[float, float]] = []  # [(time, center)]
```
to:
```python
self._crop_keyframes: list[tuple[float, float, str | None, bool, bool]] = []
```

At lines 786-787, change:
```python
def set_crop_keyframes(self, kfs: list[tuple[float, float]]) -> None:
    self._crop_keyframes = kfs
```
to:
```python
def set_crop_keyframes(self, kfs: list[tuple[float, float, str | None, bool, bool]]) -> None:
    self._crop_keyframes = kfs
```

**Step 2: Update MainWindow**

At line 1755, change:
```python
self._crop_keyframes: list[tuple[float, float]] = []  # [(time, center), ...] sorted
```
to:
```python
self._crop_keyframes: list[tuple[float, float, str | None, bool, bool]] = []  # sorted by time
```

**Step 3: Run existing tests**

Run: `pytest tests/ -v`
Expected: All 46 + 6 new = 52 PASS (no existing tests touch keyframes directly)

**Step 4: Commit**

```bash
git add main.py
git commit -m "refactor: widen keyframe tuple to carry ratio and random flags"
```

---

### Task 3: Update keyframe creation to snapshot crop state

When the user clicks the crop bar in lock mode, snapshot the current ratio, rand_portrait, and rand_square into the keyframe.

**Files:**
- Modify: `main.py:2519-2538` (_on_crop_click lock-mode branch)

**Step 1: Update the keyframe creation code**

At lines 2525-2532, change:
```python
            self._crop_keyframes = [
                (t, c) for t, c in self._crop_keyframes
                if abs(t - play_t) > 0.05
            ]
            self._crop_keyframes.append((play_t, frac))
            self._crop_keyframes.sort()
            self._timeline.set_crop_keyframes(self._crop_keyframes)
            _log(f"Crop keyframe: t={play_t:.2f}s center={frac:.3f} ({len(self._crop_keyframes)} total)")
```
to:
```python
            ratio_text = self._cmb_portrait.currentText()
            kf_ratio = None if ratio_text == "Off" else ratio_text
            kf_rand_p = self._chk_rand_portrait.isChecked()
            kf_rand_s = self._chk_rand_square.isChecked()
            self._crop_keyframes = [
                kf for kf in self._crop_keyframes
                if abs(kf[0] - play_t) > 0.05
            ]
            self._crop_keyframes.append((play_t, frac, kf_ratio, kf_rand_p, kf_rand_s))
            self._crop_keyframes.sort()
            self._timeline.set_crop_keyframes(self._crop_keyframes)
            _log(f"Crop keyframe: t={play_t:.2f}s center={frac:.3f} ratio={kf_ratio} rp={kf_rand_p} rs={kf_rand_s} ({len(self._crop_keyframes)} total)")
```

**Step 2: Update keyframe deletion filter**

At lines 2356-2357, change:
```python
        self._crop_keyframes = [
            (t, c) for t, c in self._crop_keyframes
            if abs(t - time) > 0.05
        ]
```
to:
```python
        self._crop_keyframes = [
            kf for kf in self._crop_keyframes
            if abs(kf[0] - time) > 0.05
        ]
```

**Step 3: Run existing tests**

Run: `pytest tests/ -v`
Expected: All 52 PASS

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: snapshot ratio and random flags into crop keyframes"
```

---

### Task 4: Update export to apply full keyframe state

Replace the keyframe application loop and random crop logic in `_on_export` to use the new fields.

**Files:**
- Modify: `main.py:2754-2782` (keyframe application + random crop logic in _on_export)
- Test: `tests/test_utils.py`

**Step 1: Write a test for the export keyframe resolution logic**

Add to `tests/test_utils.py`:

```python
from main import apply_keyframes_to_jobs

def test_apply_keyframes_no_keyframes():
    jobs = [(0.0, "/out/a", None, 0.5), (3.0, "/out/b", None, 0.5)]
    result = apply_keyframes_to_jobs(jobs, [], base_center=0.5, base_ratio=None,
                                     base_rand_p=True, base_rand_s=False)
    # No keyframes: jobs get base values; rand flags come from base
    assert result == [
        (0.0, "/out/a", None, 0.5, True, False),
        (3.0, "/out/b", None, 0.5, True, False),
    ]

def test_apply_keyframes_with_keyframes():
    kfs = [
        (0.0, 0.3, "9:16", True, False),
        (4.0, 0.7, None, False, True),
    ]
    jobs = [
        (0.0, "/out/a", None, 0.5),
        (3.0, "/out/b", None, 0.5),
        (6.0, "/out/c", None, 0.5),
    ]
    result = apply_keyframes_to_jobs(jobs, kfs, base_center=0.5, base_ratio=None,
                                     base_rand_p=False, base_rand_s=False)
    assert result == [
        (0.0, "/out/a", "9:16", 0.3, True, False),
        (3.0, "/out/b", "9:16", 0.3, True, False),
        (6.0, "/out/c", None, 0.7, False, True),
    ]

def test_apply_keyframes_before_first_uses_base():
    kfs = [(5.0, 0.8, "1:1", False, True)]
    jobs = [(1.0, "/out/a", None, 0.5)]
    result = apply_keyframes_to_jobs(jobs, kfs, base_center=0.5, base_ratio="4:5",
                                     base_rand_p=True, base_rand_s=False)
    assert result == [(1.0, "/out/a", "4:5", 0.5, True, False)]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_utils.py -k apply_keyframes -v`
Expected: FAIL (ImportError)

**Step 3: Write the apply_keyframes_to_jobs function**

Add to `main.py` after `resolve_keyframe`:

```python
def apply_keyframes_to_jobs(
    jobs: list[tuple[float, str, str | None, float]],
    keyframes: list[tuple[float, float, str | None, bool, bool]],
    base_center: float,
    base_ratio: str | None,
    base_rand_p: bool,
    base_rand_s: bool,
) -> list[tuple[float, str, str | None, float, bool, bool]]:
    """Resolve each job's crop state from keyframes, returning widened tuples.

    Returns list of (start, path, ratio, center, rand_portrait, rand_square).
    """
    result = []
    for s, o, _r, _c in jobs:
        kf = resolve_keyframe(keyframes, s)
        if kf is not None:
            _, center, ratio, rp, rs = kf
        else:
            center, ratio, rp, rs = base_center, base_ratio, base_rand_p, base_rand_s
        result.append((s, o, ratio, center, rp, rs))
    return result
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_utils.py -k apply_keyframes -v`
Expected: 3 PASS

**Step 5: Update _on_export to use the new functions**

Replace lines 2754-2782 (the keyframe application block + random crop block) with:

```python
            # Apply crop keyframes (or fall back to base state).
            rand_portrait = self._chk_rand_portrait.isChecked()
            rand_square = self._chk_rand_square.isChecked()
            widened = apply_keyframes_to_jobs(
                jobs, self._crop_keyframes,
                base_center=base_center, base_ratio=base_ratio,
                base_rand_p=rand_portrait, base_rand_s=rand_square,
            )

            # Random crop: for each clip whose effective flags are set,
            # ~1 in 3 gets a random ratio applied.
            final_jobs = []
            # Collect indices eligible for random crop, grouped by flag combo.
            portrait_eligible = [i for i, w in enumerate(widened) if w[4]]
            square_eligible = [i for i, w in enumerate(widened) if w[5]]
            rand_indices: set[int] = set()
            if portrait_eligible and n_clips > 1:
                n = max(1, len(portrait_eligible) // 3)
                rand_indices.update(random.sample(portrait_eligible, min(n, len(portrait_eligible))))
            if square_eligible and n_clips > 1:
                n = max(1, len(square_eligible) // 3)
                rand_indices.update(random.sample(square_eligible, min(n, len(square_eligible))))

            for i, (s, o, ratio, center, rp, rs) in enumerate(widened):
                if i in rand_indices:
                    pool = []
                    if rp:
                        pool.append("9:16")
                    if rs:
                        pool.append("1:1")
                    if pool:
                        ratio = random.choice(pool)
                jobs.append((s, o, ratio, center))

            # Replace jobs with the resolved list.
            jobs = jobs[n_clips:]  # drop the original entries, keep the new ones
```

Note: `jobs` was built with `n_clips` entries in the loop above. We append resolved entries and then slice off the originals.

Actually, a cleaner rewrite of the tail — replace the entire block from the keyframe comment through the random crop block with:

```python
            # Apply crop keyframes (or fall back to base state).
            rand_portrait = self._chk_rand_portrait.isChecked()
            rand_square = self._chk_rand_square.isChecked()
            widened = apply_keyframes_to_jobs(
                jobs, self._crop_keyframes,
                base_center=base_center, base_ratio=base_ratio,
                base_rand_p=rand_portrait, base_rand_s=rand_square,
            )

            # Random crop: eligible clips (per their keyframe flags) have
            # ~1 in 3 chance of getting a random ratio applied.
            portrait_eligible = [i for i, w in enumerate(widened) if w[4]]
            square_eligible = [i for i, w in enumerate(widened) if w[5]]
            rand_indices: dict[int, list[str]] = {}
            if portrait_eligible and n_clips > 1:
                n = max(1, len(portrait_eligible) // 3)
                for i in random.sample(portrait_eligible, min(n, len(portrait_eligible))):
                    rand_indices.setdefault(i, []).append("9:16")
            if square_eligible and n_clips > 1:
                n = max(1, len(square_eligible) // 3)
                for i in random.sample(square_eligible, min(n, len(square_eligible))):
                    rand_indices.setdefault(i, []).append("1:1")

            jobs = []
            for i, (s, o, ratio, center, _rp, _rs) in enumerate(widened):
                if i in rand_indices:
                    ratio = random.choice(rand_indices[i])
                jobs.append((s, o, ratio, center))
```

**Step 6: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 7: Commit**

```bash
git add main.py tests/test_utils.py
git commit -m "feat: apply keyframe crop modes during export"
```

---

### Task 5: Update diamond rendering with color coding

Color-code timeline keyframe diamonds based on their random flags.

**Files:**
- Modify: `main.py:898-910` (TimelineWidget.paintEvent keyframe diamond section)

**Step 1: Replace the diamond rendering block**

Replace lines 898-910:
```python
            # ── crop keyframe diamonds ────────────────────────────────────
            if self._crop_keyframes and self._duration > 0:
                for (kt, _kc) in self._crop_keyframes:
                    kx = int(kt / self._duration * w)
                    d = 4  # half-size of diamond
                    ky = h - d - 2  # near bottom of track
                    diamond = QPolygon([
                        QPoint(kx, ky - d), QPoint(kx + d, ky),
                        QPoint(kx, ky + d), QPoint(kx - d, ky),
                    ])
                    p.setBrush(QColor(255, 180, 0))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawPolygon(diamond)
```

with:
```python
            # ── crop keyframe diamonds ────────────────────────────────────
            if self._crop_keyframes and self._duration > 0:
                _KF_GOLD = QColor(255, 180, 0)
                _KF_RED = QColor(220, 60, 60)
                _KF_BLUE = QColor(60, 180, 220)
                for kf in self._crop_keyframes:
                    kt = kf[0]
                    rp = kf[3] if len(kf) > 3 else False
                    rs = kf[4] if len(kf) > 4 else False
                    kx = int(kt / self._duration * w)
                    d = 4  # half-size of diamond
                    ky = h - d - 2  # near bottom of track
                    if rp and rs:
                        # Split diamond: left half red, right half blue
                        left = QPolygon([
                            QPoint(kx, ky - d), QPoint(kx, ky + d),
                            QPoint(kx - d, ky),
                        ])
                        right = QPolygon([
                            QPoint(kx, ky - d), QPoint(kx + d, ky),
                            QPoint(kx, ky + d),
                        ])
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(_KF_RED)
                        p.drawPolygon(left)
                        p.setBrush(_KF_BLUE)
                        p.drawPolygon(right)
                    else:
                        diamond = QPolygon([
                            QPoint(kx, ky - d), QPoint(kx + d, ky),
                            QPoint(kx, ky + d), QPoint(kx - d, ky),
                        ])
                        if rp:
                            color = _KF_RED
                        elif rs:
                            color = _KF_BLUE
                        else:
                            color = _KF_GOLD
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(color)
                        p.drawPolygon(diamond)
```

**Step 2: Update the context menu keyframe hit detection**

At line 980, change:
```python
        for (kt, _kc) in self._crop_keyframes:
```
to:
```python
        for kf in self._crop_keyframes:
            kt = kf[0]
```

And remove the `_kc` reference — use `kf[0]` for `kt` only. The rest of the hit-detection logic stays the same.

**Step 3: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 4: Manual test**

Launch the app, load a video, enable lock mode, set keyframes with different combinations of random portrait/square. Verify:
- Gold diamond when no random flags set
- Red diamond when only portrait
- Blue diamond when only square
- Split red/blue when both

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: color-code keyframe diamonds by crop mode"
```

---

### Task 6: Update lock-mode scrub preview

When scrubbing in lock mode, update the crop bar, overlay, and (visually) the random checkboxes to reflect the effective keyframe state at the playback position.

**Files:**
- Modify: `main.py:2605-2621` (_on_seek_changed)

**Step 1: Replace the keyframe preview block**

Replace lines 2610-2621:
```python
        if self._crop_keyframes:
            center = self._crop_center
            for kt, kc in self._crop_keyframes:
                if kt <= t + 0.05:
                    center = kc
                else:
                    break
            self._crop_bar.set_crop_center(center)
            ratio = self._cmb_portrait.currentText()
            if ratio != "Off":
                self._mpv.set_crop_overlay(_RATIOS[ratio], center)
```

with:
```python
        if self._crop_keyframes:
            kf = resolve_keyframe(self._crop_keyframes, t)
            if kf is not None:
                _, center, ratio, rp, rs = kf
                self._crop_bar.set_crop_center(center)
                if ratio is not None:
                    self._mpv.set_crop_overlay(_RATIOS[ratio], center)
                else:
                    self._update_rand_overlays()
```

**Step 2: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: preview effective keyframe crop state during lock-mode scrub"
```

---

### Task 7: Update overwrite-mode keyframe application

The overwrite path (lines 2727-2738) also builds jobs. It doesn't currently apply keyframes, but should for consistency.

**Files:**
- Modify: `main.py:2727-2738` (overwrite branch in _on_export)

**Step 1: Check and update**

After the overwrite jobs are built, apply the same `apply_keyframes_to_jobs` logic if keyframes exist. The overwrite branch builds `jobs` as `(start, path, base_ratio, base_center)` — same shape as the normal path.

Add after line 2738 (`self._overwrite_group = []`):

```python
            rand_portrait = self._chk_rand_portrait.isChecked()
            rand_square = self._chk_rand_square.isChecked()
            if self._crop_keyframes:
                widened = apply_keyframes_to_jobs(
                    jobs, self._crop_keyframes,
                    base_center=base_center, base_ratio=base_ratio,
                    base_rand_p=rand_portrait, base_rand_s=rand_square,
                )
                jobs = [(s, o, r, c) for s, o, r, c, _rp, _rs in widened]
```

**Step 2: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: apply keyframe crop modes in overwrite exports too"
```

---

### Task 8: Update import in test file and final validation

**Files:**
- Modify: `tests/test_utils.py:2` (import line)

**Step 1: Update imports**

At line 2, add the new functions to the import:
```python
from main import build_export_path, format_time, build_ffmpeg_command, build_sequence_dir, build_audio_extract_command, build_annotation_json_path, upsert_clip_annotation, resolve_keyframe, apply_keyframes_to_jobs
```

(This should already be done incrementally in Tasks 1 and 4, but verify it's correct.)

**Step 2: Run full test suite**

Run: `pytest tests/ -v`
Expected: All 55 tests PASS (46 original + 6 resolve_keyframe + 3 apply_keyframes)

**Step 3: Manual integration test**

1. Launch `python main.py`, load a video
2. Enable lock mode (G or click lock button)
3. Scrub to a position, enable "1 random portrait", click crop bar → red diamond appears
4. Scrub forward, disable portrait, enable "1 random square", click crop bar → blue diamond appears
5. Scrub forward, enable both, click crop bar → split red/blue diamond
6. Set clip count to 6+, spread to 2s, export
7. Verify that sub-clips falling in each keyframe region get the correct random crop behavior
8. Right-click a diamond to delete it — verify it disappears

**Step 4: Commit**

```bash
git add tests/test_utils.py
git commit -m "test: verify imports for keyframe crop mode helpers"
```
