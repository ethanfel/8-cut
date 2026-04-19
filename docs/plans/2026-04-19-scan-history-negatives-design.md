# Scan History & Hard Negative Management — Final Design

Date: 2026-04-19 (implemented on `feat/training-ui`)

## Goal

1. Keep scan result history per `(file, model)` so users can track classifier improvement across training iterations
2. Make hard negatives manageable — viewable, removable, and optionally disabled per training run
3. Fix latent bug: `get_export_folders()` doesn't filter by `scan_export`

---

## 1. Ghost Folder Fix

### Bug

`get_export_folders()` queried all `output_path` rows without filtering `scan_export`. Folders that only contained scan-exported clips appeared in training dropdowns with 0 clips.

### Implementation (`core/db.py`)

**`get_export_folders(profile, include_scan_exports=False)`** — new parameter. When `False` (default), the SQL query adds `AND scan_export = 0` to exclude scan-only folders. The `get_training_stats()` method passes this through and also filters its return dict to remove folders with 0 clips:

```python
return {k: v for k, v in stats.items() if v["clips"] > 0}
```

### Test

`tests/test_db.py::test_export_folders_excludes_scan_exports` — verifies scan-only folders are excluded by default and included when `include_scan_exports=True`.

---

## 2. Scan Result History

### Schema

Added column to `scan_results`:

```sql
scan_timestamp TEXT NOT NULL DEFAULT ''
```

All rows from the same scan share one timestamp string with **microsecond precision** (`%Y%m%d_%H%M%S_%f`, e.g. `"20260419_143022_123456"`). Microsecond precision prevents version collisions on fast successive scans.

Migration adds the column via `ALTER TABLE` for existing databases. Legacy rows keep `scan_timestamp = ''`.

### DB methods (`core/db.py`)

**`save_scan_results(filename, profile, model, regions, max_versions=5)`**
1. Inserts new rows with current microsecond-precision timestamp
2. Counts distinct timestamps for this `(filename, profile, model)`
3. Prunes oldest timestamps beyond `max_versions`

No more DELETE-then-INSERT — all versions coexist in the table.

**`get_scan_versions(filename, profile, model)`**
Returns `[{timestamp, count, max_score}, ...]` ordered newest first. Filters `scan_timestamp != ''` so legacy rows don't appear as named versions.

**`get_scan_results(filename, profile, scan_timestamp=None)`**
- With `scan_timestamp`: returns rows matching that exact version
- Without (default): uses `INNER JOIN` subquery with `MAX(scan_timestamp)` per model to return only the latest version. Legacy rows (empty timestamp) sort before any real timestamp, so they're returned when no versioned scans exist.

### UI (`main.py` — `ScanResultsPanel`)

Each model tab wraps its `QTableWidget` in a container `QWidget` with a `QComboBox` for version selection:

```
container (QWidget)
├── cmb_version (QComboBox) — hidden when ≤ 1 version
└── table (QTableWidget)
```

**Helper methods** unwrap this container:
- `_current_table()` — returns `QTableWidget` from active tab (handles both raw table and container)
- `_tab_table(index)` — same by tab index

**Version combo** is populated by `_populate_version_combos()` after every `load_for_file()` and `add_scan_results()` call. Labels use `datetime.strptime` parsing with try/except fallback for robustness:

```
2026-04-19 14:30 (12 regions, best: 0.95)
```

**Version switching** via `_on_version_changed(model, idx)`:
1. Reads `scan_timestamp` from combo's `userData`
2. Calls `get_scan_results(filename, profile, scan_timestamp=ts)`
3. Repopulates the table in-place
4. **Clears the undo stack** — stale undo entries from a different version would corrupt data
5. Emits `regions_edited` to refresh the timeline

**Tab switch** connects `tab_changed` signal to `_on_scan_regions_edited` (not just `_update_scan_export_count`), so the timeline updates scan regions when switching model tabs.

### Cache interaction

Embedding cache is per `(file, model)` and doesn't change across scans. History stores classified regions (start, end, score), not embeddings.

### Test

`tests/test_db.py::test_scan_result_history` — saves 3 versions, verifies counts, ordering, and latest-by-default behavior.

---

## 3. Hard Negative Management

### Schema

Added column to `hard_negatives`:

```sql
source_model TEXT NOT NULL DEFAULT ''
```

Migration adds the column via `ALTER TABLE` for existing databases.

### DB methods (`core/db.py`)

**`add_hard_negatives(filename, profile, times, source_path="", source_model="")`** — now stores which embedding model produced the scan that led to the negative marking.

**`get_hard_negatives(profile)`** — returns all rows as `[{id, filename, start_time, source_path, source_model}, ...]` for the management dialog.

