from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from file_utils import sha256_file
from lead_story_selection import (
    finalize_lead_story,
    selection_is_consistent,
    selection_is_pending,
)


def make_pending_project(root: Path, story_count: int = 5) -> list[dict]:
    news_dir = root / "Haberler"
    news_dir.mkdir(parents=True)
    stories: list[dict] = []
    for order in range(1, story_count + 1):
        narration = news_dir / f"{order:02d}_story_{order}.txt"
        source = news_dir / f"{order:02d}_story_{order}-orijinal.txt"
        narration.write_text(f"Narration {order}.\n", encoding="utf-8")
        source.write_text(f"Original article {order}.\n", encoding="utf-8")
        stories.append(
            {
                "order": order,
                "story_id": f"story_{order}",
                "title": f"News title {order}",
                "text": str(narration),
                "original": str(source),
                "narration_sha256": sha256_file(narration),
                "source_sha256": sha256_file(source),
            }
        )
    (root / "project_info.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "expected_count": story_count,
                "lead_story_selection_required": True,
                "lead_story_selection": {
                    "schema_version": 1,
                    "status": "pending",
                    "default_story_id": "story_1",
                    "timeout_seconds": 120,
                },
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return stories


class LeadStorySelectionTests(unittest.TestCase):
    def test_three_story_selection_is_supported_and_keeps_rank_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pending_project(root, story_count=3)
            choose = MagicMock(return_value="3")

            selection = finalize_lead_story(root, choose=choose, timeout_seconds=120)

            project = json.loads((root / "project_info.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [item["story_id"] for item in project["stories"]],
                ["story_3", "story_1", "story_2"],
            )
            self.assertEqual([item["order"] for item in project["stories"]], [1, 2, 3])
            self.assertEqual(selection["selected_story_id"], "story_3")
            self.assertTrue(selection_is_consistent(project))
            choose.assert_called_once_with(
                "Which news should be first and drive the YouTube title and thumbnail?",
                {"1": "News title 1", "2": "News title 2", "3": "News title 3"},
                default="1",
                timeout_seconds=120,
                separator="-",
            )

    def test_selected_story_moves_first_and_other_stories_keep_rank_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pending_project(root)
            choose = MagicMock(return_value="4")

            selection = finalize_lead_story(
                root,
                choose=choose,
                timeout_seconds=120,
            )

            project = json.loads(
                (root / "project_info.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["story_id"] for item in project["stories"]],
                ["story_4", "story_1", "story_2", "story_3", "story_5"],
            )
            self.assertEqual(
                [item["order"] for item in project["stories"]],
                [1, 2, 3, 4, 5],
            )
            self.assertEqual(selection["selected_story_id"], "story_4")
            self.assertTrue(selection_is_consistent(project))
            self.assertFalse(selection_is_pending(root))
            choose.assert_called_once_with(
                "Which news should be first and drive the YouTube title and thumbnail?",
                {
                    "1": "News title 1",
                    "2": "News title 2",
                    "3": "News title 3",
                    "4": "News title 4",
                    "5": "News title 5",
                },
                default="1",
                timeout_seconds=120,
                separator="-",
            )
            self.assertEqual(
                (root / "video_script.txt").read_text(encoding="utf-8"),
                "Narration 4.\n\nNarration 1.\n\nNarration 2.\n\nNarration 3.\n\nNarration 5.\n",
            )

    def test_saved_decision_is_reapplied_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pending_project(root)
            finalize_lead_story(root, choose=MagicMock(return_value="3"))

            project_path = root / "project_info.json"
            project = json.loads(project_path.read_text(encoding="utf-8"))
            project["lead_story_selection"]["status"] = "pending"
            project["stories"] = sorted(
                project["stories"], key=lambda item: int(item["story_id"].split("_")[1])
            )
            for order, story in enumerate(project["stories"], start=1):
                story["order"] = order
            project_path.write_text(json.dumps(project), encoding="utf-8")
            choose = MagicMock(side_effect=AssertionError("must not prompt again"))

            selection = finalize_lead_story(root, choose=choose)

            reapplied = json.loads(project_path.read_text(encoding="utf-8"))
            self.assertEqual(reapplied["stories"][0]["story_id"], "story_3")
            self.assertTrue(selection["decision_reused"])
            choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
