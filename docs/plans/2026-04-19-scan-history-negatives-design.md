# Scan History & Hard Negative Management Design

Date: 2026-04-19

## Goal

1. Keep scan result history per `(file, model)` so users can track classifier improvement across training iterations
2. Make hard negatives manageable — viewable, removable, and optionally disabled per training run
3. Fix latent bug: `get_export_folders()` doesn't filter by `scan_export`

## 1. Scan Result History

### Current behavior

`save_scan_results()` **replaces** all results for `(filename, profile, model)` on every scan. No history is preserved.

### Change

Keep the last N scan results per `(filename, profile, model)` with timestamps. The most recent is the "active" result displayed in the panel; older versions are accessible for comparison.

### Schema change

Add column to `scan_results`:

```sql
ALTER TABLE scan_results ADD COLUMN scan_timestamp TEXT NOT NULL DEFAULT '';
```

All rows from the same scan share the same timestamp string (e.g. `"20260419_143022"`).

### save_scan_results changes

Instead of `DELETE ... WHERE filename=? AND profile=? AND model=?`, the new flow:

1. Insert new rows with current timestamp
2. Count distinct timestamps for this `(filename, profile, model)`
3. If count > N (default 5), delete rows belonging to the oldest timestamps

### UI changes

Add a small version dropdown/selector in `ScanResultsPanel` per model tab — shows timestamps of available scan versions. Selecting a version loads that version's results into the tab. The most recent is selected by default.

The tab label shows the active version's region count, e.g. `HUBERT_XLARGE (12) [v3]`.

### Cache interaction

Embedding cache is per `(file, model)` and doesn't change across scans. Only the classifier output changes. History stores the classified regions (start, end, score), not embeddings.

## 2. Hard Negative Management

### Current behavior

- Hard negatives stored in `hard_negatives` table: `(filename, profile, start_time, source_path)`
- No model column — applied globally within a profile
- Removable one-by-one via N toggle in scan panel, but no bulk management
- Always used in training — no way to disable

### Changes

#### Schema

Add `source_model TEXT NOT NULL DEFAULT ''` column to `hard_negatives`. Populated when marking negatives from scan results (we know which model tab is active).

#### Training toggle

New checkbox in `TrainDialog`: **"Use hard negatives"** (default checked). When unchecked, `get_training_data()` skips the `hard_negatives` query entirely. Non-destructive — negatives remain in DB.

#### Management dialog

New `HardNegativesDialog` accessible from Train dialog via "Manage..." button next to the checkbox. Shows:

- Table: filename, start time, source model, date added (if we add created_at)
- Filter by source model (dropdown)
- Multi-select + Delete button
- "Clear All" button with confirmation
- Count summary at top

### Training integration

`get_training_data()` gets a new `use_hard_negatives: bool = True` parameter. When False, the hard negatives query (lines 365-374 of db.py) is skipped entirely.

## 3. Ghost Folder Fix

### Bug

`get_export_folders()` queries all `output_path` rows without filtering `scan_export`. Folders that only contain scan-exported clips appear in training dropdowns with 0 clips.

### Fix

Add `include_scan_exports` parameter to `get_export_folders()`. When False (default), only query rows with `scan_export = 0`. Also filter out folders with 0 clips from `get_training_stats()` result dict.
