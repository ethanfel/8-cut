# Scan History & Hard Negative Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add scan result versioning, hard negative management dialog with training toggle, and fix ghost folder bug.

**Architecture:** DB schema changes in `core/db.py` (new columns, new queries). UI changes in `main.py` (version selector in ScanResultsPanel, management dialog, training toggle). No changes to `core/audio_scan.py`.

**Tech Stack:** SQLite (existing), PyQt6 (existing)

**Key design notes:**
- Scan history stores N versions per `(filename, profile, model)` using a `scan_timestamp` column. All rows from one scan share the same timestamp.
- Hard negatives gain a `source_model` column (informational) and training gains a `use_hard_negatives` toggle.
- `get_export_folders()` must respect `scan_export` filter to prevent ghost folders.

---

### Task 1: Fix ghost folder bug in get_export_folders

**Files:**
- Modify: `core/db.py:294-313` (get_export_folders)
- Modify: `core/db.py:410-443` (get_training_stats — filter out 0-clip folders)
- Test: `tests/test_db.py`

**Step 1: Write failing test**

```python
def test_export_folders_excludes_scan_exports():
    """Scan-export-only folders should not appear when include_scan_exports=False."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        # Manual export
        db.add("a.mp4", 10.0, "/out/mp4_Intense/g1/clip.mp4", profile="test")
        # Scan export to different folder
        db.add("a.mp4", 20.0, "/out/mp4_ScanOnly/g1/clip.mp4", profile="test",
               scan_export=True)
        folders = db.get_export_folders("test")
        assert "mp4_Intense" in folders
        assert "mp4_ScanOnly" not in folders, "scan-only folder should be excluded"
        # With include_scan_exports=True, both should appear
        folders_all = db.get_export_folders("test", include_scan_exports=True)
        assert "mp4_ScanOnly" in folders_all
    finally:
        os.unlink(path)
```

**Step 2: Fix get_export_folders**

Add `include_scan_exports` parameter:

```python
def get_export_folders(self, profile: str = "default",
                       include_scan_exports: bool = False) -> list[str]:
    if not self._enabled:
        return []
    if include_scan_exports:
        rows = self._con.execute(
            "SELECT DISTINCT output_path FROM processed WHERE profile = ?",
            (profile,),
        ).fetchall()
    else:
        rows = self._con.execute(
            "SELECT DISTINCT output_path FROM processed"
            " WHERE profile = ? AND scan_export = 0",
            (profile,),
        ).fetchall()
    folder_names: set[str] = set()
    for (op,) in rows:
        grandparent = os.path.basename(os.path.dirname(os.path.dirname(op)))
        if grandparent:
            folder_names.add(grandparent)
    return sorted(folder_names)
```

**Step 3: Update get_training_stats to pass through**

```python
    folders = self.get_export_folders(profile, include_scan_exports=include_scan_exports)
```

And filter out empty folders at the end:

```python
    return {k: v for k, v in stats.items() if v["clips"] > 0}
```

**Step 4: Run tests, commit**

```bash
pytest tests/ -v
git add core/db.py tests/test_db.py
git commit -m "fix: get_export_folders respects scan_export filter"
```

---

### Task 2: Scan result history — schema and DB methods

**Files:**
- Modify: `core/db.py:86-98` (scan_results schema — add scan_timestamp column)
- Modify: `core/db.py:100-113` (migration — add scan_timestamp to existing tables)
- Modify: `core/db.py:447-468` (save_scan_results — version management)
- Add: `core/db.py` (get_scan_versions, load_scan_version, delete_scan_version)
- Test: `tests/test_db.py`

**Step 1: Write failing test**

```python
def test_scan_result_history():
    """save_scan_results should keep multiple versions."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        # Save three versions
        db.save_scan_results("v.mp4", "test", "MODEL_A",
                             [(0, 8, 0.9)])
        db.save_scan_results("v.mp4", "test", "MODEL_A",
                             [(0, 8, 0.8), (10, 18, 0.7)])
        db.save_scan_results("v.mp4", "test", "MODEL_A",
                             [(5, 13, 0.95)])
        versions = db.get_scan_versions("v.mp4", "test", "MODEL_A")
        assert len(versions) == 3
        # Most recent first
        assert versions[0]["count"] == 1   # latest: 1 region
        assert versions[1]["count"] == 2   # middle: 2 regions
        assert versions[2]["count"] == 1   # oldest: 1 region
        # get_scan_results returns latest version by default
        results = db.get_scan_results("v.mp4", "test")
        assert len(results.get("MODEL_A", [])) == 1
    finally:
        os.unlink(path)
```

