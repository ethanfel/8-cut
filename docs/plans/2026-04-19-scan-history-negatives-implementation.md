# Scan History & Hard Negative Management — Implementation Log

> All tasks complete. See the design doc for the final specification.

**Branch:** `feat/training-ui`

---

### Task 1: Fix ghost folder bug in get_export_folders -- DONE

**Commit:** `2614a76 fix: get_export_folders respects scan_export filter`

- `core/db.py` — `get_export_folders(profile, include_scan_exports=False)`: filters `scan_export = 0` by default
- `core/db.py` — `get_training_stats()`: passes `include_scan_exports` through, filters out 0-clip folders
- `tests/test_db.py` — `test_export_folders_excludes_scan_exports`

---

### Task 2: Scan result history — schema and DB methods -- DONE

**Commit:** `4fb2ae1 feat: scan result history — keep N versions per (file, model)`

- `core/db.py` — added `scan_timestamp TEXT NOT NULL DEFAULT ''` column with migration
- `core/db.py` — `save_scan_results()`: versioned insert with microsecond-precision timestamp (`%Y%m%d_%H%M%S_%f`), auto-prunes beyond `max_versions=5`
- `core/db.py` — `get_scan_versions()`: returns `[{timestamp, count, max_score}, ...]` newest first
- `core/db.py` — `get_scan_results(scan_timestamp=None)`: `INNER JOIN` subquery with `MAX(scan_timestamp)` for latest-by-default
- `tests/test_db.py` — `test_scan_result_history`

---

### Task 3: Scan history UI — version selector in ScanResultsPanel -- DONE

**Commit:** `8ed9fbf feat: scan version selector in results panel`

- `main.py` — `_add_tab()`: wraps table in container `QWidget` with version `QComboBox` (hidden when ≤ 1 version)
- `main.py` — `_current_table()` / `_tab_table(idx)`: unwrap container to get `QTableWidget`
- `main.py` — `_populate_version_combos()`: queries `get_scan_versions()`, formats labels with `datetime.strptime` + try/except fallback
- `main.py` — `_on_version_changed()`: reloads table from specific version, clears undo stack, emits `regions_edited`
- `main.py` — `current_model_name()`: extracts model name from tab text

---

### Task 4: Hard negatives — schema and training toggle -- DONE

**Commit:** `edc5784 feat: hard negative source_model tracking, training toggle`

- `core/db.py` — added `source_model TEXT NOT NULL DEFAULT ''` column to `hard_negatives` with migration
- `core/db.py` — `add_hard_negatives(source_model="")`: stores originating model
- `core/db.py` — `get_hard_negatives(profile)`: returns full rows as list of dicts
- `core/db.py` — `delete_hard_negatives_by_ids(ids)`: bulk delete by row IDs
- `core/db.py` — `get_training_data(use_hard_negatives=True)`: conditionally skips hard negatives query
- `main.py` — `TrainDialog`: "Use hard negatives" checkbox + "Manage..." button in HBox layout
- `main.py` — `_on_scan_negatives()`: passes `source_model=self._scan_panel.current_model_name()`
- `tests/test_db.py` — `test_hard_negatives_source_model`, `test_training_data_skips_hard_negatives`, `test_delete_hard_negatives_by_ids`

---

### Task 5: Hard negatives management dialog -- DONE

**Commit:** `e6db83f feat: hard negatives management dialog with filter and bulk delete`

- `main.py` — `HardNegativesDialog`: table with File/Time/Source Model/hidden ID columns, model filter combo, delete selected, filter-aware clear all, close button
- Filter-aware "Clear All": respects active model filter, shows appropriate confirmation message

---

### Task 6: Code review fixes -- DONE

**Commit:** `5d45b8d fix: timestamp collision, undo stack invalidation, label parsing, filter-aware clear`

Four issues found during code review:
1. **Timestamp collision** — second-precision timestamps could merge versions on sub-second calls. Fixed with microsecond precision `%f`
2. **Undo stack invalidation** — switching scan versions left stale undo entries. Fixed by clearing undo stack in `_on_version_changed()`
3. **Timestamp label fragile parsing** — hard-coded string slicing. Fixed with `datetime.strptime` + try/except fallback
4. **Clear All ignoring filter** — deleted all negatives regardless of model filter. Fixed to respect active filter

---

### Runtime fixes (discovered during manual testing)

| Commit | Fix |
|--------|-----|
| `a3c657c` | Install `torchvision` from CUDA wheel index (was pulling CPU build from PyPI) |
| `3c3b1d7` | Remove "skip if torch exists" guard in Windows setup so re-runs fix broken envs |
| `fd043f4` | Pin `transformers>=4.30,<5.0` — EAT remote model code incompatible with transformers 5.x |
| `7d6fee9` | Copy read-only numpy array before `torch.from_numpy()` in EAT preprocessing |
| `bd345ab` | Connect `tab_changed` to `_on_scan_regions_edited` so timeline refreshes on tab switch |
| `d8b3972` | Add `--extra-index-url` to `pip install -r requirements.txt` in both setup scripts |

---

### Test results

All 68 tests pass (5 new DB tests + 63 existing).
