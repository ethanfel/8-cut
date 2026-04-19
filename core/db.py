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
                "  scan_export     INTEGER NOT NULL DEFAULT 0,"
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
            "  id              INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  filename        TEXT NOT NULL,"
            "  profile         TEXT NOT NULL DEFAULT 'default',"
            "  model           TEXT NOT NULL,"
            "  start_time      REAL NOT NULL,"
            "  end_time        REAL NOT NULL,"
            "  score           REAL NOT NULL,"
            "  disabled        INTEGER NOT NULL DEFAULT 0,"
            "  orig_start_time REAL,"
            "  orig_end_time   REAL,"
            "  scan_timestamp  TEXT NOT NULL DEFAULT ''"
            ")"
        )
        # Migrate: add new columns to existing scan_results tables
        sr_cols = {
            row[1]
            for row in self._con.execute("PRAGMA table_info(scan_results)").fetchall()
        }
        for col, typedef in [
            ("disabled",        "INTEGER NOT NULL DEFAULT 0"),
            ("orig_start_time", "REAL"),
            ("orig_end_time",   "REAL"),
            ("scan_timestamp",  "TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in sr_cols:
                self._con.execute(
                    f"ALTER TABLE scan_results ADD COLUMN {col} {typedef}"
                )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_file_profile_model"
            " ON scan_results(filename, profile, model)"
        )
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS hard_negatives ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  filename     TEXT NOT NULL,"
            "  profile      TEXT NOT NULL DEFAULT 'default',"
            "  start_time   REAL NOT NULL,"
            "  source_path  TEXT NOT NULL DEFAULT '',"
            "  source_model TEXT NOT NULL DEFAULT ''"
            ")"
        )
        # Migrate: add source_model column to existing hard_negatives tables
        hn_cols = {
            row[1]
            for row in self._con.execute("PRAGMA table_info(hard_negatives)").fetchall()
        }
        if "source_model" not in hn_cols:
            self._con.execute(
                "ALTER TABLE hard_negatives ADD COLUMN source_model TEXT NOT NULL DEFAULT ''"
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
        filename match, sorted by start_time. Empty list if no match.
        Excludes scan exports (shown via scan panel instead)."""
        if not self._enabled:
            return []
        return self._get_markers_for(filename, profile)

    def get_clip_count(self, filename: str, profile: str = "default") -> int:
        """Return total number of exported clips (including scan exports)."""
        if not self._enabled:
            return 0
        row = self._con.execute(
            "SELECT COUNT(*) FROM processed WHERE filename = ? AND profile = ?",
            (filename, profile),
        ).fetchone()
        return row[0] if row else 0

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

    def get_max_counter(self, folder: str, name: str) -> int:
        """Return the highest counter N found in output_paths matching folder/name_NNN*.

        Parses the group directory component (e.g. 'clip_035') from stored
        output_path values.  Returns 0 if no matches exist.
        """
        if not self._enabled:
            return 0
        prefix = os.path.join(folder, name + "_")
        rows = self._con.execute(
            "SELECT DISTINCT output_path FROM processed"
            " WHERE output_path LIKE ?",
            (prefix + "%",),
        ).fetchall()
        max_n = 0
        for (op,) in rows:
            # output_path: .../folder/name_NNN/name_NNN_sub.ext
            parent = os.path.basename(os.path.dirname(op))
            # parent should be "name_NNN"
            parts = parent.rsplit("_", 1)
            if len(parts) == 2:
                try:
                    max_n = max(max_n, int(parts[1]))
                except ValueError:
                    pass
        return max_n

    def get_export_folders(self, profile: str = "default",
                           include_scan_exports: bool = False) -> list[str]:
        """Return distinct export folder names found in output_paths for a profile.

        Export paths follow the structure:
            .../export_folder/group_dir/clip.mp4
        The export folder is 2 levels up from the clip file.
        Returns folder names sorted alphabetically (e.g. ["mp4_Intense", "mp4_Soft"]).
        """
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

    def get_training_data(self, profile: str, positive_folder: str,
                          negative_folder: str = "",
                          fallback_video_dir: str = "",
                          include_scan_exports: bool = False,
                          use_hard_negatives: bool = True,
                          ) -> list[tuple[str, list[float], list[float], list[float]]]:
        """Build training video_infos from DB data.

        Args:
            profile: profile name
            positive_folder: export folder name for positive class (e.g. "mp4_Intense")
            negative_folder: export folder name for explicit negatives (optional)
            fallback_video_dir: if source_path is empty, try filename in this dir
            include_scan_exports: if True, include auto-exported scan clips
            use_hard_negatives: if False, skip hard negatives from scan feedback

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

    def get_training_stats(self, profile: str,
                           include_scan_exports: bool = False) -> dict[str, dict]:
        """Return per-subprofile stats for training readiness display.

        Returns dict mapping subprofile_name → {
            'videos': number of distinct source videos,
            'clips': total clip count,
        }
        """
        if not self._enabled:
            return {}
        if include_scan_exports:
            rows = self._con.execute(
                "SELECT filename, output_path FROM processed WHERE profile = ?",
                (profile,),
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT filename, output_path FROM processed"
                " WHERE profile = ? AND scan_export = 0",
                (profile,),
            ).fetchall()
        folders = self.get_export_folders(profile, include_scan_exports=include_scan_exports)
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
        return {k: v for k, v in stats.items() if v["clips"] > 0}

    # ── Scan results ─────────────────────────────────────────────

    def save_scan_results(self, filename: str, profile: str, model: str,
                          regions: list[tuple[float, float, float]],
                          max_versions: int = 5) -> None:
        """Save scan results as a new version for (filename, profile, model).

        regions: list of (start_time, end_time, score).
        Keeps up to max_versions; oldest are pruned automatically.
        """
        if not self._enabled:
            return
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

    def get_scan_results(self, filename: str, profile: str,
                         scan_timestamp: str | None = None
                         ) -> dict[str, list[tuple[int, float, float, float, bool, float, float]]]:
        """Return scan results grouped by model.

        If scan_timestamp is given, returns only that version's rows.
        Otherwise returns the latest version per model.

        Returns {model: [(row_id, start, end, score, disabled, orig_start, orig_end), ...]}
        sorted by start_time.
        """
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
        result: dict[str, list[tuple[int, float, float, float, bool, float, float]]] = {}
        for row_id, model, s, e, sc, dis, os_, oe in rows:
            # Fall back to current bounds for legacy rows without orig
            result.setdefault(model, []).append(
                (row_id, s, e, sc, bool(dis), os_ if os_ is not None else s,
                 oe if oe is not None else e))
        return result

    def delete_scan_result(self, row_id: int) -> None:
        """Delete a single scan result row."""
        if not self._enabled:
            return
        with self._lock:
            self._con.execute("DELETE FROM scan_results WHERE id = ?", (row_id,))
            self._con.commit()

    def toggle_scan_result_disabled(self, row_id: int, disabled: bool) -> None:
        """Set disabled flag on a scan result row."""
        if not self._enabled:
            return
        with self._lock:
            self._con.execute(
                "UPDATE scan_results SET disabled = ? WHERE id = ?",
                (1 if disabled else 0, row_id),
            )
            self._con.commit()

    def update_scan_result_times(self, row_id: int,
                                 start: float, end: float) -> None:
        """Update start/end times of a scan result row (resize)."""
        if not self._enabled:
            return
        with self._lock:
            self._con.execute(
                "UPDATE scan_results SET start_time = ?, end_time = ? WHERE id = ?",
                (start, end, row_id),
            )
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
                           times: list[float], source_path: str = "",
                           source_model: str = "") -> None:
        """Save timestamps as hard-negative training examples."""
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