**Step 2: Add scan_timestamp column**

In the CREATE TABLE (line 87-98), add:

```sql
  scan_timestamp  TEXT NOT NULL DEFAULT ''
```

In the migration block (lines 100-113), add:

```python
        ("scan_timestamp", "TEXT NOT NULL DEFAULT ''"),
```

**Step 3: Modify save_scan_results**

Replace the current DELETE+INSERT with versioned insert + cleanup:

```python
def save_scan_results(self, filename: str, profile: str, model: str,
                      regions: list[tuple[float, float, float]],
                      max_versions: int = 5) -> None:
    if not self._enabled:
        return
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with self._lock:
        self._con.executemany(
            "INSERT INTO scan_results"
            " (filename, profile, model, start_time, end_time, score,"
            "  orig_start_time, orig_end_time, scan_timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(filename, profile, model, s, e, sc, s, e, ts)
             for s, e, sc in regions],
        )
        # Prune old versions beyond max_versions
        versions = self._con.execute(
            "SELECT DISTINCT scan_timestamp FROM scan_results"
            " WHERE filename = ? AND profile = ? AND model = ?"
            " ORDER BY scan_timestamp DESC",
            (filename, profile, model),
        ).fetchall()
        if len(versions) > max_versions:
            old_ts = [v[0] for v in versions[max_versions:]]
            self._con.execute(
                "DELETE FROM scan_results"
                " WHERE filename = ? AND profile = ? AND model = ?"
                f" AND scan_timestamp IN ({','.join('?' * len(old_ts))})",
                (filename, profile, model, *old_ts),
            )
        self._con.commit()
```

**Step 4: Add get_scan_versions**

```python
def get_scan_versions(self, filename: str, profile: str, model: str
                      ) -> list[dict]:
    """Return list of scan versions for (filename, profile, model).
    
    Returns [{timestamp, count, max_score}, ...] ordered newest first.
    """
    if not self._enabled:
        return []
    rows = self._con.execute(
        "SELECT scan_timestamp, COUNT(*), MAX(score)"
        " FROM scan_results"
        " WHERE filename = ? AND profile = ? AND model = ?"
        "   AND scan_timestamp != ''"
        " GROUP BY scan_timestamp"
        " ORDER BY scan_timestamp DESC",
        (filename, profile, model),
    ).fetchall()
    return [{"timestamp": ts, "count": cnt, "max_score": sc}
            for ts, cnt, sc in rows]
```

**Step 5: Modify get_scan_results to support version selection**

Add optional `scan_timestamp` parameter. When None (default), returns latest version:

```python
def get_scan_results(self, filename: str, profile: str,
                     scan_timestamp: str | None = None
                     ) -> dict[str, list[tuple]]:
    if not self._enabled:
        return {}
    if scan_timestamp:
        rows = self._con.execute(
            "SELECT id, model, start_time, end_time, score, disabled,"
            "       orig_start_time, orig_end_time"
            " FROM scan_results"
            " WHERE filename = ? AND profile = ? AND scan_timestamp = ?"
            " ORDER BY model, start_time",
            (filename, profile, scan_timestamp),
        ).fetchall()
    else:
        # For each model, get rows from the latest timestamp only
        rows = self._con.execute(
            "SELECT r.id, r.model, r.start_time, r.end_time, r.score,"
            "       r.disabled, r.orig_start_time, r.orig_end_time"
            " FROM scan_results r"
            " INNER JOIN ("
            "   SELECT model, MAX(scan_timestamp) AS latest"
            "   FROM scan_results"
            "   WHERE filename = ? AND profile = ?"
            "   GROUP BY model"
            " ) m ON r.model = m.model AND r.scan_timestamp = m.latest"
            " WHERE r.filename = ? AND r.profile = ?"
            " ORDER BY r.model, r.start_time",
            (filename, profile, filename, profile),
        ).fetchall()
    result: dict[str, list] = {}
    for row_id, model, s, e, sc, dis, os_, oe in rows:
        result.setdefault(model, []).append(
            (row_id, s, e, sc, bool(dis),
             os_ if os_ is not None else s,
             oe if oe is not None else e))
    return result
```

**Important:** Legacy rows (before this change) have `scan_timestamp = ''`. The `MAX(scan_timestamp)` query handles this correctly — empty string sorts before any real timestamp, so legacy rows are returned when they're the only version. The `get_scan_versions` query filters `scan_timestamp != ''` so legacy rows don't appear as named versions.

