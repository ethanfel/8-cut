import json
import os


def build_annotation_json_path(folder: str) -> str:
    return os.path.join(folder, "dataset.json")


def remove_clip_annotation(folder: str, clip_path: str) -> None:
    """Remove the entry for *clip_path* from <folder>/dataset.json if present."""
    json_path = build_annotation_json_path(folder)
    if not os.path.exists(json_path):
        return
    abs_path = os.path.abspath(clip_path)
    with open(json_path, "r", encoding="utf-8") as f:
        try:
            entries = json.load(f)
        except (json.JSONDecodeError, ValueError):
            return
    entries = [e for e in entries if e.get("path") != abs_path]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")


def upsert_clip_annotation(folder: str, clip_path: str, label: str) -> None:
    """Insert or update one entry in <folder>/dataset.json.

    Each entry stores a path relative to *folder* and the sound label.
    Matches on ``path``; if an entry for the same clip already exists it is
    replaced (overwrite-export case).  Nothing is written when *label* is
    empty.
    """
    if not label.strip():
        return
    os.makedirs(folder, exist_ok=True)
    json_path = build_annotation_json_path(folder)
    entries: list[dict] = []
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except (json.JSONDecodeError, ValueError):
                entries = []
    abs_path = os.path.abspath(clip_path)
    entry: dict = {"path": abs_path, "label": label}
    for i, e in enumerate(entries):
        if e.get("path") == abs_path:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")
