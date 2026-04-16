import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .paths import _log


class ProcessedDB:
    _SCHEMA_VERSION = 3  # bump when schema changes

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path.home() / ".8cut.db")
        self._path = db_path
        try:
            self._con = sqlite3.connect(db_path, check_same_thread=False)
            self._migrate()
            self._enabled = True
            _log(f"DB opened: {db_path}")
        except Exception as e:
            _log(f"DB unavailable: {e}")
            self._con = None
            self._enabled = False

    def _migrate(self) -> None:
        """Create table if missing, then add any new columns for old DBs."""
        cols = {
            row[1]
            for row in self._con.execute("PRAGMA table_info(processed)").fetchall()
        }
        if not cols:
            # Fresh DB — create from scratch
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS processed ("
                "  id              INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  filename        TEXT    NOT NULL,"
                "  start_time      REAL    NOT NULL,"
                "  output_path     TEXT    NOT NULL,"
                "  label           TEXT    NOT NULL DEFAULT '',"
                "  category        TEXT    NOT NULL DEFAULT '',"
                "  short_side      INTEGER DEFAULT 512,"
                "  portrait_ratio  TEXT    NOT NULL DEFAULT '',"
                "  crop_center     REAL    NOT NULL DEFAULT 0.5,"
                "  format          TEXT    NOT NULL DEFAULT 'MP4',"
                "  clip_count      INTEGER NOT NULL DEFAULT 3,"
                "  spread          REAL    NOT NULL DEFAULT 3.0,"
                "  profile         TEXT    NOT NULL DEFAULT 'default',"
                "  processed_at    TEXT    NOT NULL"
                ")"
            )
        else:
            # Add missing columns to legacy tables
            new_cols = {
                "label":          "TEXT NOT NULL DEFAULT ''",
                "category":       "TEXT NOT NULL DEFAULT ''",
                "short_side":     "INTEGER DEFAULT 512",
                "portrait_ratio": "TEXT NOT NULL DEFAULT ''",
                "crop_center":    "REAL NOT NULL DEFAULT 0.5",
                "format":         "TEXT NOT NULL DEFAULT 'MP4'",
                "clip_count":     "INTEGER NOT NULL DEFAULT 3",
                "spread":         "REAL NOT NULL DEFAULT 3.0",
                "profile":        "TEXT NOT NULL DEFAULT 'default'",
            }
            for col, typedef in new_cols.items():
                if col not in cols:
                    self._con.execute(
                        f"ALTER TABLE processed ADD COLUMN {col} {typedef}"
                    )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_filename ON processed(filename)"
        )
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS hidden_files ("
            "  filename  TEXT NOT NULL,"
            "  profile   TEXT NOT NULL DEFAULT 'default',"
            "  PRIMARY KEY (filename, profile)"
            ")"
        )
        self._con.commit()

    def add(self, filename: str, start_time: float, output_path: str,
            label: str = "", category: str = "",
            short_side: int | None = None, portrait_ratio: str = "",
            crop_center: float = 0.5, fmt: str = "MP4",
            clip_count: int = 3, spread: float = 3.0,
            profile: str = "default") -> None:
        if not self._enabled:
            return
        self._con.execute(
            "INSERT INTO processed"
            " (filename, start_time, output_path, label, category,"
            "  short_side, portrait_ratio, crop_center, format,"
            "  clip_count, spread, profile, processed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, start_time, output_path, label, category,
             short_side, portrait_ratio, crop_center, fmt,
             clip_count, spread, profile,
             datetime.now(timezone.utc).isoformat()),
        )
        self._con.commit()

    def get_labels(self) -> list[str]:
        """Return distinct non-empty labels ordered by most recently used."""
        if not self._enabled:
            return []
        rows = self._con.execute(
            "SELECT DISTINCT label FROM processed"
            " WHERE label != '' ORDER BY processed_at DESC"
        ).fetchall()
        # Deduplicate while preserving order (DISTINCT on processed_at DESC
        # may return duplicates if the same label was used multiple times).
        seen: set[str] = set()
        result = []
        for (lbl,) in rows:
            if lbl not in seen:
                seen.add(lbl)
                result.append(lbl)
        return result

    def get_by_output_path(self, output_path: str) -> dict | None:
        """Return config dict for an output_path, or None."""
        if not self._enabled:
            return None
        self._con.row_factory = sqlite3.Row
        row = self._con.execute(
            "SELECT label, category, short_side, portrait_ratio, crop_center, format,"
            " clip_count, spread"
            " FROM processed WHERE output_path = ?",
            (output_path,),
        ).fetchone()
        self._con.row_factory = None
        return dict(row) if row else None

    def delete_by_output_path(self, output_path: str) -> None:
        if not self._enabled:
            return
        self._con.execute("DELETE FROM processed WHERE output_path = ?", (output_path,))
        self._con.commit()

    def get_group(self, output_path: str) -> list[str]:
        """Return all output_paths sharing the same (filename, start_time) as *output_path*."""
        if not self._enabled:
            return []
        row = self._con.execute(
            "SELECT filename, start_time FROM processed WHERE output_path = ?",
            (output_path,),
        ).fetchone()
        if not row:
            return []
        rows = self._con.execute(
            "SELECT output_path FROM processed"
            " WHERE filename = ? AND start_time = ? ORDER BY output_path",
            (row[0], row[1]),
        ).fetchall()
        return [r[0] for r in rows]

    def delete_group(self, output_path: str) -> list[str]:
        """Delete all rows sharing the same (filename, start_time) as *output_path*.
        Returns list of deleted output_paths."""
        if not self._enabled:
            return []
        row = self._con.execute(
            "SELECT filename, start_time FROM processed WHERE output_path = ?",
            (output_path,),
        ).fetchone()
        if not row:
            return []
        filename, start_time = row
        paths = [r[0] for r in self._con.execute(
            "SELECT output_path FROM processed WHERE filename = ? AND start_time = ?",
            (filename, start_time),
        ).fetchall()]
        self._con.execute(
            "DELETE FROM processed WHERE filename = ? AND start_time = ?",
            (filename, start_time),
        )
        self._con.commit()
        return paths

    def _get_markers_for(self, match: str, profile: str = "default") -> list[tuple[float, int, str]]:
        rows = self._con.execute(
            "SELECT start_time, output_path FROM processed"
            " WHERE filename = ? AND profile = ? ORDER BY start_time",
            (match, profile),
        ).fetchall()
        # Deduplicate by start_time — batch exports share the same cursor.
        seen_times: dict[float, tuple[float, int, str]] = {}
        n = 0
        for t, p in rows:
            if t not in seen_times:
                n += 1
                seen_times[t] = (t, n, p)
        return list(seen_times.values())

    def get_markers(self, filename: str, profile: str = "default") -> list[tuple[float, int, str]]:
        """Return [(start_time, marker_number, output_path), ...] for exact
        filename match, sorted by start_time. Empty list if no match."""
        if not self._enabled:
            return []
        return self._get_markers_for(filename, profile)

    def get_profiles(self) -> list[str]:
        """Return distinct profile names, ordered alphabetically."""
        if not self._enabled:
            return []
        rows = self._con.execute(
            "SELECT DISTINCT profile FROM processed ORDER BY profile"
        ).fetchall()
        return [r[0] for r in rows]

    def hide_file(self, filename: str, profile: str = "default") -> None:
        if not self._enabled:
            return
        self._con.execute(
            "INSERT OR IGNORE INTO hidden_files (filename, profile) VALUES (?, ?)",
            (filename, profile),
        )
        self._con.commit()

    def unhide_file(self, filename: str, profile: str = "default") -> None:
        if not self._enabled:
            return
        self._con.execute(
            "DELETE FROM hidden_files WHERE filename = ? AND profile = ?",
            (filename, profile),
        )
        self._con.commit()

    def get_hidden_files(self, profile: str = "default") -> set[str]:
        if not self._enabled:
            return set()
        rows = self._con.execute(
            "SELECT filename FROM hidden_files WHERE profile = ?", (profile,)
        ).fetchall()
        return {r[0] for r in rows}
