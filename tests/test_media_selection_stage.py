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
    _create_context_safe_fallback_asset,
    _effective_hero_images,
    _enforce_selection_constraints,
    _hero_exclusions_for_story,
    _is_pexels_asset,
    _local_repository_index,
    _load_selector_cache,
    _pexels_asset_matches,
    _pexels_candidate_matches_subject,
    _pexels_visual_concept,
    _resolve_context_safe_fallbacks,
    _save_selector_cache,
    _split_uncovered_interval,
    _validate_decisions,
    _window_excerpt,
    _window_text,
    _window_entities,
    _windows_requiring_online_fallback,
    build_archive_windows,
    coverage_quality,
    infer_scene_intent,
    pexels_query_prompt,
    resolve_pexels_fallbacks,
    run_media_selection,
    shortlist_assets,
)
from tooling import find_executable


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
    def test_initial_repository_pass_excludes_downloaded_pexels_assets(self) -> None:
        local = {
            "asset_id": "local-google-earth",
            "workspace_relative_path": "AI Generated/google-earth.png",
            "source_url": "",
        }
        pexels = {
            "asset_id": "pexels-globe",
            "workspace_relative_path": "Online Retrieved/Pexels/globe_123.jpg",
            "source_url": "https://www.pexels.com/photo/globe-123/",
        }

        local_index = _local_repository_index({"assets": [local, pexels]})

        self.assertEqual(local_index["assets"], [local])
        self.assertFalse(_is_pexels_asset(local))
        self.assertTrue(_is_pexels_asset(pexels))

    def test_same_story_hero_can_be_reused_but_other_story_hero_cannot(self) -> None:
        owners = {
            "own-checksum": {"story_google_earth"},
            "other-checksum": {"story_spaceflight"},
        }

        exclusions = _hero_exclusions_for_story(owners, "story_google_earth")

        self.assertNotIn("own-checksum", exclusions)
        self.assertIn("other-checksum", exclusions)

    def test_pexels_planner_prompt_preserves_google_earth_visual_context(self) -> None:
        prompt = pexels_query_prompt(
            {"title": "Google Earth's AI Makeover Lasted Less Than Two Days"},
            [
                {
                    "window_id": "story_01_archive_001",
                    "text": (
                        "Google Earth briefly let users transform satellite "
                        "imagery with an AI image tool before Google removed it."
                    ),
                    "timestamp_excerpt": "before Google removed it",
                    "entities": ["Google Earth", "Google"],
                    "scene_intent": "software_product",
                }
            ],
        )

        self.assertIn("Google Earth satellite map computer screen", prompt)
        self.assertIn("full_sentence_context", prompt)
        self.assertIn("Google Earth briefly let users", prompt)

    def test_neutral_local_choice_is_sent_to_pexels_query_planner(self) -> None:
        window = {
            "window_id": "story_01_archive_001",
            "shortlist": [
                {
                    "asset_id": "neutral-card",
                    "sha256": "neutral-sha",
                    "context_safe_fallback": True,
                }
            ],
        }
        decision = WindowDecision(
            window_id=window["window_id"],
            relevance_status="relevant",
            selected_asset_id="neutral-card",
            confidence=0.15,
            selection_reason="Temporary context-safe card awaiting online fallback.",
        )

        unresolved = _windows_requiring_online_fallback(
            [window],
            {window["window_id"]: decision},
            video_usage_counts=Counter(),
            maximum_uses=2,
        )

        self.assertEqual(unresolved, [window])

    def test_pexels_reuse_requires_original_query_and_visible_topic_match(self) -> None:
        cryptocurrency = {
            "source_url": "https://www.pexels.com/photo/bitcoin-123/",
            "filename": "bitcoin-coins-on-laptop.jpg",
            "local_caption": "bitcoin coins on laptop keyboard",
            "keywords": ["bitcoin", "cryptocurrency", "laptop"],
            "provider_metadata": {
                "query": "bitcoin cryptocurrency digital wallet",
                "required_entity": "cryptocurrency",
                "title": "Bitcoin coins on a laptop keyboard",
                "description": "Physical cryptocurrency coins beside a laptop",
            },
        }
        google_earth = {
            "source_url": "https://www.pexels.com/photo/satellite-map-456/",
            "filename": "satellite-map-on-monitor.jpg",
            "local_caption": "satellite map displayed on monitor",
            "keywords": ["satellite", "map", "monitor"],
            "provider_metadata": {
                "query": "Google Earth satellite map computer screen",
                "required_entity": "Google Earth satellite map interface",
                "title": "Satellite map displayed on a desktop monitor",
                "description": "A detailed satellite map on a workstation",
            },
        }

        query = "Google Earth satellite map computer screen"
        entity = "Google Earth satellite map interface"
        self.assertFalse(_pexels_asset_matches(cryptocurrency, query, entity))
        self.assertTrue(_pexels_asset_matches(google_earth, query, entity))

    def test_google_search_page_does_not_pass_as_google_earth(self) -> None:
        generic_google = {
            "title": "Person using a laptop with Google search on screen",
            "description": "A Google website is open on a laptop outdoors",
            "source_page": "https://www.pexels.com/photo/google-website-123/",
        }
        satellite_map = {
            "title": "Detailed satellite map displayed on a monitor",
            "description": "Satellite imagery and a map fill the screen",
            "source_page": "https://www.pexels.com/photo/satellite-map-456/",
        }
        query = "Google Earth satellite map computer screen"
        entity = "Google Earth satellite map interface"

        self.assertFalse(
            _pexels_candidate_matches_subject(generic_google, query, entity)
        )
        self.assertTrue(
            _pexels_candidate_matches_subject(satellite_map, query, entity)
        )

    def test_ai_irrelevant_decision_keeps_strong_named_local_asset(self) -> None:
        window = {
            "window_id": "story_01_archive_001",
            "text": "Google Earth displayed transformed satellite imagery.",
            "entities": ["Google Earth"],
            "scene_intent": "software_product",
            "shortlist": [
                {
                    "asset_id": "google-earth-local",
                    "sha256": "f" * 64,
                    "local_score": 55.0,
                    "context_grounded": True,
                    "grounding_basis": "exact_entity",
                    "score_components": {"entity_hits": ["Google Earth"]},
                }
            ],
        }
        decisions = StoryMediaDecisions(
            decisions=[
                WindowDecision(
                    window_id=window["window_id"],
                    relevance_status="images_are_irrelevant",
                    confidence=0.8,
                    selection_reason="The legacy selector preferred online search.",
                    online_search_query="Google Earth satellite map",
                    required_entity="Google Earth",
                )
            ]
        )

        repaired = _validate_decisions(
            decisions,
            [window],
            {"title": "Google Earth AI feature"},
            minimum_confidence=0.45,
        )[window["window_id"]]

        self.assertEqual(repaired.relevance_status, "relevant")
        self.assertEqual(repaired.selected_asset_id, "google-earth-local")

    def test_exact_entity_local_recovery_survives_confidence_gate(self) -> None:
        window = {
            "window_id": "story_01_archive_001",
            "text": "Google Earth displayed transformed satellite imagery.",
            "entities": ["Google Earth"],
            "scene_intent": "software_product",
            "shortlist": [
                {
                    "asset_id": "google-earth-local",
                    "sha256": "e" * 64,
                    "local_score": 55.0,
                    "context_grounded": True,
                    "grounding_basis": "exact_entity",
                }
            ],
        }
        decisions = StoryMediaDecisions(
            decisions=[
                WindowDecision(
                    window_id=window["window_id"],
                    relevance_status="relevant",
                    selected_asset_id="legacy-pexels-choice",
                    confidence=0.8,
                    selection_reason="A cached choice predates local-first ranking.",
                )
            ]
        )

        repaired = _validate_decisions(
            decisions,
            [window],
            {"title": "Google Earth AI feature"},
            minimum_confidence=0.45,
        )[window["window_id"]]

        self.assertEqual(repaired.relevance_status, "relevant")
        self.assertEqual(repaired.selected_asset_id, "google-earth-local")
        self.assertGreaterEqual(repaired.confidence, 0.45)

    def test_tiny_tail_is_rebalanced_without_exceeding_maximum_hold(self) -> None:
        settings = replace(
            selection_settings(),
            minimum_window_seconds=5,
            target_window_seconds=7,
            maximum_window_seconds=9,
        )
        windows = _split_uncovered_interval(
            0,
            16.149,
            [0, 7.082, 16.149],
            settings,
        )

        self.assertAlmostEqual(windows[0][0], 0)
        self.assertAlmostEqual(windows[-1][1], 16.149)
        self.assertTrue(all(end - start <= 9.001 for start, end in windows))

    def test_window_uses_full_containing_sentence_for_entity_context(self) -> None:
        story = {
            "title": "Microsoft responds",
            "phrases": [
                {
                    "start": 0,
                    "end": 2,
                    "text": "Microsoft has confirmed that,",
                },
                {
                    "start": 2,
                    "end": 4,
                    "text": "the allegations are false.",
                },
            ],
            "sentences": [
                {
                    "start": 0,
                    "end": 4,
                    "text": "Microsoft has confirmed that, the allegations are false.",
                }
            ],
        }

        self.assertEqual(
            _window_excerpt(story, 2, 4),
            "the allegations are false.",
        )
        self.assertEqual(
            _window_text(story, 2, 4),
            "Microsoft has confirmed that, the allegations are false.",
        )

    def test_pexels_query_broadens_exact_brand_without_losing_topic(self) -> None:
        window = {
            "text": "COLDCARD users reported a Bitcoin wallet firmware flaw.",
            "timestamp_excerpt": "COLDCARD users reported a Bitcoin wallet flaw.",
            "entities": ["COLDCARD"],
            "scene_intent": "cybersecurity",
        }
        decision = WindowDecision(
            window_id="window_1",
            relevance_status="images_are_irrelevant",
            confidence=0.8,
            selection_reason="No local photo depicts this wallet story.",
            online_search_query="COLDCARD Bitcoin wallet firmware",
            required_entity="COLDCARD",
        )

        key, query, entity = _pexels_visual_concept(window, decision)

        self.assertEqual(key, "cryptocurrency")
        self.assertEqual(query, "bitcoin cryptocurrency digital wallet")
        self.assertEqual(entity, "cryptocurrency")
        self.assertNotIn("coldcard", query.casefold())

    def test_old_phrase_only_manifest_expands_to_sentence_punctuation(self) -> None:
        story = {
            "title": "Microsoft responds",
            "phrases": [
                {"start": 0, "end": 2, "text": "Microsoft has confirmed that,"},
                {"start": 2, "end": 4, "text": "the allegations are false."},
                {"start": 4, "end": 6, "text": "A separate topic follows."},
            ],
            "sentences": [],
        }

        self.assertEqual(
            _window_text(story, 2, 4),
            "Microsoft has confirmed that, the allegations are false.",
        )

    def test_software_launch_does_not_select_a_rocket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rocket = make_asset(
                root,
                "SpaceX/spacex-launch.png",
                ["spacex", "rocket", "launch"],
                "user_confirmed_no_copyright",
            )
            person = make_asset(
                root,
                "People/tom-evslin-portrait.png",
                ["tom", "evslin", "portrait"],
                "user_confirmed_no_copyright",
            )
            text = (
                "Tom Evslin argued AT&T should become an ISP, offer unlimited "
                "pricing, launch quickly, and adapt."
            )
            window = {
                "text": text,
                "entities": ["Tom Evslin", "AT&T"],
                "scene_intent": infer_scene_intent(text),
                "visual_queries": [],
            }

            shortlist = shortlist_assets(
                {"assets": [rocket, person]},
                window,
                selection_settings(),
            )
            by_id = {item["asset_id"]: item for item in shortlist}

            self.assertNotEqual(window["scene_intent"], "space")
            self.assertTrue(by_id[person["asset_id"]]["context_grounded"])
            self.assertFalse(by_id[rocket["asset_id"]]["context_grounded"])

    def test_stale_thumbnail_asset_is_excluded_from_shortlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thumbnail = make_asset(
                root,
                "Media/Thumbnails/openai phone launch.png",
                ["openai", "phone", "launch"],
                "user_confirmed_no_copyright",
            )
            regular = make_asset(
                root,
                "Media/OpenAI/openai office.png",
                ["openai", "office"],
                "user_confirmed_no_copyright",
            )

            shortlist = shortlist_assets(
                {"assets": [thumbnail, regular]},
                {
                    "text": "OpenAI announced a new phone product.",
                    "entities": ["OpenAI"],
                    "scene_intent": "company_or_product",
                    "visual_queries": [],
                },
                selection_settings(),
            )

            self.assertIn(regular["asset_id"], {item["asset_id"] for item in shortlist})
            self.assertNotIn(
                thumbnail["asset_id"],
                {item["asset_id"] for item in shortlist},
            )

    @unittest.skipUnless(find_executable("ffmpeg"), "FFmpeg is not available")
    def test_real_context_safe_fallback_is_rendered_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            root.mkdir()
            window = {
                "window_id": "story_01_archive_010",
                "text": "A claim without an honest matching photograph.",
                "entities": ["Example Platform"],
            }
            with patch("media_selection_stage.project_dir", return_value=root):
                asset = _create_context_safe_fallback_asset(5, window)

            self.assertTrue(Path(asset["absolute_path"]).is_file())
            self.assertEqual(asset["width"], 1280)
            self.assertEqual(asset["height"], 720)
            self.assertTrue(asset["context_safe_fallback"])

    @unittest.skipUnless(find_executable("ffmpeg"), "FFmpeg is not available")
    def test_context_safe_variant_has_distinct_rendered_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            root.mkdir()
            window = {
                "window_id": "story_02_archive_008",
                "text": "A neutral fallback scene.",
                "entities": [],
            }
            with patch("media_selection_stage.project_dir", return_value=root):
                base = _create_context_safe_fallback_asset(5, window)
                variant = _create_context_safe_fallback_asset(
                    5,
                    {**window, "_context_safe_variant": 1},
                )

            self.assertNotEqual(base["sha256"], variant["sha256"])
            self.assertNotEqual(base["absolute_path"], variant["absolute_path"])

    def test_enforcer_retries_after_a_fallback_checksum_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exhausted = make_asset(root, "Media/exhausted.png", ["tech"], "cleared")
            collision = make_asset(root, "Media/collision.png", ["neutral"], "cleared")
            unique = make_asset(root, "Media/unique.png", ["neutral"], "cleared")
            collision["context_safe_fallback"] = True
            unique["context_safe_fallback"] = True
            window = {
                "window_id": "story_02_archive_008",
                "shortlist": [{**exhausted, "local_score": 10.0}],
            }
            calls: list[int] = []

            def fallback_factory(value: dict) -> dict:
                variant = int(value.get("_context_safe_variant", 0))
                calls.append(variant)
                return collision if variant == 1 else unique

            selected = _enforce_selection_constraints(
                [window],
                {
                    window["window_id"]: WindowDecision(
                        window_id=window["window_id"],
                        selected_asset_id=exhausted["asset_id"],
                        confidence=0.8,
                        selection_reason="Reviewed asset.",
                    )
                },
                {exhausted["asset_id"]: exhausted},
                Counter({exhausted["sha256"]: 2, collision["sha256"]: 2}),
                2,
                fallback_factory=fallback_factory,
            )

            self.assertEqual(calls, [1, 2])
            self.assertEqual(
                selected[0]["selected_asset"]["asset_id"], unique["asset_id"]
            )

    def test_enforcer_prefers_exact_story_asset_for_its_second_allowed_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exact = make_asset(
                root,
                "Media/google-earth-map.png",
                ["google", "earth", "map"],
                "cleared",
            )
            weaker = make_asset(
                root,
                "Media/starlink-earth.png",
                ["starlink", "earth"],
                "cleared",
            )
            exact_candidate = {
                **exact,
                "local_score": 55.0,
                "context_grounded": True,
                "grounding_basis": "exact_entity",
            }
            weaker_candidate = {
                **weaker,
                "local_score": 20.0,
                "context_grounded": True,
                "grounding_basis": "exact_entity",
            }
            window = {
                "window_id": "story_google_earth_archive_001",
                "shortlist": [exact_candidate, weaker_candidate],
            }
            decision = WindowDecision(
                window_id=window["window_id"],
                selected_asset_id=exact["asset_id"],
                confidence=0.8,
                selection_reason="The exact Google Earth asset matches.",
                alternative_asset_ids=[weaker["asset_id"]],
            )

            selected = _enforce_selection_constraints(
                [window],
                {window["window_id"]: decision},
                {exact["asset_id"]: exact, weaker["asset_id"]: weaker},
                Counter({exact["sha256"]: 1}),
                2,
            )

            self.assertEqual(
                selected[0]["selected_asset"]["asset_id"],
                exact["asset_id"],
            )

    def test_one_distinctive_context_term_is_relevant_enough(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = make_asset(
                root,
                "Media/urban_transport.png",
                ["robotaxi", "transport"],
                "user_confirmed_no_copyright",
            )
            result = shortlist_assets(
                {"assets": [asset]},
                {
                    "text": "The robotaxi service expanded into another city.",
                    "entities": [],
                    "scene_intent": "vehicle_or_factory",
                    "visual_queries": [],
                },
                selection_settings(),
            )

            self.assertTrue(result[0]["context_grounded"])
            self.assertEqual(
                result[0]["grounding_basis"],
                "distinctive_context",
            )

    def test_weak_verb_overlap_does_not_ground_an_unrelated_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = make_asset(
                root,
                "Media/new-york-court-blocks-kalshi.png",
                ["york", "court", "block", "kalshi"],
                "user_confirmed_no_copyright",
            )
            result = shortlist_assets(
                {"assets": [unrelated]},
                {
                    "text": "Chrome may block a hijacker extension by default.",
                    "entities": ["Chrome"],
                    "scene_intent": "technology_concept",
                    "visual_queries": [],
                },
                selection_settings(),
            )

            self.assertFalse(result[0]["context_grounded"])
            self.assertNotIn("block", result[0]["distinctive_overlap_tokens"])

    def test_other_current_story_hero_assets_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            own = make_asset(
                root,
                "Media/chrome-browser.png",
                ["chrome", "browser"],
                "user_confirmed_no_copyright",
            )
            other_story = make_asset(
                root,
                "Media/chrome-extension.png",
                ["chrome", "extension"],
                "user_confirmed_no_copyright",
            )
            result = shortlist_assets(
                {"assets": [own, other_story]},
                {
                    "text": "Chrome changed extension behavior.",
                    "entities": ["Chrome"],
                    "scene_intent": "technology_concept",
                    "visual_queries": [],
                },
                selection_settings(),
                excluded_sha={other_story["sha256"]},
            )

            self.assertEqual([item["asset_id"] for item in result], [own["asset_id"]])

    def test_effective_hero_hold_never_exceeds_visual_ceiling(self) -> None:
        images = _effective_hero_images(
            {
                "images": [
                    {
                        "image_index": 1,
                        "time_range": {"start": 2.0, "end": 15.0},
                        "validation": {"status": "passed"},
                    },
                    {
                        "image_index": 2,
                        "time_range": {"start": 16.0, "end": 21.0},
                        "validation": {"status": "passed"},
                    },
                ]
            },
            9.0,
        )

        self.assertEqual(images[0]["time_range"]["end"], 11.0)
        self.assertLessEqual(
            images[0]["time_range"]["end"] - images[0]["time_range"]["start"],
            9.0,
        )

    def test_asset_already_used_twice_can_be_removed_before_ai_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exhausted = make_asset(
                root,
                "Media/chrome-exhausted.png",
                ["chrome", "browser"],
                "user_confirmed_no_copyright",
            )
            available = make_asset(
                root,
                "Media/chrome-available.png",
                ["chrome", "extension"],
                "user_confirmed_no_copyright",
            )
            result = shortlist_assets(
                {"assets": [exhausted, available]},
                {
                    "text": "Chrome changed extension behavior.",
                    "entities": ["Chrome"],
                    "scene_intent": "technology_concept",
                    "visual_queries": [],
                },
                selection_settings(),
                excluded_sha={exhausted["sha256"]},
            )

            self.assertEqual(
                [item["asset_id"] for item in result], [available["asset_id"]]
            )

    def test_missing_related_image_uses_context_safe_card_without_failure(self) -> None:
        window = {
            "window_id": "story_01_archive_009",
            "text": "The claim has no honest photographic match.",
            "entities": ["Example Platform"],
            "visual_queries": ["Example Platform policy"],
            "shortlist": [],
        }
        decision = WindowDecision(
            window_id=window["window_id"],
            relevance_status="images_are_irrelevant",
            confidence=0.9,
            selection_reason="No local or Pexels image represents this exact claim.",
            online_search_query="Example Platform policy",
            required_entity="Example Platform",
        )
        asset = {
            "asset_id": "safe_fallback_unique",
            "absolute_path": "safe.png",
            "workspace_relative_path": "safe.png",
            "filename": "safe.png",
            "sha256": "abc123",
            "width": 1280,
            "height": 720,
            "aspect_ratio": 16 / 9,
            "crop_retention_16_9": 1.0,
            "source_url": "",
            "creator": "Tech Reports local renderer",
            "license": {"status": "user_confirmed_no_copyright"},
            "quality_flags": [],
            "context_safe_fallback": True,
        }

        with patch(
            "media_selection_stage._create_context_safe_fallback_asset",
            return_value=asset,
        ):
            resolved = _resolve_context_safe_fallbacks(
                5,
                [window],
                {window["window_id"]: decision},
            )

        self.assertEqual(
            resolved[window["window_id"]].selected_asset_id,
            "safe_fallback_unique",
        )
        self.assertTrue(window["shortlist"][0]["context_safe_fallback"])
        self.assertEqual(window["_transient_assets"], [asset])

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
                        alternative_asset_ids=[
                            assets[1]["asset_id"],
                            assets[2]["asset_id"],
                        ],
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

    def test_exhausted_reuse_cap_gets_unique_local_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exhausted = make_asset(
                root,
                "Media/exhausted.png",
                ["technology"],
                "cleared",
            )
            fallback = make_asset(
                root,
                "Media/context_safe.png",
                ["neutral", "background"],
                "cleared",
            )
            fallback["context_safe_fallback"] = True
            window = {
                "window_id": "story_03_archive_002",
                "shortlist": [{**exhausted, "local_score": 10.0}],
            }
            decisions = {
                window["window_id"]: WindowDecision(
                    window_id=window["window_id"],
                    selected_asset_id=exhausted["asset_id"],
                    confidence=0.8,
                    selection_reason="The original image matched the context.",
                )
            }
            usage = Counter({exhausted["sha256"]: 2})

            selected = _enforce_selection_constraints(
                [window],
                decisions,
                {exhausted["asset_id"]: exhausted},
                usage,
                2,
                fallback_factory=lambda _window: fallback,
            )

            self.assertEqual(
                selected[0]["selected_asset"]["asset_id"],
                fallback["asset_id"],
            )
            self.assertTrue(
                selected[0]["selected_asset"]["context_safe_fallback"]
            )
            self.assertEqual(usage[exhausted["sha256"]], 2)
            self.assertEqual(usage[fallback["sha256"]], 1)

    def test_enforcer_never_uses_an_unreviewed_shortlist_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = make_asset(root, "Media/reviewed.png", ["chrome"], "cleared")
            unrelated = make_asset(root, "Media/rocket.png", ["rocket"], "cleared")
            fallback = make_asset(root, "Media/safe.png", ["neutral"], "cleared")
            fallback["context_safe_fallback"] = True
            window = {
                "window_id": "story_05_archive_001",
                "shortlist": [
                    {**reviewed, "local_score": 10.0, "context_grounded": True},
                    {**unrelated, "local_score": 9.0, "context_grounded": True},
                ],
            }
            decision = WindowDecision(
                window_id=window["window_id"],
                selected_asset_id=reviewed["asset_id"],
                confidence=0.9,
                selection_reason="The reviewed Chrome visual matches.",
            )

            selected = _enforce_selection_constraints(
                [window],
                {window["window_id"]: decision},
                {
                    reviewed["asset_id"]: reviewed,
                    unrelated["asset_id"]: unrelated,
                },
                Counter({reviewed["sha256"]: 2}),
                2,
                fallback_factory=lambda _window: fallback,
            )

            self.assertEqual(
                selected[0]["selected_asset"]["asset_id"], fallback["asset_id"]
            )
            self.assertNotEqual(
                selected[0]["selected_asset"]["asset_id"], unrelated["asset_id"]
            )

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
            compatible = _load_selector_cache(
                path,
                "fingerprint-b",
                compatible_fingerprints={"fingerprint-a"},
            )
            self.assertIsNotNone(compatible)

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
    def test_existing_pexels_asset_is_reused_without_network(self) -> None:
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
                "Online/pexels_data-center_engineers-and-servers_123.jpg",
                ["data", "center", "engineers", "servers"],
                "pexels_license",
            )
            online["source_url"] = "https://www.pexels.com/photo/example-123/"
            online["creator"] = "Example Photographer"
            online["license"] = {
                "status": "pexels_license",
                "name": "Pexels License",
                "url": "https://www.pexels.com/license/",
                "attribution_required": True,
            }
            window = {
                "window_id": "story_01_archive_001",
                "text": "Engineers investigated a data center server outage.",
                "entities": ["data center"],
                "scene_intent": "technical_infrastructure",
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
                confidence=0.85,
                selection_reason="The local image does not depict infrastructure.",
                online_search_query="data center engineers servers",
                required_entity="data center",
            )
            with (
                patch(
                    "pexels_media_stage.search_pexels",
                    return_value={
                        "candidates": [],
                        "required_entity": "data center",
                    },
                ) as online_search,
                patch("pexels_media_stage.save_search_manifest"),
            ):
                resolved, refreshed, used, searches = resolve_pexels_fallbacks(
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

            online_search.assert_called_once()
            self.assertEqual((used, searches), (0, 1))
            self.assertEqual(refreshed["assets"], [local, online])
            self.assertEqual(
                resolved[window["window_id"]].selected_asset_id,
                online["asset_id"],
            )

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