**`delete_hard_negatives_by_ids(ids)`** — bulk delete by row IDs.

**`get_training_data(..., use_hard_negatives=True)`** — new parameter. When `False`, the hard negatives query is skipped entirely. Non-destructive — negatives remain in DB.

### Source model tracking (`main.py`)

`_on_scan_negatives()` now passes `source_model=self._scan_panel.current_model_name()` when marking negatives from scan results. `current_model_name()` extracts the model name from the active tab text (stripping the count suffix).

### Training toggle (`main.py` — `TrainDialog`)

Checkbox **"Use hard negatives in training"** (default checked) with "Manage..." button in an HBox layout. The toggle:
- Updates live training stats preview via debounced `_update_stats()`
- Passes `use_hard_negatives` through `_open_train_dialog()` to `get_training_data()`

### Management dialog (`main.py` — `HardNegativesDialog`)

Accessible from TrainDialog's "Manage..." button. Features:

| Component | Details |
|-----------|---------|
| **Filter combo** | `(all)` + each distinct `source_model` found in data |
| **Summary label** | `<b>N</b> hard negatives` |
| **Table** | File, Time (`{:.1f}s`), Source Model, hidden ID column |
| **Delete Selected** | Multi-select aware, skips hidden (filtered) rows |
| **Clear All** | **Filter-aware**: if a model filter is active, only deletes negatives for that model with an appropriate confirmation message. If `(all)`, deletes everything. |
| **Close** | Closes dialog, triggers stats refresh in parent TrainDialog |

`blockSignals(True)` guards prevent spurious filter callbacks during `_load()` repopulation.

### Tests

- `test_hard_negatives_source_model` — verifies source_model stored and retrieved
- `test_training_data_skips_hard_negatives` — verifies `use_hard_negatives=False` excludes them
- `test_delete_hard_negatives_by_ids` — verifies bulk deletion by ID

---

## 4. Runtime Fixes (discovered during testing)

### EAT/torchvision ABI mismatch

**Problem:** `torchvision` installed from PyPI (CPU build) was incompatible with `torch` from CUDA wheel index, causing `operator torchvision::nms does not exist`.

**Fix:** Added `torchvision` to the explicit torch install line in both setup scripts:
```bash
pip install torch torchaudio torchvision --index-url "$TORCH_INDEX"
```

Also added `--extra-index-url "$TORCH_INDEX"` to the `pip install -r requirements.txt` line to prevent transitive dependencies (timm, ultralytics) from pulling CPU-only torch packages.

Applied to: `setup_env.sh` (both conda and venv paths), `setup-windows.ps1`.

### EAT / transformers 5.x incompatibility

**Problem:** transformers 5.x broke EAT's remote model code (`'EATModel' object has no attribute 'all_tied_weights_keys'`).

**Fix:** Pinned `transformers>=4.30,<5.0` in `requirements.txt`.

### NumPy non-writable array warning

**Problem:** Cached HuBERT/EAT embeddings loaded from disk are read-only numpy arrays. `torch.from_numpy()` on a non-writable array triggers a deprecation warning.

**Fix:** In `core/audio_scan.py`, changed EAT preprocessing to copy the array:
```python
wav = torch.from_numpy(np.array(chunk)).unsqueeze(0).float()
```

### Timeline not updating on tab switch

**Problem:** Switching model tabs in the scan results panel didn't refresh the timeline's highlighted regions because `tab_changed` was only connected to `_update_scan_export_count`.

**Fix:** Connected `tab_changed` to `_on_scan_regions_edited` instead, which handles both timeline refresh and export count update.

---

## File Summary

| File | Changes |
|------|---------|
| `core/db.py` | Schema migrations, `get_export_folders` filter, versioned `save_scan_results`, `get_scan_versions`, version-aware `get_scan_results`, `add_hard_negatives` with `source_model`, `get_hard_negatives`, `delete_hard_negatives_by_ids`, `get_training_data` with `use_hard_negatives` |
| `main.py` | `HardNegativesDialog` class, `TrainDialog` hard neg toggle + manage button, `ScanResultsPanel` container/combo architecture, version combo population and switching, `current_model_name()`, tab-switch timeline fix |
| `core/audio_scan.py` | `np.array(chunk)` copy for read-only numpy arrays in EAT preprocessing |
| `requirements.txt` | `transformers>=4.30,<5.0` pin |
| `setup_env.sh` | `torchvision` in torch install, `--extra-index-url` on requirements install |
| `setup-windows.ps1` | `torchvision` in torch install, `--extra-index-url` on requirements install, removed skip-if-exists guard |
| `tests/test_db.py` | 5 tests covering all DB-layer changes |