**Step 6: Run tests, commit**

```bash
pytest tests/ -v
git add core/db.py tests/test_db.py
git commit -m "feat: scan result history — keep N versions per (file, model)"
```

---

### Task 3: Scan history UI — version selector in ScanResultsPanel

**Files:**
- Modify: `main.py` (ScanResultsPanel — add version combo per tab)
- Modify: `main.py` (ScanResultsPanel.load_for_file — populate versions)

**Step 1: Add version combo to tab UI**

In `ScanResultsPanel._add_tab()`, add a small QComboBox above the table. When no history exists, hide it. When versions exist, populate with timestamps and connect to a slot that reloads the tab with that version.

```python
# In _add_tab, create a container widget with version combo + table
container = QWidget()
layout = QVBoxLayout(container)
layout.setContentsMargins(0, 0, 0, 0)

cmb_version = QComboBox()
cmb_version.setMaximumWidth(200)
cmb_version.setToolTip("Scan version history")
cmb_version.hide()  # Hidden when only 1 version
layout.addWidget(cmb_version)
layout.addWidget(table)

self._tabs.addTab(container, label)
```

Store the combo and table as properties on the container widget for later access.

**Step 2: Populate versions in load_for_file**

After creating each model tab, query `get_scan_versions()`. If > 1 version, show the combo with entries like `"2026-04-19 14:30 (12 regions, best: 0.95)"`. Connect `currentIndexChanged` to reload that version's results.

**Step 3: Version switching slot**

When user selects a different version from the combo:
1. Call `db.get_scan_results(filename, profile, scan_timestamp=selected_ts)`
2. Repopulate the table with that version's rows
3. Update timeline regions

**Step 4: Test manually, commit**

```bash
git add main.py
git commit -m "feat: scan version selector in results panel"
```

---

### Task 4: Hard negatives — schema and training toggle

**Files:**
- Modify: `core/db.py:118-130` (hard_negatives schema — add source_model column)
- Modify: `core/db.py:548-560` (add_hard_negatives — accept source_model)
- Modify: `core/db.py:365-374` (get_training_data — use_hard_negatives parameter)
- Modify: `main.py` (TrainDialog — add "Use hard negatives" checkbox)
- Modify: `main.py` (_open_train_dialog — pass use_hard_negatives to get_training_data)
- Test: `tests/test_db.py`

**Step 1: Write failing test**

```python
def test_hard_negatives_source_model():
    """Hard negatives should store source_model."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add_hard_negatives("a.mp4", "test", [10.0, 20.0],
                              source_path="/a.mp4", source_model="HUBERT_XLARGE")
        rows = db.get_hard_negatives("test")
        assert len(rows) == 2
        assert all(r["source_model"] == "HUBERT_XLARGE" for r in rows)
    finally:
        os.unlink(path)

def test_training_data_skips_hard_negatives():
    """get_training_data with use_hard_negatives=False should skip them."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("a.mp4", 10.0, "/out/folder/g/clip.mp4", profile="test",
               source_path="/videos/a.mp4")
        db.add_hard_negatives("a.mp4", "test", [500.0], source_path="/videos/a.mp4")
        # With hard negatives
        data_with = db.get_training_data("test", "folder", use_hard_negatives=True)
        # Without hard negatives
        data_without = db.get_training_data("test", "folder", use_hard_negatives=False)
        # Both should find the video, but negative counts differ
        assert len(data_with) >= 1
        neg_with = sum(len(vi[3]) for vi in data_with)
        neg_without = sum(len(vi[3]) for vi in data_without)
        assert neg_with > neg_without or neg_with == neg_without  # depends on margin
    finally:
        os.unlink(path)
```

**Step 2: Add source_model column to hard_negatives**

In CREATE TABLE (line 119-125), add:

```sql
  source_model TEXT NOT NULL DEFAULT ''
```

In migration section, add after the hard_negatives table creation:

```python
hn_cols = {
    row[1]
    for row in self._con.execute("PRAGMA table_info(hard_negatives)").fetchall()
}
if "source_model" not in hn_cols:
    self._con.execute(
        "ALTER TABLE hard_negatives ADD COLUMN source_model TEXT NOT NULL DEFAULT ''"
    )
```

**Step 3: Update add_hard_negatives to accept source_model**

```python
def add_hard_negatives(self, filename: str, profile: str,
                       times: list[float], source_path: str = "",
                       source_model: str = "") -> None:
    if not self._enabled or not times:
        return
    with self._lock:
        for t in times:
            self._con.execute(
                "INSERT INTO hard_negatives"
                " (filename, profile, start_time, source_path, source_model)"
                " VALUES (?, ?, ?, ?, ?)",
                (filename, profile, t, source_path, source_model),
            )
        self._con.commit()
```

