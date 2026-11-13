from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from file_locations import (
    DATA_ROOT,
    NEWS_ARCHIVE_ROOT,
    NEWS_HISTORY_RESET_FILE,
    PROCESSED_URLS_FILE,
    PROJECTS_ROOT,
)
from file_utils import atomic_write_json


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _assert_inside(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    root = parent.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"Refusing to modify a path outside the data root: {resolved}")


def _active_lock_files(projects_root: Path) -> list[Path]:
    return sorted(projects_root.glob("Video */.news_studio.lock"))


def clean_news_history(
    *,
    data_root: Path = DATA_ROOT,
    archive_root: Path = NEWS_ARCHIVE_ROOT,
    processed_urls_file: Path = PROCESSED_URLS_FILE,
    reset_file: Path = NEWS_HISTORY_RESET_FILE,
    projects_root: Path = PROJECTS_ROOT,
    dry_run: bool = False,
) -> dict:
    data_root = data_root.resolve()
    archive_root = archive_root.resolve()
    processed_urls_file = processed_urls_file.resolve()
    reset_file = reset_file.resolve()
    projects_root = projects_root.resolve()
    for path in (archive_root, processed_urls_file, reset_file):
        _assert_inside(path, data_root)

    locks = _active_lock_files(projects_root)
    if locks:
        raise RuntimeError(
            "News Studio appears to be running. Close it before cleaning history. "
            f"Lock: {locks[0]}"
        )

    excluded_projects = sorted(
        item.name for item in projects_root.glob("Video *") if item.is_dir()
    )
    backup_root = data_root / "history_backups" / _timestamp()
    _assert_inside(backup_root, data_root)
    planned_backups = [
        str(path)
        for path in (archive_root, processed_urls_file, reset_file)
        if path.exists()
    ]
    result = {
        "status": "DRY_RUN" if dry_run else "NEWS_HISTORY_CLEANED",
        "backup_directory": str(backup_root),
        "backed_up": planned_backups,
        "excluded_backfill_projects": excluded_projects,
        "processed_url_count": 0,
        "archive_record_count": 0,
    }
    if dry_run:
        return result

    backup_root.mkdir(parents=True, exist_ok=False)
    if archive_root.exists():
        shutil.move(str(archive_root), str(backup_root / archive_root.name))
    if processed_urls_file.exists():
        shutil.move(
            str(processed_urls_file),
            str(backup_root / processed_urls_file.name),
        )
    if reset_file.exists():
        shutil.move(str(reset_file), str(backup_root / reset_file.name))

    archive_root.mkdir(parents=True, exist_ok=True)
    (archive_root / "articles").mkdir(exist_ok=True)
    (archive_root / "summaries").mkdir(exist_ok=True)
    now = utc_now()
    atomic_write_json(
        archive_root / "index.json",
        {"schema_version": 1, "updated_at_utc": now, "records": {}},
    )
    atomic_write_json(
        archive_root / "similarity_cache.json",
        {"schema_version": 1, "updated_at_utc": now, "comparisons": {}},
    )
    atomic_write_json(
        processed_urls_file,
        {"updated_at": now, "last_video": "", "used_urls": []},
    )
    atomic_write_json(
        reset_file,
        {
            "schema_version": 1,
            "reset_at_utc": now,
            "backup_directory": str(backup_root),
            "excluded_backfill_projects": excluded_projects,
            "policy": (
                "Projects that existed at reset time are excluded from automatic "
                "archive backfill. Newly completed projects are tracked normally."
            ),
        },
    )
    return result


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely back up and reset all generated-news history."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive CLEAN confirmation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be reset without changing files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    if not args.yes and not args.dry_run:
        print("This resets URL and semantic duplicate history.")
        print("Existing history will be moved to a timestamped backup.")
        answer = input("Type CLEAN to continue: ").strip()
        if answer != "CLEAN":
            print("Cancelled; no files were changed.")
            return 0
    try:
        result = clean_news_history(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"[CLEAN HISTORY ERROR] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.dry_run:
        print("News history is empty. Future completed videos will be tracked normally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
