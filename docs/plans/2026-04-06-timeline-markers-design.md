# Timeline Markers Design

## Overview

When a file is loaded that matches a previously processed source, numbered markers appear on the timeline at the exact positions where clips were previously extracted. Hovering over a marker shows the output path of that export.

## DB Schema Change

Drop the `UNIQUE` constraint on `filename` and add `start_time` and `output_path` columns:

```sql
CREATE TABLE processed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    start_time REAL NOT NULL,
    output_path TEXT NOT NULL,
    processed_at TEXT NOT NULL
)
```

Multiple rows per source filename are now allowed (one per export).

**Migration:** On startup, detect the old schema via `PRAGMA table_info(processed)`. If `start_time` column is missing, drop and recreate the table. Data from the old schema is lost (acceptable — it only stored filenames, not positions).

## New DB Methods

- `add(filename, start_time, output_path)` — inserts a new row; no deduplication (accumulates all exports)
- `get_markers(filename) -> list[tuple[float, int, str]]` — finds the best fuzzy-similar filename in the DB, fetches all its rows ordered by `start_time`, returns `[(start_time, marker_number, output_path), ...]` (1-indexed)

Existing `find_similar` is kept unchanged for the status bar warning.

## TimelineWidget Changes

- `set_markers(markers: list[tuple[float, int, str]])` — stores marker list, calls `update()`
- `paintEvent`: for each marker, draws a red vertical line at the corresponding x position + the marker number in small white text above the line
- `mouseMoveEvent`: checks if mouse x is within ±4px of any marker's x. If yes, calls `QToolTip.showText(QCursor.pos(), output_path)`. Otherwise hides the tooltip.

## MainWindow Changes

- `_after_load`: calls `db.get_markers(basename)` and passes result to `timeline.set_markers()`
- `_on_export_done`: calls `db.add(filename, cursor, output_path)` (now with start_time and output_path), then refreshes markers on the timeline immediately via `db.get_markers` + `timeline.set_markers`
