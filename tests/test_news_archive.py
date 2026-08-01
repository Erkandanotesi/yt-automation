from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from news_archive import ArchiveSettings, NewsArchive, content_sha256


class NewsArchiveTests(unittest.TestCase):
    def archive(self, root: Path) -> NewsArchive:
        return NewsArchive(
            ArchiveSettings(
                root=root,
                candidate_limit=3,
                local_similarity_threshold=0.10,
                ai_text_character_limit=4000,
                cache_namespace="unit-test",
            )
        )

    @staticmethod
    def inspect(archive: NewsArchive, *, url: str, title: str, text: str, **kwargs):
        return archive.inspect_and_store(
            canonical_url=url,
            title=title,
            source="Test Feed",
            source_url=url,
            published_utc="2026-08-01T00:00:00+00:00",
            category="tech",
            article_text=text,
            video_name="Video 6",
            **kwargs,
        )

    def test_files_include_story_id_and_content_hash_and_exact_duplicate_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = self.archive(Path(directory))
            text = "OpenAI released a security update for enterprise customers. " * 20
            first = self.inspect(
                archive,
                url="https://first.example/security-update",
                title="OpenAI releases enterprise security update",
                text=text,
            )
            archive.mark_used(
                archive_id=first["record"]["archive_id"],
                video_name="Video 5",
                order=1,
                narration="OpenAI released a security update.",
            )

            calls = []
            second = self.inspect(
                archive,
                url="https://second.example/reported-update",
                title="Enterprise users receive OpenAI security update",
                text=text,
                compare_with_ai=lambda payload: calls.append(payload),
            )

            filename = first["record"]["source_filename"]
            self.assertIn(first["record"]["story_id"], filename)
            self.assertIn(content_sha256(text)[:16], filename)
            self.assertTrue(Path(first["record"]["source_file"]).is_file())
            self.assertTrue(Path(second["record"]["source_file"]).is_file())
            self.assertTrue(second["decision"]["is_duplicate"])
            self.assertEqual(second["decision"]["method"], "exact_content_sha256")
            self.assertEqual(calls, [])

    def test_cross_feed_ai_comparison_sends_files_and_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = self.archive(Path(directory))
            previous_text = (
                "Nvidia announced the Atlas graphics chip on Tuesday. The chip targets "
                "laptops and begins shipping in September. " * 16
            )
            first = self.inspect(
                archive,
                url="https://first.example/nvidia-atlas",
                title="Nvidia announces Atlas laptop graphics chip",
                text=previous_text,
            )
            archive.mark_used(
                archive_id=first["record"]["archive_id"],
                video_name="Video 5",
                order=2,
                narration="Nvidia announced Atlas.",
            )
            new_text = (
                "The Atlas laptop GPU was unveiled by Nvidia on Tuesday and is due to "
                "ship in September. The company described its mobile graphics plans. " * 16
            )
            calls = []

            def compare(payload: dict) -> dict:
                calls.append(payload)
                return {
                    "is_duplicate": True,
                    "duplicate_archive_id": first["record"]["archive_id"],
                    "confidence": 0.98,
                    "reason": "Both files report the same Nvidia Atlas announcement.",
                }

            second = self.inspect(
                archive,
                url="https://second.example/atlas-gpu",
                title="Nvidia unveils Atlas GPU for laptops",
                text=new_text,
                compare_with_ai=compare,
            )
            repeated = self.inspect(
                archive,
                url="https://second.example/atlas-gpu",
                title="Nvidia unveils Atlas GPU for laptops",
                text=new_text,
                compare_with_ai=lambda payload: self.fail("cache was not used"),
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]["previous_files"]), 1)
            self.assertIn("Atlas graphics chip", calls[0]["previous_files"][0]["text"])
            self.assertTrue(second["decision"]["is_duplicate"])
            self.assertTrue(repeated["decision"]["cache_hit"])
            self.assertFalse(repeated["decision"]["ai_request_performed"])

    def test_unrelated_story_avoids_ai_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = self.archive(Path(directory))
            first = self.inspect(
                archive,
                url="https://first.example/linux-kernel",
                title="Linux kernel fixes filesystem bug",
                text="Linux maintainers fixed an ext4 filesystem bug. " * 20,
            )
            archive.mark_used(
                archive_id=first["record"]["archive_id"],
                video_name="Video 5",
                order=3,
                narration="Linux fixed a filesystem bug.",
            )
            result = self.inspect(
                archive,
                url="https://second.example/robot-vacuum",
                title="Home robot vacuum gains a quieter motor",
                text="A household vacuum robot received a quieter motor and longer battery. " * 20,
                compare_with_ai=lambda payload: self.fail("unrelated files reached AI"),
            )
            self.assertFalse(result["decision"]["is_duplicate"])
            self.assertEqual(result["decision"]["method"], "local_no_match")

    def test_completed_project_backfill_is_idempotent_and_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.archive(root / "archive")
            projects = root / "projects"
            project = projects / "Video 1"
            news = project / "Haberler"
            news.mkdir(parents=True)
            original = news / "01_story_test-orijinal.txt"
            summary = news / "01_story_test.txt"
            original.write_text("A completed archived technology article. " * 20, encoding="utf-8")
            summary.write_text("A short narration of the archived article.\n", encoding="utf-8")
            (project / "project_info.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "video_name": "Video 1",
                        "category": "tech",
                        "stories": [
                            {
                                "order": 1,
                                "title": "Archived technology article",
                                "source_title": "Archived technology article",
                                "source": "Example",
                                "source_url": "https://example.com/archive",
                                "published_utc": "2026-07-01T00:00:00+00:00",
                                "original": str(original),
                                "text": str(summary),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            first = archive.backfill_projects(projects)
            second = archive.backfill_projects(projects)

            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["already_present"], 1)
            self.assertEqual(archive.status()["used_record_count"], 1)


if __name__ == "__main__":
    unittest.main()
