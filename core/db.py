import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import _log


class ProcessedDB:
    _SCHEMA_VERSION = 4  # bump when schema changes

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path.home() / ".8cut.db")
        self._path = db_path
        self._lock = threading.Lock()
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
                "  source_path     TEXT    NOT NULL DEFAULT '',"
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
                "source_path":    "TEXT NOT NULL DEFAULT ''",
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
            profile: str = "default", source_path: str = "") -> None:
        if not self._enabled:
            return
        with self._lock:
            self._con.execute(
                "INSERT INTO processed"
                " (filename, start_time, output_path, label, category,"
                "  short_side, portrait_ratio, crop_center, format,"
                "  clip_count, spread, profile, source_path, processed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (filename, start_time, output_path, label, category,
                 short_side, portrait_ratio, crop_center, fmt,
                 clip_count, spread, profile, source_path,
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
        cur = self._con.cursor()
        cur.row_factory = sqlite3.Row
        row = cur.execute(
            "SELECT label, category, short_side, portrait_ratio, crop_center, format,"
            " clip_count, spread"
            " FROM processed WHERE output_path = ?",
            (output_path,),
        ).fetchone()
        return dict(row) if row else None

    def delete_by_output_path(self, output_path: str) -> None:
        if not self._enabled:
            return
        with self._lock:
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
        with self._lock:
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

    def get_all_export_paths(self, profile: str = "default") -> list[str]:
        """Return all unique output_path values for a given profile."""
        if not self._enabled:
            return []
        rows = self._con.execute(
            "SELECT DISTINCT output_path FROM processed WHERE profile = ?",
            (profile,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_export_folders(self, profile: str = "default") -> list[str]:
        """Return distinct export folder names found in output_paths for a profile.

        Export paths follow the structure:
            .../export_folder/group_dir/clip.mp4
        The export folder is 2 levels up from the clip file.
        Returns folder names sorted alphabetically (e.g. ["mp4_Intense", "mp4_Soft"]).
        """
        if not self._enabled:
            return []
        rows = self._con.execute(
            "SELECT DISTINCT output_path FROM processed WHERE profile = ?",
            (profile,),
        ).fetchall()
        folder_names: set[str] = set()
        for (op,) in rows:
            grandparent = os.path.basename(os.path.dirname(os.path.dirname(op)))
            if grandparent:
                folder_names.add(grandparent)
        return sorted(folder_names)

    def get_training_data(self, profile: str, positive_folder: str,
                          fallback_video_dir: str = "",
                          ) -> list[tuple[str, list[float], list[float]]]:
        """Build training video_infos from DB data.

        Args:
            profile: profile name
            positive_folder: export folder name for positive class (e.g. "mp4_Intense")
            fallback_video_dir: if source_path is empty, try filename in this dir

        Returns:
            list of (source_video_path, positive_times, soft_times) per video.
            Soft times = clips from any other export folder.
        """
        if not self._enabled:
            return []
        rows = self._con.execute(
            "SELECT filename, start_time, output_path, source_path"
            " FROM processed WHERE profile = ?",
            (profile,),
        ).fetchall()

        # Collect times by video, split by positive vs other folders
        pos_by_video: dict[str, set[float]] = {}
        soft_by_video: dict[str, set[float]] = {}
        source_by_filename: dict[str, str] = {}

        for fn, st, op, sp in rows:
            if sp:
                source_by_filename[fn] = sp
            grandparent = os.path.basename(os.path.dirname(os.path.dirname(op)))
            if grandparent == positive_folder:
                pos_by_video.setdefault(fn, set()).add(st)
            else:
                soft_by_video.setdefault(fn, set()).add(st)

        # Remove positive times from soft to avoid conflicting labels
        for fn in pos_by_video:
            if fn in soft_by_video:
                soft_by_video[fn] -= pos_by_video[fn]

        result = []
        for fn in pos_by_video:
            sp = source_by_filename.get(fn, "")
            if not sp or not os.path.exists(sp):
                # Fallback: try video_dir / filename
                if fallback_video_dir:
                    sp = os.path.join(fallback_video_dir, fn)
            if not sp or not os.path.exists(sp):
                continue
            gt_pos = sorted(pos_by_video[fn])
            gt_soft = sorted(soft_by_video.get(fn, set()))
            result.append((sp, gt_pos, gt_soft))
        return result

    def get_training_stats(self, profile: str) -> dict[str, dict]:
        """Return per-subprofile stats for training readiness display.

        Returns dict mapping subprofile_name → {
            'videos': number of distinct source videos,
            'clips': total clip count,
        }
        """
        if not self._enabled:
            return {}
        rows = self._con.execute(
            "SELECT filename, output_path FROM processed WHERE profile = ?",
            (profile,),
        ).fetchall()
        folders = self.get_export_folders(profile)
        stats: dict[str, dict] = {}
        for folder_name in folders:
            videos: set[str] = set()
            clips = 0
            for fn, op in rows:
                grandparent = os.path.basename(os.path.dirname(os.path.dirname(op)))
                if grandparent == folder_name:
                    videos.add(fn)
                    clips += 1
            stats[folder_name] = {"videos": len(videos), "clips": clips}
        return stats

    def hide_file(self, filename: str, profile: str = "default") -> None:
        if not self._enabled:
            return
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO hidden_files (filename, profile) VALUES (?, ?)",
                (filename, profile),
            )
            self._con.commit()

    def unhide_file(self, filename: str, profile: str = "default") -> None:
        if not self._enabled:
            return
        with self._lock:
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
