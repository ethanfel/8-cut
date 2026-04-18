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
                "scan_export":    "INTEGER NOT NULL DEFAULT 0",
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
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS scan_results ("
            "  id         INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  filename   TEXT NOT NULL,"
            "  profile    TEXT NOT NULL DEFAULT 'default',"
            "  model      TEXT NOT NULL,"
            "  start_time REAL NOT NULL,"
            "  end_time   REAL NOT NULL,"
            "  score      REAL NOT NULL"
            ")"
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_file_profile_model"
            " ON scan_results(filename, profile, model)"
        )
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS hard_negatives ("
            "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  filename    TEXT NOT NULL,"
            "  profile     TEXT NOT NULL DEFAULT 'default',"
            "  start_time  REAL NOT NULL,"
            "  source_path TEXT NOT NULL DEFAULT ''"
            ")"
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_hardneg_file_profile"
            " ON hard_negatives(filename, profile)"
        )
        self._con.commit()

    def add(self, filename: str, start_time: float, output_path: str,
            label: str = "", category: str = "",
            short_side: int | None = None, portrait_ratio: str = "",
            crop_center: float = 0.5, fmt: str = "MP4",
            clip_count: int = 3, spread: float = 3.0,
            profile: str = "default", source_path: str = "",
            scan_export: bool = False) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._con.execute(
                "INSERT INTO processed"
                " (filename, start_time, output_path, label, category,"
                "  short_side, portrait_ratio, crop_center, format,"
                "  clip_count, spread, profile, source_path, scan_export, processed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (filename, start_time, output_path, label, category,
                 short_side, portrait_ratio, crop_center, fmt,
                 clip_count, spread, profile, source_path,
                 1 if scan_export else 0,
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

    def get_group(self, output_path: str, profile: str = "") -> list[str]:
        """Return all output_paths sharing the same (filename, start_time, profile) as *output_path*."""
        if not self._enabled:
            return []
        row = self._con.execute(
            "SELECT filename, start_time, profile FROM processed WHERE output_path = ?",
            (output_path,),
        ).fetchone()
        if not row:
            return []
        filename, start_time, row_profile = row
        p = profile or row_profile
        rows = self._con.execute(
            "SELECT output_path FROM processed"
            " WHERE filename = ? AND start_time = ? AND profile = ? ORDER BY output_path",
            (filename, start_time, p),
        ).fetchall()
        return [r[0] for r in rows]

    def delete_group(self, output_path: str, profile: str = "") -> list[str]:
        """Delete all rows sharing the same (filename, start_time, profile) as *output_path*.
        Returns list of deleted output_paths."""
        if not self._enabled:
            return []
        with self._lock:
            row = self._con.execute(
                "SELECT filename, start_time, profile FROM processed WHERE output_path = ?",
                (output_path,),
            ).fetchone()
            if not row:
                return []
            filename, start_time, row_profile = row
            p = profile or row_profile
            paths = [r[0] for r in self._con.execute(
                "SELECT output_path FROM processed"
                " WHERE filename = ? AND start_time = ? AND profile = ?",
                (filename, start_time, p),
            ).fetchall()]
            self._con.execute(
                "DELETE FROM processed WHERE filename = ? AND start_time = ? AND profile = ?",
                (filename, start_time, p),
            )
            self._con.commit()
            return paths

    def _get_markers_for(self, match: str, profile: str = "default") -> list[tuple[float, int, str]]:
        rows = self._con.execute(
            "SELECT start_time, output_path FROM processed"
            " WHERE filename = ? AND profile = ? AND scan_export = 0"
            " ORDER BY start_time",
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
                          negative_folder: str = "",
                          fallback_video_dir: str = "",
                          include_scan_exports: bool = False,
                          ) -> list[tuple[str, list[float], list[float], list[float]]]:
        """Build training video_infos from DB data.

        Args:
            profile: profile name
            positive_folder: export folder name for positive class (e.g. "mp4_Intense")
            negative_folder: export folder name for explicit negatives (optional)
            fallback_video_dir: if source_path is empty, try filename in this dir
            include_scan_exports: if True, include auto-exported scan clips

        Returns:
            list of (source_video_path, positive_times, soft_times, negative_times)
            per video.  Soft times = clips from any other non-negative folder.
        """
        if not self._enabled:
            return []
        if include_scan_exports:
            rows = self._con.execute(
                "SELECT filename, start_time, output_path, source_path"
                " FROM processed WHERE profile = ?",
                (profile,),
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT filename, start_time, output_path, source_path"
                " FROM processed WHERE profile = ? AND scan_export = 0",
                (profile,),
            ).fetchall()

        # Collect times by video, split by folder role
        pos_by_video: dict[str, set[float]] = {}
        neg_by_video: dict[str, set[float]] = {}
        soft_by_video: dict[str, set[float]] = {}
        source_by_filename: dict[str, str] = {}

        for fn, st, op, sp in rows:
            if sp:
                source_by_filename[fn] = sp
            grandparent = os.path.basename(os.path.dirname(os.path.dirname(op)))
            if grandparent == positive_folder:
                pos_by_video.setdefault(fn, set()).add(st)
            elif negative_folder and grandparent == negative_folder:
                neg_by_video.setdefault(fn, set()).add(st)
            else:
                soft_by_video.setdefault(fn, set()).add(st)

        # Include hard negatives from scan feedback
        hard_rows = self._con.execute(
            "SELECT filename, start_time, source_path FROM hard_negatives"
            " WHERE profile = ?",
            (profile,),
        ).fetchall()
        for fn, st, sp in hard_rows:
            neg_by_video.setdefault(fn, set()).add(st)
            if sp:
                source_by_filename.setdefault(fn, sp)

        # Remove positive times from soft/neg to avoid conflicting labels
        for fn in pos_by_video:
            if fn in soft_by_video:
                soft_by_video[fn] -= pos_by_video[fn]
            if fn in neg_by_video:
                neg_by_video[fn] -= pos_by_video[fn]

        # Deduplicate nearby markers (spread clips from same position)
        def _dedup_times(times: set[float], min_gap: float = 8.0) -> list[float]:
            if not times:
                return []
            ordered = sorted(times)
            result = [ordered[0]]
            for t in ordered[1:]:
                if t - result[-1] >= min_gap:
                    result.append(t)
            return result

        # Include videos that have positives OR explicit negatives
        all_videos = set(pos_by_video) | set(neg_by_video)
        result = []
        for fn in all_videos:
            sp = source_by_filename.get(fn, "")
            if not sp or not os.path.exists(sp):
                if fallback_video_dir:
                    sp = os.path.join(fallback_video_dir, fn)
            if not sp or not os.path.exists(sp):
                continue
            gt_pos = _dedup_times(pos_by_video.get(fn, set()))
            gt_soft = _dedup_times(soft_by_video.get(fn, set()))
            gt_neg = _dedup_times(neg_by_video.get(fn, set()))
            result.append((sp, gt_pos, gt_soft, gt_neg))
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

    # ── Scan results ─────────────────────────────────────────────

    def save_scan_results(self, filename: str, profile: str, model: str,
                          regions: list[tuple[float, float, float]]) -> None:
        """Replace scan results for (filename, profile, model) with new regions.

        regions: list of (start_time, end_time, score).
        """
        if not self._enabled:
            return
        with self._lock:
            self._con.execute(
                "DELETE FROM scan_results"
                " WHERE filename = ? AND profile = ? AND model = ?",
                (filename, profile, model),
            )
            self._con.executemany(
                "INSERT INTO scan_results"
                " (filename, profile, model, start_time, end_time, score)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(filename, profile, model, s, e, sc) for s, e, sc in regions],
            )
            self._con.commit()

    def get_scan_results(self, filename: str, profile: str
                         ) -> dict[str, list[tuple[int, float, float, float]]]:
        """Return scan results grouped by model.

        Returns {model: [(row_id, start_time, end_time, score), ...]} sorted by
        start_time.
        """
        if not self._enabled:
            return {}
        rows = self._con.execute(
            "SELECT id, model, start_time, end_time, score FROM scan_results"
            " WHERE filename = ? AND profile = ?"
            " ORDER BY model, start_time",
            (filename, profile),
        ).fetchall()
        result: dict[str, list[tuple[int, float, float, float]]] = {}
        for row_id, model, s, e, sc in rows:
            result.setdefault(model, []).append((row_id, s, e, sc))
        return result

    def delete_scan_result(self, row_id: int) -> None:
        """Delete a single scan result row."""
        if not self._enabled:
            return
        with self._lock:
            self._con.execute("DELETE FROM scan_results WHERE id = ?", (row_id,))
            self._con.commit()

    def get_scan_models(self, filename: str, profile: str) -> list[str]:
        """Return model names that have scan results for this file."""
        if not self._enabled:
            return []
        rows = self._con.execute(
            "SELECT DISTINCT model FROM scan_results"
            " WHERE filename = ? AND profile = ? ORDER BY model",
            (filename, profile),
        ).fetchall()
        return [r[0] for r in rows]

    def get_scanned_filenames(self, profile: str, model: str) -> set[str]:
        """Return filenames that already have scan results for this model."""
        if not self._enabled:
            return set()
        rows = self._con.execute(
            "SELECT DISTINCT filename FROM scan_results"
            " WHERE profile = ? AND model = ?",
            (profile, model),
        ).fetchall()
        return {r[0] for r in rows}

    def add_hard_negatives(self, filename: str, profile: str,
                           times: list[float], source_path: str = "") -> None:
        """Save timestamps as hard-negative training examples."""
        if not self._enabled or not times:
            return
        with self._lock:
            for t in times:
                self._con.execute(
                    "INSERT INTO hard_negatives (filename, profile, start_time, source_path)"
                    " VALUES (?, ?, ?, ?)",
                    (filename, profile, t, source_path),
                )
            self._con.commit()

    def get_hard_negative_times(self, filename: str, profile: str) -> set[float]:
        """Return start_times marked as hard negatives for this file."""
        if not self._enabled:
            return set()
        rows = self._con.execute(
            "SELECT start_time FROM hard_negatives"
            " WHERE filename = ? AND profile = ?",
            (filename, profile),
        ).fetchall()
        return {r[0] for r in rows}

    def remove_hard_negatives(self, filename: str, profile: str,
                              times: list[float]) -> None:
        """Remove specific hard-negative timestamps."""
        if not self._enabled or not times:
            return
        with self._lock:
            for t in times:
                self._con.execute(
                    "DELETE FROM hard_negatives"
                    " WHERE filename = ? AND profile = ? AND start_time = ?",
                    (filename, profile, t),
                )
            self._con.commit()

    def get_training_filenames(self, profile: str) -> set[str]:
        """Return filenames used in training (have exported clips)."""
        if not self._enabled:
            return set()
        rows = self._con.execute(
            "SELECT DISTINCT filename FROM processed WHERE profile = ?",
            (profile,),
        ).fetchall()
        return {r[0] for r in rows}

    # ── Hidden files ───────────────────────────────────────────

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