**Step 4: Add get_hard_negatives (full rows for management dialog)**

```python
def get_hard_negatives(self, profile: str) -> list[dict]:
    """Return all hard negatives for a profile with full details."""
    if not self._enabled:
        return []
    rows = self._con.execute(
        "SELECT id, filename, start_time, source_path, source_model"
        " FROM hard_negatives WHERE profile = ?"
        " ORDER BY filename, start_time",
        (profile,),
    ).fetchall()
    return [{"id": r[0], "filename": r[1], "start_time": r[2],
             "source_path": r[3], "source_model": r[4]} for r in rows]
```

**Step 5: Add delete_hard_negatives_by_ids**

```python
def delete_hard_negatives_by_ids(self, ids: list[int]) -> None:
    """Delete hard negatives by row IDs."""
    if not self._enabled or not ids:
        return
    with self._lock:
        self._con.execute(
            f"DELETE FROM hard_negatives WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        self._con.commit()
```

**Step 6: Add use_hard_negatives parameter to get_training_data**

In `get_training_data()` (line 315), add parameter:

```python
def get_training_data(self, profile: str, positive_folder: str,
                      negative_folder: str = "",
                      fallback_video_dir: str = "",
                      include_scan_exports: bool = False,
                      use_hard_negatives: bool = True,
                      ) -> list[tuple[str, list[float], list[float], list[float]]]:
```

Then wrap the hard negatives query (lines 365-374) in a conditional:

```python
    if use_hard_negatives:
        hard_rows = self._con.execute(
            "SELECT filename, start_time, source_path FROM hard_negatives"
            " WHERE profile = ?",
            (profile,),
        ).fetchall()
        for fn, st, sp in hard_rows:
            neg_by_video.setdefault(fn, set()).add(st)
            if sp:
                source_by_filename.setdefault(fn, sp)
```

**Step 7: Pass source_model when marking negatives from scan panel**

In `main.py`, `_on_scan_negatives()` needs to pass the current scan model. The scan panel knows which tab is active:

```python
def _on_scan_negatives(self, times: list) -> None:
    if not self._file_path:
        return
    filename = os.path.basename(self._file_path)
    # Get current model tab name for source_model
    source_model = self._scan_panel.current_model_name()
    self._db.add_hard_negatives(filename, self._profile, times,
                                source_path=self._file_path,
                                source_model=source_model)
```

Add `current_model_name()` to ScanResultsPanel:

```python
def current_model_name(self) -> str:
    """Return the model name of the currently active tab."""
    idx = self._tabs.currentIndex()
    if idx >= 0:
        return self._tabs.tabText(idx).split(" (")[0]  # strip count suffix
    return ""
```

**Step 8: Add training toggle to TrainDialog**

After the existing `_chk_scan_exports` checkbox:

```python
self._chk_hard_negatives = QCheckBox("Use hard negatives in training")
self._chk_hard_negatives.setChecked(True)
self._chk_hard_negatives.setToolTip(
    "When unchecked, manually marked hard negatives are excluded from training.\n"
    "Useful when training a new model type where old negatives may not apply.")
self._chk_hard_negatives.stateChanged.connect(lambda: self._debounce.start())
form.addRow("", self._chk_hard_negatives)
```

Add property:

```python
@property
def use_hard_negatives(self) -> bool:
    return self._chk_hard_negatives.isChecked()
```

**Step 9: Wire toggle through _open_train_dialog**

In `_open_train_dialog()`, pass the flag:

```python
    video_infos = self._db.get_training_data(
        self._profile, pos_folder, negative_folder=neg_folder,
        fallback_video_dir=video_dir,
        include_scan_exports=inc_scan,
        use_hard_negatives=dlg.use_hard_negatives,
    )
```

Also update `_update_stats()` in TrainDialog to pass it through for accurate counts:

```python
    use_neg = self._chk_hard_negatives.isChecked() if hasattr(self, '_chk_hard_negatives') else True
    video_infos = self._db.get_training_data(
        self._profile, folder, negative_folder=neg_folder,
        fallback_video_dir=self._txt_video_dir.text(),
        include_scan_exports=inc_scan,
        use_hard_negatives=use_neg,
    )
```

**Step 10: Run tests, commit**

```bash
pytest tests/ -v
git add core/db.py main.py tests/test_db.py
git commit -m "feat: hard negative source_model tracking, training toggle"
```

