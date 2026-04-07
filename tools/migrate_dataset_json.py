#!/usr/bin/env python3
"""One-shot migration: generate dataset.json files from ~/.8cut.db history.

For each export folder that has records in the database, writes (or merges
into) a dataset.json with path (relative to the folder), label, and fps when
a <clip>.fps.txt sidecar exists alongside the WAV file.

Usage:
    python tools/migrate_dataset_json.py [--dry-run]
"""
import argparse
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path


DB_PATH = Path.home() / ".8cut.db"


def load_db_records(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT output_path, label FROM processed WHERE label != ''"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def read_fps_sidecar(output_path: str) -> float | None:
    """Read fps from <output_path>.fps.txt if it exists."""
    sidecar = output_path + ".fps.txt"
    if os.path.exists(sidecar):
        try:
            return float(Path(sidecar).read_text().strip())
        except ValueError:
            pass
    return None


def build_entries_for_folder(
    folder: str, records: list[dict]
) -> list[dict]:
    entries = []
    for rec in records:
        output_path = rec["output_path"]
        rel = os.path.relpath(output_path, folder)
        entry: dict = {"path": rel, "label": rec["label"]}
        fps = read_fps_sidecar(output_path)
        if fps is not None:
            entry["fps"] = fps
        entries.append(entry)
    return entries


def merge_into_json(json_path: str, new_entries: list[dict], dry_run: bool) -> int:
    """Merge new_entries into existing json_path, returning count added/updated."""
    existing: list[dict] = []
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except (json.JSONDecodeError, ValueError):
                existing = []

    by_path = {e["path"]: e for e in existing}
    changed = 0
    for entry in new_entries:
        if by_path.get(entry["path"]) != entry:
            by_path[entry["path"]] = entry
            changed += 1

    merged = list(by_path.values())
    if not dry_run and changed:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written without writing")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

    records = load_db_records(DB_PATH)
    if not records:
        print("No labelled records in database.")
        return

    # Group records by parent folder of output_path
    by_folder: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        folder = os.path.dirname(rec["output_path"])
        by_folder[folder].append(rec)

    total_written = 0
    for folder, recs in sorted(by_folder.items()):
        entries = build_entries_for_folder(folder, recs)
        json_path = os.path.join(folder, "dataset.json")
        changed = merge_into_json(json_path, entries, args.dry_run)
        status = "(dry run) " if args.dry_run else ""
        print(f"{status}{json_path}: {len(entries)} clips, {changed} added/updated")
        total_written += changed

    suffix = " (dry run)" if args.dry_run else ""
    print(f"\nTotal: {total_written} entries written{suffix}")


if __name__ == "__main__":
    main()
