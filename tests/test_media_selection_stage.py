from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from file_utils import sha256_file
from media_index import MediaIndexSettings, MediaRoot
from media_selection_stage import (
    MediaSelectionSettings,
    StoryMediaDecisions,
    WindowDecision,
    _enforce_selection_constraints,
    _load_selector_cache,
    _save_selector_cache,
    _validate_decisions,
    _window_entities,
    build_archive_windows,
    coverage_quality,
    resolve_wikimedia_fallbacks,
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
    def test_entity_context_is_carried_into_pronoun_window(self) -> None:
        story = {
            "title": "OpenAI prepares a new developer platform",
            "named_entities": {"companies": ["OpenAI"], "products": []},
            "phrases": [
                {
                    "start": 0,
                    "end": 3,
                    "text": "OpenAI introduced the platform.",
                }
            ],
            "sentences": [],
            "visual_beats": [],
        }

        entities = _window_entities(
            story,
            "It will become available to developers next week.",
            start=3,
        )

        self.assertEqual(entities, ["OpenAI"])

    def test_broad_only_ai_choice_is_redirected_to_online_fallback(self) -> None:
        window = {
            "window_id": "story_01_archive_001",
            "entities": ["OpenAI"],
            "visual_queries": ["OpenAI developer platform"],
            "shortlist": [
                {
                    "asset_id": "generic-ai",
                    "context_grounded": False,
                }
            ],
        }
        decisions = StoryMediaDecisions(
            decisions=[
                WindowDecision(
                    window_id=window["window_id"],
                    selected_asset_id="generic-ai",
                    confidence=0.95,
                    selection_reason=(
                        "The generic technology image loosely matches the category."
                    ),
                )
            ]
        )

        validated = _validate_decisions(
            decisions,
            [window],
            {"title": "OpenAI prepares a new developer platform"},
            minimum_confidence=0.70,
        )

        self.assertEqual(
            validated[window["window_id"]].relevance_status,
            "images_are_irrelevant",
        )
        self.assertIsNone(validated[window["window_id"]].selected_asset_id)

    def test_low_confidence_choice_is_redirected_to_online_fallback(self) -> None:
        window = {
            "window_id": "story_01_archive_002",
            "entities": ["Nvidia"],
            "visual_queries": ["Nvidia GPU"],
            "shortlist": [
                {
                    "asset_id": "nvidia-gpu",
                    "context_grounded": True,
                }
            ],
        }
        decisions = StoryMediaDecisions(
            decisions=[
                WindowDecision(
                    window_id=window["window_id"],
                    selected_asset_id="nvidia-gpu",
                    confidence=0.55,
                    selection_reason="The entity matches but the exact event is uncertain.",
                )
            ]
        )

        validated = _validate_decisions(
            decisions,
            [window],
            {"title": "Nvidia announces a new GPU"},
            minimum_confidence=0.70,
        )

        self.assertEqual(
            validated[window["window_id"]].relevance_status,
            "images_are_irrelevant",
        )

    def test_image_usage_is_capped_twice_across_stories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = [
                make_asset(
                    root,
                    f"Media/asset_{index}.png",
                    ["technology"],
                    "cleared",
                )
                for index in range(3)
            ]
            assets_by_id = {asset["asset_id"]: asset for asset in assets}
            shortlist = [
                {
                    **asset,
                    "local_score": 10 - index,
                }
                for index, asset in enumerate(assets)
            ]
            usage: Counter[str] = Counter()

            for story_number in range(2):
                windows = [
                    {
                        "window_id": f"story_{story_number}_window_{index}",
                        "shortlist": shortlist,
                    }
                    for index in range(3)
                ]
                decisions = {
                    window["window_id"]: WindowDecision(
                        window_id=window["window_id"],
                        selected_asset_id=assets[0]["asset_id"],
                        confidence=0.8,
                        selection_reason="The preferred image matches this test window.",
                    )
                    for window in windows
                }
                _enforce_selection_constraints(
                    windows,
                    decisions,
                    assets_by_id,
                    usage,
                    2,
                )

            self.assertEqual(sum(usage.values()), 6)
            self.assertTrue(all(count <= 2 for count in usage.values()))

    def test_raw_selector_decisions_are_cached_before_downstream_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selector_cache" / "story.json"
            decisions = StoryMediaDecisions(
                decisions=[
                    WindowDecision(
                        window_id="story_01_archive_001",
                        relevance_status="relevant",
                        selected_asset_id="foreign-asset",
                        confidence=0.7,
                        selection_reason="This raw decision still needs local validation.",
                    )
                ]
            )

            _save_selector_cache(
                path,
                story_id="story_1",
                fingerprint="fingerprint-a",
                decisions=decisions,
            )

            cached = _load_selector_cache(path, "fingerprint-a")
            self.assertIsNotNone(cached)
            self.assertEqual(cached.decisions[0].selected_asset_id, "foreign-asset")
            self.assertIsNone(_load_selector_cache(path, "fingerprint-b"))

    def test_asset_outside_window_shortlist_is_repaired_locally(self) -> None:
        window = {
            "window_id": "story_01_archive_013",
            "text": "The security team continued its investigation.",
            "entities": [],
            "scene_intent": "neutral_technology_context",
            "shortlist": [
                {
                    "asset_id": "safe-local",
                    "sha256": "c" * 64,
                    "local_score": 1.0,
                }
            ],
        }
        decisions = StoryMediaDecisions(
            decisions=[
                WindowDecision(
                    window_id=window["window_id"],
                    relevance_status="relevant",
                    selected_asset_id="asset-from-another-window",
                    confidence=0.8,
                    selection_reason="The model selected a misplaced candidate asset.",
                )
            ]
        )

        validated = _validate_decisions(decisions, [window], {"title": "Security"})

        repaired = validated[window["window_id"]]
        self.assertEqual(repaired.selected_asset_id, "safe-local")
        self.assertEqual(repaired.confidence, 0.15)

    def test_omitted_window_is_filled_from_its_own_shortlist(self) -> None:
        window = {
            "window_id": "story_01_archive_014",
            "text": "Engineers continued working on a fix.",
            "entities": [],
            "scene_intent": "neutral_technology_context",
            "shortlist": [
                {
                    "asset_id": "safe-local",
                    "sha256": "d" * 64,
                    "local_score": 1.0,
                }
            ],
        }

        validated = _validate_decisions(
            StoryMediaDecisions(decisions=[]),
            [window],
            {"title": "Service disruption"},
        )

        self.assertEqual(
            validated[window["window_id"]].selected_asset_id,
            "safe-local",
        )
    def test_story_title_entity_repairs_split_window_online_fallback(self) -> None:
        window = {
            "window_id": "story_03_archive_003",
            "text": "service safely. The bank detected attempted fraud on some accounts.",
            "entities": [],
            "scene_intent": "neutral_technology_context",
            "shortlist": [
                {
                    "asset_id": "generic-tech",
                    "sha256": "a" * 64,
                    "local_score": 1.0,
                }
            ],
        }
        story = {
            "title": "Charities remain locked out of CAF Bank online accounts",
            "named_entities": {"companies_organizations": ["CAF Bank"]},
            "visual_beats": [],
        }
        decisions = StoryMediaDecisions(
            decisions=[
                WindowDecision(
                    window_id=window["window_id"],
                    relevance_status="images_are_irrelevant",
                    selected_asset_id=None,
                    confidence=0.8,
                    selection_reason="The local candidates do not show this bank outage.",
                    online_search_query="CAF banking service outage",
                    required_entity="CAF banking service",
                )
            ]
        )

        validated = _validate_decisions(decisions, [window], story)

        repaired = validated[window["window_id"]]
        self.assertEqual(repaired.relevance_status, "images_are_irrelevant")
        self.assertEqual(repaired.required_entity, "CAF Bank")
        self.assertIn("CAF Bank", repaired.online_search_query)

    def test_ungrounded_online_entity_without_story_entity_uses_local_fallback(self) -> None:
        window = {
            "window_id": "story_01_archive_001",
            "text": "The service remains unavailable while engineers investigate.",
            "entities": [],
            "scene_intent": "neutral_technology_context",
            "shortlist": [
                {
                    "asset_id": "generic-tech",
                    "sha256": "b" * 64,
                    "local_score": 1.0,
                }
            ],
        }
        decisions = StoryMediaDecisions(
            decisions=[
                WindowDecision(
                    window_id=window["window_id"],
                    relevance_status="images_are_irrelevant",
                    selected_asset_id=None,
                    confidence=0.7,
                    selection_reason="No local candidate depicts the exact outage.",
                    online_search_query="Invented Company outage",
                    required_entity="Invented Company",
                )
            ]
        )

        validated = _validate_decisions(
            decisions,
            [window],
            {"title": "Service outage"},
        )

        repaired = validated[window["window_id"]]
        self.assertEqual(repaired.relevance_status, "relevant")
        self.assertEqual(repaired.selected_asset_id, "generic-tech")
        self.assertEqual(repaired.confidence, 0.15)

    def test_existing_wikimedia_asset_is_reused_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = make_asset(
                root,
                "Local/generic_technology.png",
                ["generic", "technology"],
                "user_confirmed_no_copyright",
            )
            online = make_asset(
                root,
                "Online/wikimedia_microsoft-outlook_logo.png",
                ["microsoft", "outlook", "logo"],
                "public_domain",
            )
            online["source_url"] = (
                "https://commons.wikimedia.org/wiki/File:Microsoft_Outlook.png"
            )
            online["license"] = {
                "status": "public_domain",
                "name": "CC0",
                "attribution_required": False,
            }
            window = {
                "window_id": "story_01_archive_001",
                "text": "Microsoft Outlook added a new control.",
                "entities": ["Microsoft Outlook"],
                "scene_intent": "neutral_technology_context",
                "visual_queries": [],
                "shortlist": [
                    {
                        "asset_id": local["asset_id"],
                        "sha256": local["sha256"],
                        "quality_flags": [],
                        "local_score": 1.0,
                    }
                ],
            }
            decision = WindowDecision(
                window_id=window["window_id"],
                relevance_status="images_are_irrelevant",
                selected_asset_id=None,
                confidence=0.9,
                selection_reason="The local image is unrelated.",
                online_search_query="Microsoft Outlook logo",
                required_entity="Microsoft Outlook",
            )
            with patch(
                "online_media_stage.search_wikimedia_commons"
            ) as online_search:
                resolved, refreshed, used, searches = resolve_wikimedia_fallbacks(
                    video_number=5,
                    windows=[window],
                    decisions={window["window_id"]: decision},
                    media_index={"assets": [local, online]},
                    index_settings=MediaIndexSettings(
                        index_path=root / "index.json",
                        roots=(MediaRoot("test", root, "unknown"),),
                    ),
                    settings=selection_settings(),
                    remaining_downloads=1,
                    remaining_searches=1,
                )

            online_search.assert_not_called()
            self.assertEqual(used, 0)
            self.assertEqual(searches, 0)
            self.assertEqual(refreshed["assets"], [local, online])
            self.assertEqual(
                resolved[window["window_id"]].selected_asset_id,
                online["asset_id"],
            )

    def test_irrelevant_ai_decision_uses_wikimedia_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = make_asset(
                root,
                "Local/generic_technology.png",
                ["generic", "technology"],
                "user_confirmed_no_copyright",
            )
            online = make_asset(
                root,
                "Online/microsoft_outlook_logo.png",
                ["microsoft", "outlook", "logo", "email"],
                "public_domain",
            )
            online["source_url"] = "https://commons.wikimedia.org/wiki/File:Outlook.png"
            online["creator"] = "Example Creator"
            online["license"] = {
                "status": "creative_commons_commercial_reuse",
                "name": "CC BY-SA 4.0",
                "url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "attribution_required": True,
                "attribution_text": "Example Creator; CC BY-SA 4.0",
            }
            window = {
                "window_id": "story_01_archive_001",
                "text": "Microsoft Outlook Web Access was targeted.",
                "entities": ["Microsoft Outlook"],
                "scene_intent": "neutral_technology_context",
                "visual_queries": [],
                "shortlist": [
                    {
                        "asset_id": local["asset_id"],
                        "sha256": local["sha256"],
                        "quality_flags": [],
                        "local_score": 1.0,
                    }
                ],
            }
            decision = WindowDecision(
                window_id=window["window_id"],
                relevance_status="images_are_irrelevant",
                selected_asset_id=None,
                confidence=0.8,
                selection_reason="The local image is unrelated to Microsoft Outlook.",
                online_search_query="Microsoft Outlook logo",
                required_entity="Microsoft Outlook",
            )
            index_settings = MediaIndexSettings(
                index_path=root / "index.json",
                roots=(MediaRoot("test", root, "unknown"),),
            )
            search_result = {
                "candidates": [
                    {
                        "page_id": 1,
                        "title": "Microsoft Outlook logo.png",
                        "license": {"allowed": True},
                    }
                ]
            }
            with (
                patch(
                    "online_media_stage.search_wikimedia_commons",
                    return_value=search_result,
                ),
                patch("online_media_stage.save_search_manifest"),
                patch(
                    "online_media_stage.download_candidate",
                    return_value={"sha256": online["sha256"]},
                ),
                patch(
                    "media_selection_stage.load_media_index",
                    return_value={"assets": [local, online]},
                ),
            ):
                resolved, refreshed, used, searches = resolve_wikimedia_fallbacks(
                    video_number=5,
                    windows=[window],
                    decisions={window["window_id"]: decision},
                    media_index={"assets": [local]},
                    index_settings=index_settings,
                    settings=selection_settings(),
                    remaining_downloads=1,
                    remaining_searches=1,
                )

            self.assertEqual(used, 1)
            self.assertEqual(searches, 1)
            self.assertEqual(len(refreshed["assets"]), 2)
            self.assertEqual(
                resolved[window["window_id"]].selected_asset_id,
                online["asset_id"],
            )
            self.assertEqual(window["shortlist"][0]["asset_id"], online["asset_id"])

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