---

### Task 5: Hard negatives management dialog

**Files:**
- Modify: `main.py` (add HardNegativesDialog class)
- Modify: `main.py` (TrainDialog — add "Manage..." button)

**Step 1: Create HardNegativesDialog**

Place before TrainDialog class:

```python
class HardNegativesDialog(QDialog):
    """View and manage hard negative training examples."""

    def __init__(self, db: ProcessedDB, profile: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hard Negatives")
        self.setMinimumSize(600, 400)
        self._db = db
        self._profile = profile

        layout = QVBoxLayout(self)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter model:"))
        self._cmb_filter = QComboBox()
        self._cmb_filter.addItem("(all)")
        self._cmb_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self._cmb_filter, 1)
        layout.addLayout(filter_row)

        # Summary
        self._lbl_summary = QLabel()
        layout.addWidget(self._lbl_summary)

        # Table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["File", "Time", "Source Model", "ID"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setColumnHidden(3, True)  # hide ID column
        layout.addWidget(self._table)

        # Buttons
        btn_row = QHBoxLayout()
        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(btn_delete)
        btn_clear = QPushButton("Clear All")
        btn_clear.clicked.connect(self._clear_all)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self._load()

    def _load(self):
        rows = self._db.get_hard_negatives(self._profile)
        models = sorted(set(r["source_model"] for r in rows if r["source_model"]))
        self._cmb_filter.blockSignals(True)
        self._cmb_filter.clear()
        self._cmb_filter.addItem("(all)")
        for m in models:
            self._cmb_filter.addItem(m)
        self._cmb_filter.blockSignals(False)

        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._table.setItem(i, 0, QTableWidgetItem(r["filename"]))
            self._table.setItem(i, 1, QTableWidgetItem(f'{r["start_time"]:.1f}s'))
            self._table.setItem(i, 2, QTableWidgetItem(r["source_model"]))
            item = QTableWidgetItem(str(r["id"]))
            self._table.setItem(i, 3, item)
        self._lbl_summary.setText(f"<b>{len(rows)}</b> hard negatives")

    def _apply_filter(self):
        model = self._cmb_filter.currentText()
        for row in range(self._table.rowCount()):
            if model == "(all)":
                self._table.setRowHidden(row, False)
            else:
                src = self._table.item(row, 2).text()
                self._table.setRowHidden(row, src != model)

    def _delete_selected(self):
        ids = []
        for row in sorted(set(i.row() for i in self._table.selectedItems()), reverse=True):
            if not self._table.isRowHidden(row):
                ids.append(int(self._table.item(row, 3).text()))
        if ids:
            self._db.delete_hard_negatives_by_ids(ids)
            self._load()

    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear All",
            f"Delete all hard negatives for profile '{self._profile}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            all_rows = self._db.get_hard_negatives(self._profile)
            self._db.delete_hard_negatives_by_ids([r["id"] for r in all_rows])
            self._load()
```

**Step 2: Add "Manage..." button to TrainDialog**

After the hard negatives checkbox, add a button:

```python
neg_row = QHBoxLayout()
neg_row.addWidget(self._chk_hard_negatives)
btn_manage_neg = QPushButton("Manage…")
btn_manage_neg.setFixedWidth(80)
btn_manage_neg.clicked.connect(self._manage_negatives)
neg_row.addWidget(btn_manage_neg)
form.addRow("", neg_row)  # replaces the standalone checkbox addRow
```

Add handler:

```python
def _manage_negatives(self):
    dlg = HardNegativesDialog(self._db, self._profile, parent=self)
    dlg.exec()
    self._debounce.start()  # refresh stats after potential deletions
```

**Step 3: Test manually, commit**

```bash
pytest tests/ -v
git add main.py
git commit -m "feat: hard negatives management dialog with filter and bulk delete"
```

---

### Task 6: Final integration test and push

**Step 1: Manual test checklist**

- [ ] Open Train dialog — verify no ghost folders appear
- [ ] Train with "Use hard negatives" unchecked — verify training works
- [ ] Train with "Use hard negatives" checked — verify negatives are used
- [ ] Open Manage dialog — verify negatives listed with source model
- [ ] Delete selected negatives — verify they're removed
- [ ] Scan a video — verify results saved with timestamp
- [ ] Rescan same video — verify version history appears
- [ ] Switch version in scan panel — verify correct results display
- [ ] Mark negative from scan results — verify source_model stored

**Step 2: Push**

```bash
git push
```
