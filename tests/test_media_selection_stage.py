from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from file_utils import sha256_file
from media_index import MediaIndexSettings, MediaRoot
from media_selection_stage import (
    MediaSelectionSettings,
    build_archive_windows,
    coverage_quality,
    run_media_selection,
    shortlist_assets,
)


def selection_settings() -> MediaSelectionSettings:
    return MediaSelectionSettings(
        model="gpt-5.6-luna",
        reasoning_effort="low",
        shortlist_size=4,
        minimum_window_seconds=3,
        target_window_seconds=5,
        maximum_window_seconds=7,
        minimum_local_score=1,
        api_retries=1,
        timeout_seconds=10,
        copy_selected_assets=False,
    )


def make_story() -> dict:
    phrases = [
        {
            "index": index,
            "text": text,
            "start": start,
            "end": start + 3,
        }
        for index, (start, text) in enumerate(
            [
                (0, "OpenAI introduced a phone app."),
                (3, "The interface puts ChatGPT on mobile."),
                (6, "Nvidia showed a new GPU."),
                (9, "The chip targets data centers."),
                (12, "SpaceX prepared Falcon 9."),
                (15, "The rocket moved to its launch site."),
            ]
        )
    ]
    return {
        "story_id": "story_000000000001",
        "order": 1,
        "title": "AI hardware and space roundup",
        "narration": {"start": 0, "end": 18, "duration": 18},
        "phrases": phrases,
        "sentences": phrases,
        "visual_beats": [
            {
                "start": item["start"],
                "end": item["end"],
                "important_claim": item["text"],
                "visual_query": item["text"],
            }
            for item in phrases
        ],
        "named_entities": {
            "companies": ["OpenAI", "Nvidia", "SpaceX"],
            "products": ["ChatGPT", "Falcon 9"],
        },
    }


def make_storyboard_story(root: Path) -> dict:
    hero_one = root / "hero_one.png"
    hero_two = root / "hero_two.png"
    hero_one.write_bytes(b"hero-one")
    hero_two.write_bytes(b"hero-two")
    return {
        "story_id": "story_000000000001",
        "images": [
            {
                "image_index": 1,
                "time_range": {"start": 3, "end": 6},
                "output_file": str(hero_one),
                "generation": {"file_sha256": sha256_file(hero_one)},
                "validation": {"status": "passed"},
            },
            {
                "image_index": 2,
                "time_range": {"start": 12, "end": 15},
                "output_file": str(hero_two),
                "generation": {"file_sha256": sha256_file(hero_two)},
                "validation": {"status": "passed"},
            },
        ],
    }


def make_asset(root: Path, name: str, keywords: list[str], license_status: str) -> dict:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode("utf-8"))
    checksum = sha256_file(path)
    return {
        "asset_id": f"asset_{checksum[:16]}",
        "absolute_path": str(path),
        "workspace_relative_path": str(path),
        "media_root": "test",
        "folder": str(path.parent),
        "filename": path.name,
        "sha256": checksum,
        "width": 1280,
        "height": 720,
        "aspect_ratio": 16 / 9,
        "crop_retention_16_9": 1.0,
        "keywords": keywords,
        "local_caption": path.stem.replace("_", " "),
        "license": {
            "status": license_status,
            "review_required": license_status == "unknown",
        },
        "quality_flags": [],
        "prior_use_count": 0,
        "usage_events": [],
    }


