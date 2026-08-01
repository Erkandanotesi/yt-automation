from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import news_pipeline
from file_locations import AUTOMATION_ROOT
from file_utils import sha256_file


class NewsPipelineConfigurationTests(unittest.TestCase):
    def test_ai_ranking_is_completed_with_local_reserves(self) -> None:
        candidates = [
            {
                "title": f"Technology story {index}",
                "source": "Test Feed",
                "published": news_pipeline.datetime.now(news_pipeline.timezone.utc),
                "rss_text": "A substantive technology report.",
                "rough_score": 70 - index,
            }
            for index in range(5)
        ]
        ai_result = news_pipeline.RankingResult(
            selected=[
                news_pipeline.RankedNews(index=0, score=90, reason="Best supplied story")
            ]
        )
        with (
            patch.object(news_pipeline, "TOP_N", 2),
            patch.object(news_pipeline, "RANKING_RESERVE", 2),
            patch.object(news_pipeline, "OPENAI_NEWS_SELECTION_MODEL", "selection-model"),
            patch.object(
                news_pipeline,
                "OPENAI_NEWS_SELECTION_REASONING_EFFORT",
                "medium",
            ),
            patch.object(
                news_pipeline, "structured_ai_call", return_value=ai_result
            ) as ai_call,
        ):
            ranked = news_pipeline.rank_candidates(candidates, "Technology news")
        self.assertEqual(len(ranked), 4)
        self.assertEqual(ranked[0].index, 0)
        self.assertEqual({item.index for item in ranked}, {0, 1, 2, 3})
        self.assertEqual(ai_call.call_args.kwargs["openai_model"], "selection-model")
        self.assertEqual(
            ai_call.call_args.kwargs["openai_reasoning_effort"], "medium"
        )

    def test_incomplete_project_reuses_validated_story_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            narration = root / "01_story.txt"
            source = root / "01_story-orijinal.txt"
            narration.write_text("A previously validated narration.\n", encoding="utf-8")
            source.write_text("A previously validated source article.\n", encoding="utf-8")
            project_info = root / "project_info.json"
            project_info.write_text(json.dumps({
                "status": "incomplete",
                "stories": [{
                    "order": 1,
                    "story_id": "story_saved",
                    "title": "Saved technology story",
                    "source_title": "Saved technology story",
                    "source": "Test Feed",
                    "source_url": "https://example.com/saved",
                    "published_utc": "2026-08-01T12:00:00+00:00",
                    "score": 80,
                    "ranking_reason": "Previously selected and validated.",
                    "text": str(narration),
                    "original": str(source),
                    "narration_sha256": sha256_file(narration),
                    "source_sha256": sha256_file(source),
                    "validation": {"factual_review": {}},
                }],
            }), encoding="utf-8")
            candidates: list[dict] = []
            with patch.object(news_pipeline, "PROJECT_INFO_PATH", project_info):
                restored = news_pipeline.load_incomplete_created_items(candidates)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["story_id"], "story_saved")
        self.assertEqual(len(candidates), 1)

    def test_launcher_pauses_only_after_a_failure(self) -> None:
        launcher = (AUTOMATION_ROOT / "create_video.bat").read_text(encoding="utf-8")
        self.assertIn('if not "%RESULT%"=="0"', launcher)
        self.assertIn("pause", launcher.casefold())
        self.assertLess(launcher.index("if not"), launcher.casefold().index("pause"))

    def test_central_roots_are_available_during_configuration_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / "Automation"
            projects = root / "Projects"
            with (
                patch.object(news_pipeline, "AUTOMATION_ROOT", automation),
                patch.object(news_pipeline, "PROJECTS_ROOT", projects),
                patch.object(news_pipeline, "PROVIDER", "openai"),
                patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
                patch.object(
                    news_pipeline.ArchiveSettings,
                    "from_environment",
                ) as archive_settings,
            ):
                news_pipeline.validate_configuration()

            self.assertTrue(automation.is_dir())
            self.assertTrue(projects.is_dir())
            archive_settings.return_value.validate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
