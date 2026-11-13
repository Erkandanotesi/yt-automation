from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_news_history import clean_news_history
from news_archive import ArchiveSettings, NewsArchive


class CleanNewsHistoryTests(unittest.TestCase):
    def test_cleanup_backs_up_history_and_blocks_old_project_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            archive = data / "news_archive"
            archive.mkdir(parents=True)
            (archive / "articles").mkdir()
            (archive / "old.txt").write_text("old archive", encoding="utf-8")
            processed = data / "processed_urls.json"
            processed.write_text(
                json.dumps({"used_urls": ["https://example.com/used"]}),
                encoding="utf-8",
            )
            reset = data / "news_history_reset.json"
            projects = root / "projects"
            (projects / "Video 12").mkdir(parents=True)
            (projects / "Video 13").mkdir()

            result = clean_news_history(
                data_root=data,
                archive_root=archive,
                processed_urls_file=processed,
                reset_file=reset,
                projects_root=projects,
            )

            backup = Path(result["backup_directory"])
            self.assertEqual(result["status"], "NEWS_HISTORY_CLEANED")
            self.assertTrue((backup / "news_archive" / "old.txt").is_file())
            self.assertTrue((backup / "processed_urls.json").is_file())
            self.assertEqual(
                json.loads(processed.read_text(encoding="utf-8"))["used_urls"],
                [],
            )
            self.assertEqual(
                json.loads((archive / "index.json").read_text(encoding="utf-8"))[
                    "records"
                ],
                {},
            )
            self.assertEqual(
                json.loads(reset.read_text(encoding="utf-8"))[
                    "excluded_backfill_projects"
                ],
                ["Video 12", "Video 13"],
            )

    def test_cleanup_refuses_to_run_while_project_lock_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            projects = root / "projects"
            video = projects / "Video 14"
            video.mkdir(parents=True)
            (video / ".news_studio.lock").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "appears to be running"):
                clean_news_history(
                    data_root=data,
                    archive_root=data / "news_archive",
                    processed_urls_file=data / "processed_urls.json",
                    reset_file=data / "news_history_reset.json",
                    projects_root=projects,
                )

    def test_archive_backfill_honors_reset_project_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            (projects / "Video 12").mkdir(parents=True)
            (projects / "Video 14").mkdir()
            reset = root / "news_history_reset.json"
            reset.write_text(
                json.dumps({"excluded_backfill_projects": ["Video 12"]}),
                encoding="utf-8",
            )
            archive = NewsArchive(ArchiveSettings(root=root / "archive"))

            with patch("news_archive.NEWS_HISTORY_RESET_FILE", reset):
                result = archive.backfill_projects(projects)

            self.assertEqual(result["skipped_by_history_reset"], 1)
            self.assertEqual(result["imported"], 0)


if __name__ == "__main__":
    unittest.main()
