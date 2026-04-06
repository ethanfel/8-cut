# Playlist & Processed-Files Database Design

## Overview

Two new features added to the existing 8-cut tool:

1. **Playlist** — a queue of source video files; auto-advances to the next file after each export.
2. **Processed-files database** — SQLite store of exported source filenames; warns when a newly loaded file is fuzzy-similar to one already processed.

## Stack additions

- `sqlite3` (Python built-in) — persistent DB at `~/.8cut.db`
- `difflib.SequenceMatcher` (Python built-in) — fuzzy filename similarity, threshold 0.75

## Layout

```
┌──────────────────────────────────────────────────────┐
│  [Drop video files here]                             │
├─────────────────┬────────────────────────────────────┤
│ QUEUE           │                                    │
│ ▶ clip_a.mp4   │        mpv video preview           │
│   clip_b.mp4   │                                    │
│   clip_c.mp4   ├────────────────────────────────────┤
│                 │  Timeline ──────|cursor──────      │
│                 ├────────────────────────────────────┤
│                 │  [▶ Play 8s] [⏸]  cursor / dur    │
├─────────────────┴────────────────────────────────────┤
│  Name: [clip]  Folder: [~]  → clip_001.mp4  [Export] │
└──────────────────────────────────────────────────────┘
```

- Drop one or multiple files → all added to the queue
- Clicking a queue item loads it immediately
- `▶` marks the current file
- Auto-advances to next item after a successful export

## Components

### `ProcessedDB`

Wraps `sqlite3`. DB file: `~/.8cut.db`, created on first run.

Table:
```sql
CREATE TABLE IF NOT EXISTS processed (
    filename TEXT NOT NULL,
    processed_at TEXT NOT NULL
)
```

Methods:
- `add(filename: str)` — inserts row with `filename` and current UTC ISO timestamp
- `find_similar(filename: str) -> str | None` — loads all filenames, iterates with `SequenceMatcher(None, a, b).ratio() >= 0.75`, returns the best-matching stored filename or `None`

### `PlaylistWidget(QListWidget)`

- Accepts file drops (via `dragEnterEvent` / `dropEvent`)
- Emits `file_selected = pyqtSignal(str)` when an item is activated (click or auto-advance)
- `add_files(paths: list[str])` — appends paths, skips duplicates already in the list
- `advance()` — selects the next item; if at end, does nothing (queue exhausted)
- Current item shown with a `▶ ` prefix in the display text; others show bare filename

### `MainWindow` changes

- Drop zone (`dragEnterEvent`/`dropEvent`) now calls `playlist.add_files(paths)` instead of loading directly
- `playlist.file_selected` connected to `_load_file`
- `_after_load`: calls `db.find_similar(os.path.basename(path))` — if match found, shows `"⚠ Similar to already processed: {match}"` in status bar
- `_on_export_done`: calls `db.add(os.path.basename(self._file_path))`, then `self._playlist.advance()`

## Warning behavior

- Warning is informational only — does not block export
- Shown in the existing status bar with a `⚠` prefix
- Shown each time a file is loaded (queue advance or manual click)

## Error handling

- DB connection errors on startup: logged to stderr, app continues without DB (warning feature silently disabled)
- Queue exhaustion (advance past last item): silently does nothing; user can add more files