class MediaSelectionTests(unittest.TestCase):
    def test_uncovered_windows_follow_boundaries_and_cover_every_second(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = make_story()
            storyboard_story = make_storyboard_story(root)

            windows = build_archive_windows(
                story,
                storyboard_story,
                selection_settings(),
            )
            quality = coverage_quality(
                18,
                [(3, 6), (12, 15)],
                windows,
            )

            self.assertEqual(quality["status"], "passed")
            self.assertEqual(
                [(item["start"], item["end"]) for item in windows],
                [(0.0, 3.0), (6.0, 12.0), (15.0, 18.0)],
            )
            self.assertTrue(all(3 <= item["duration"] <= 7 for item in windows))

    def test_local_shortlist_prefers_precise_entity_and_cleared_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            precise = make_asset(
                root,
                "OpenAI/openai_chatgpt_phone.png",
                ["openai", "chatgpt", "phone", "mobile"],
                "user_confirmed_no_copyright",
            )
            generic = make_asset(
                root,
                "Combined/technology_background.png",
                ["technology", "background"],
                "unknown",
            )
            index = {"assets": [generic, precise]}
            window = {
                "text": "OpenAI launched ChatGPT on phones.",
                "entities": ["OpenAI", "ChatGPT"],
                "scene_intent": "mobile_device",
                "visual_queries": ["OpenAI ChatGPT phone app"],
            }

            result = shortlist_assets(index, window, selection_settings())

            self.assertEqual(result[0]["asset_id"], precise["asset_id"])
            self.assertEqual(
                result[0]["license_status"],
                "user_confirmed_no_copyright",
            )

    def test_low_confidence_query_uses_crop_safe_combined_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = make_asset(
                root,
                "Company/specific_product.png",
                ["specific", "product"],
                "unknown",
            )
            neutral = make_asset(
                root,
                "_combined/neutral_technology_background.png",
                ["neutral", "technology", "background"],
                "user_confirmed_no_copyright",
            )
            result = shortlist_assets(
                {"assets": [unrelated, neutral]},
                {
                    "text": "A completely unmatched abstract claim.",
                    "entities": [],
                    "scene_intent": "neutral_technology_context",
                    "visual_queries": [],
                },
                replace(selection_settings(), minimum_local_score=100),
            )

            self.assertEqual(result[0]["asset_id"], neutral["asset_id"])
            self.assertTrue(result[0]["neutral_fallback"])

    def test_offline_end_to_end_saves_hashes_coverage_and_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Video 5"
            project.mkdir()
            story = make_story()
            storyboard_story = make_storyboard_story(project)
            (project / "04_storyboard").mkdir()
            (project / "state.json").write_text(
                json.dumps(
                    {"stages": {"manual_separators": {"status": "READY"}}}
                ),
                encoding="utf-8",
            )
            (project / "story_timing.json").write_text(
                json.dumps(
                    {
                        "quality": {"manual_separators_validated": True},
                        "stories": [story],
                    }
                ),
                encoding="utf-8",
            )
            (project / "04_storyboard" / "storyboard.json").write_text(
                json.dumps({"stories": [storyboard_story]}),
                encoding="utf-8",
            )
            assets = [
                make_asset(
                    root,
                    "Media/openai phone.png",
                    ["openai", "phone", "chatgpt"],
                    "user_confirmed_no_copyright",
                ),
                make_asset(
                    root,
                    "Media/nvidia gpu.png",
                    ["nvidia", "gpu", "chip", "datacenter"],
                    "unknown",
                ),
                make_asset(
                    root,
                    "Media/spacex falcon launch.png",
                    ["spacex", "falcon", "rocket", "launch"],
                    "user_confirmed_no_copyright",
                ),
                make_asset(
                    root,
                    "Media/neutral technology.png",
                    ["neutral", "technology"],
                    "unknown",
                ),
            ]
            index_path = root / "media_index.json"
            index_path.write_text(
                json.dumps(
                    {
                        "index_fingerprint": "fixture",
                        "assets": assets,
                        "retired_assets": [],
                    }
                ),
                encoding="utf-8",
            )
            index_settings = MediaIndexSettings(
                index_path=index_path,
                roots=(MediaRoot("test", root / "Media", "unknown"),),
            )

            with patch(
                "media_selection_stage.project_dir",
                return_value=project,
            ):
                plan = run_media_selection(
                    video_number=5,
                    settings=selection_settings(),
                    index_settings=index_settings,
                    rescan=False,
                    offline=True,
                )

            self.assertEqual(plan["quality"]["status"], "passed")
            self.assertTrue(
                plan["quality"]["every_narration_interval_has_visual_coverage"]
            )
            self.assertTrue(plan["quality"]["no_immediate_duplicates"])
            saved = json.loads(
                (project / "05_visuals" / "media_plan.json").read_text(
                    encoding="utf-8"
                )
            )
            windows = saved["stories"][0]["windows"]
            self.assertEqual(len(windows), 3)
            self.assertTrue(
                all(item["selected_asset"]["sha256"] for item in windows)
            )
            self.assertTrue(
                all(
                    Path(item["selected_asset"]["absolute_path"]).is_file()
                    for item in windows
                )
            )


if __name__ == "__main__":
    unittest.main()
