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
    StockVideoCandidateReview,
    StockVideoWindowQuery,
    StoryStockVideoQueryPlan,
    _complete_stock_video_query_plan,
    resolve_stock_video_primaries,
    run_media_selection,
    stock_video_query_prompt,
)
from stock_video_stage import (
    StockVideoSettings,
    download_stock_video,
    search_stock_videos,
)


def provider_settings(root: Path) -> StockVideoSettings:
    return StockVideoSettings(
        pexels_api_key="pexels-test-key",
        pixabay_api_key="pixabay-test-key",
        request_interval_seconds=0,
        request_result_limit=6,
        candidate_limit=4,
        minimum_width=640,
        minimum_height=360,
        pexels_destination=root / "pexels",
        pixabay_destination=root / "pixabay",
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
        stock_video_review_candidate_limit=4,
        stock_video_min_review_confidence=0.65,
    )


def pexels_payload() -> dict:
    return {
        "videos": [
            {
                "id": 101,
                "width": 1920,
                "height": 1080,
                "duration": 12,
                "url": "https://www.pexels.com/video/engineers-in-server-room-101/",
                "image": "https://images.pexels.com/videos/101/preview.jpg",
                "user": {
                    "name": "Example Creator",
                    "url": "https://www.pexels.com/@example",
                },
                "video_files": [
                    {
                        "id": 1,
                        "quality": "hd",
                        "file_type": "video/mp4",
                        "width": 1280,
                        "height": 720,
                        "link": "https://videos.pexels.com/video-files/101/101-hd.mp4",
                    },
                    {
                        "id": 2,
                        "quality": "hd",
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "link": "https://videos.pexels.com/video-files/101/101-fullhd.mp4",
                    },
                ],
            },
            {
                "id": 102,
                "duration": 3,
                "url": "https://www.pexels.com/video/short-clip-102/",
                "image": "https://images.pexels.com/videos/102/preview.jpg",
                "video_files": [
                    {
                        "id": 3,
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "link": "https://videos.pexels.com/video-files/102/102.mp4",
                    }
                ],
            },
        ]
    }


def pixabay_payload() -> dict:
    return {
        "hits": [
            {
                "id": 201,
                "duration": 15,
                "pageURL": "https://pixabay.com/videos/server-room-engineer-201/",
                "user_id": 44,
                "user": "Example User",
                "tags": "server room, engineer, data center",
                "videos": {
                    "medium": {
                        "url": "https://cdn.pixabay.com/video/201_medium.mp4",
                        "width": 1920,
                        "height": 1080,
                        "size": 1234,
                        "thumbnail": "https://cdn.pixabay.com/video/201_thumb.jpg",
                    }
                },
            }
        ]
    }


def normalized_candidate(provider: str, video_id: str) -> dict:
    host = "pexels.com" if provider == "pexels" else "pixabay.com"
    return {
        "provider": provider,
        "candidate_id": f"{provider}:{video_id}",
        "video_id": video_id,
        "title": "Engineers inspecting server racks",
        "description": "Engineers inspecting server racks in a data center",
        "creator": "Example Creator",
        "source_page": f"https://www.{host}/video/{video_id}/",
        "preview_url": f"https://images.{host}/preview/{video_id}.jpg",
        "download_url": f"https://videos.{host}/files/{video_id}.mp4",
        "width": 1920,
        "height": 1080,
        "duration_seconds": 12.0,
        "aspect_ratio": 16 / 9,
        "crop_retention_16_9": 1.0,
        "api_result_rank": 1,
        "query": "engineer inspecting server racks",
        "license": {
            "status": f"{provider}_license",
            "name": f"{provider.title()} License",
            "url": f"https://www.{host}/license/",
            "attribution_required": True,
        },
    }


class StockVideoStageTests(unittest.TestCase):
    def test_pexels_search_uses_short_query_and_rejects_too_short_clip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "stock_video_stage._fetch_json",
                return_value=(pexels_payload(), {"remaining": "199"}),
            ) as fetch:
                result = search_stock_videos(
                    provider="pexels",
                    query="engineer inspecting server racks",
                    required_duration=7,
                    settings=provider_settings(root),
                )

            requested_url = fetch.call_args.args[1]
            self.assertIn("query=engineer+inspecting+server+racks", requested_url)
            self.assertIn("orientation=landscape", requested_url)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["candidates"][0]["video_id"], "101")
            self.assertEqual(result["candidates"][0]["width"], 1920)
            self.assertEqual(result["rejected"][0]["reason"], "shorter_than_visual_window")

    def test_pixabay_search_enables_safe_film_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "stock_video_stage._fetch_json",
                return_value=(pixabay_payload(), {"remaining": "99"}),
            ) as fetch:
                result = search_stock_videos(
                    provider="pixabay",
                    query="data center engineer",
                    required_duration=7,
                    settings=provider_settings(root),
                )

            requested_url = fetch.call_args.args[1]
            self.assertIn("video_type=film", requested_url)
            self.assertIn("safesearch=true", requested_url)
            self.assertNotIn("pixabay-test-key", json.dumps(result, default=str))
            self.assertEqual(result["candidates"][0]["candidate_id"], "pixabay:201")

    def test_download_is_validated_cached_and_records_query_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = normalized_candidate("pexels", "101")

            def fake_download(_url: str, path: Path, _settings: StockVideoSettings) -> None:
                path.write_bytes(b"validated-video")

            probe = {
                "duration_seconds": 12.0,
                "width": 1920,
                "height": 1080,
                "codec": "h264",
                "pixel_format": "yuv420p",
            }
            with (
                patch("stock_video_stage._stream_download", side_effect=fake_download) as stream,
                patch("stock_video_stage.probe_video", return_value=probe),
            ):
                first = download_stock_video(
                    candidate,
                    query="server room engineers",
                    context="Engineers inspected racks.",
                    required_duration=7,
                    settings=provider_settings(root),
                )
                second = download_stock_video(
                    candidate,
                    query="data center inspection",
                    context="A data center was inspected.",
                    required_duration=7,
                    settings=provider_settings(root),
                )

            sidecar = json.loads(Path(first["sidecar"]).read_text(encoding="utf-8"))
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(first["file"], second["file"])
            self.assertEqual(len(sidecar["query_history"]), 2)
            self.assertTrue(sidecar["attribution_required"])
            stream.assert_called_once()

    def test_query_planner_receives_full_context_but_enforces_short_provider_text(self) -> None:
        window = {
            "window_id": "story_01_archive_001",
            "text": "Engineers entered the data center and inspected several server racks.",
            "timestamp_excerpt": "inspected several server racks",
            "duration": 7,
            "entities": ["data center"],
            "scene_intent": "technical_infrastructure",
            "visual_queries": ["engineers inspect servers"],
        }
        overly_long = StoryStockVideoQueryPlan(
            queries=[
                StockVideoWindowQuery(
                    window_id=window["window_id"],
                    primary_query="engineers carefully inspecting many computer server racks inside data center",
                    alternative_queries=["server maintenance team working in modern data center"],
                    visual_subject="engineers inspecting server racks",
                    motion_description="Engineers move between racks and inspect hardware.",
                    rationale="The clause describes a concrete human action in a visible setting.",
                )
            ]
        )

        completed = _complete_stock_video_query_plan(overly_long, [window])
        prompt = stock_video_query_prompt({"story_id": "story", "title": "Server repair"}, [window])

        self.assertLessEqual(len(completed.queries[0].primary_query.split()), 7)
        self.assertIn(window["text"], prompt)
        self.assertIn(window["timestamp_excerpt"], prompt)
        self.assertIn("2 to 6 concrete English words", prompt)

    def test_resolver_uses_pexels_first_and_keeps_local_image_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = {
                "window_id": "story_01_archive_001",
                "text": "Engineers inspected server racks after the outage.",
                "timestamp_excerpt": "inspected server racks",
                "start": 0.0,
                "end": 7.0,
                "duration": 7.0,
                "entities": ["server racks"],
                "scene_intent": "technical_infrastructure",
                "visual_queries": ["engineers inspect server racks"],
                "shortlist": [
                    {
                        "asset_id": "local-image",
                        "sha256": "a" * 64,
                        "context_grounded": True,
                    }
                ],
            }
            query_plan = StoryStockVideoQueryPlan(
                queries=[
                    StockVideoWindowQuery(
                        window_id=window["window_id"],
                        primary_query="engineer inspecting server racks",
                        alternative_queries=["data center maintenance"],
                        visual_subject="engineer and server racks",
                        motion_description="An engineer moves along racks while checking hardware.",
                        rationale="This is the directly visible action in the timed clause.",
                    )
                ]
            )
            candidate = normalized_candidate("pexels", "101")
            searches: list[str] = []

            def fake_search(**kwargs: object) -> dict:
                provider = str(kwargs["provider"])
                searches.append(provider)
                return {
                    "schema_version": 1,
                    "provider": provider,
                    "created_at_utc": "2026-08-26T00:00:00+00:00",
                    "query": kwargs["query"],
                    "required_duration": kwargs["required_duration"],
                    "candidates": [candidate] if provider == "pexels" else [],
                }

            downloaded = {
                "file": str(root / "pexels-101.mp4"),
                "sidecar": str(root / "pexels-101.mp4.metadata.json"),
                "sha256": "b" * 64,
                "provider": "pexels",
                "provider_page_id": "101",
                "source_url": candidate["source_page"],
                "creator": "Example Creator",
                "width": 1920,
                "height": 1080,
                "duration_seconds": 12.0,
                "license": candidate["license"],
                "cached": False,
            }
            Path(downloaded["file"]).write_bytes(b"video")
            review = StockVideoCandidateReview(
                selected_candidate_id="pexels:101",
                confidence=0.91,
                visible_subject="An engineer standing beside rows of server racks.",
                selection_reason="The preview clearly shows the requested subject and setting.",
            )

            with (
                patch("media_selection_stage.project_dir", return_value=root),
                patch("stock_video_stage.search_stock_videos", side_effect=fake_search),
                patch("stock_video_stage.save_search_manifest"),
                patch("stock_video_stage.load_fresh_search_cache", return_value=None),
                patch("stock_video_stage.download_stock_video", return_value=downloaded),
                patch(
                    "media_selection_stage.review_stock_video_candidates_with_openai",
                    return_value=review,
                ),
            ):
                decisions, used, downloads, reviews, cache_hits, errors = (
                    resolve_stock_video_primaries(
                        video_number=7,
                        story={"story_id": "story_1", "title": "Server outage"},
                        windows=[window],
                        query_plan=query_plan,
                        settings=selection_settings(),
                        stock_settings=provider_settings(root),
                        remaining_searches={"pexels": 5, "pixabay": 5},
                        remaining_downloads=5,
                    )
                )

            decision = decisions[window["window_id"]]
            self.assertEqual(searches, ["pexels"])
            self.assertEqual(used, {"pexels": 1, "pixabay": 0})
            self.assertEqual((downloads, reviews, cache_hits), (1, 1, 0))
            self.assertEqual(errors, [])
            self.assertTrue(decision.selected_asset_id.startswith("asset_"))
            self.assertEqual(decision.alternative_asset_ids, ["local-image"])
            self.assertEqual(window["shortlist"][0]["asset_kind"], "video")
            transient = window["_transient_assets"][0]
            self.assertEqual(transient["kind"], "video")
            self.assertEqual(transient["source_start_seconds"], 2.5)

    def test_resolver_uses_pixabay_only_after_pexels_has_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = {
                "window_id": "story_01_archive_002",
                "text": "A technician checked the data center.",
                "timestamp_excerpt": "checked the data center",
                "start": 0.0,
                "end": 5.0,
                "duration": 5.0,
                "entities": ["data center"],
                "scene_intent": "technical_infrastructure",
                "visual_queries": [],
                "shortlist": [{"asset_id": "local", "sha256": "c" * 64}],
            }
            planned = StockVideoWindowQuery(
                window_id=window["window_id"],
                primary_query="technician checking data center",
                visual_subject="technician in data center",
                motion_description="A technician walks through a server room.",
                rationale="The narration describes a visible inspection action.",
            )
            pixabay = normalized_candidate("pixabay", "201")
            providers: list[str] = []

            def fake_search(**kwargs: object) -> dict:
                provider = str(kwargs["provider"])
                providers.append(provider)
                return {
                    "schema_version": 1,
                    "provider": provider,
                    "created_at_utc": "2026-08-26T00:00:00+00:00",
                    "query": kwargs["query"],
                    "required_duration": kwargs["required_duration"],
                    "candidates": [pixabay] if provider == "pixabay" else [],
                }

            downloaded = {
                "file": str(root / "pixabay-201.mp4"),
                "sidecar": str(root / "pixabay-201.mp4.metadata.json"),
                "sha256": "d" * 64,
                "provider": "pixabay",
                "provider_page_id": "201",
                "source_url": pixabay["source_page"],
                "creator": "Example Creator",
                "width": 1920,
                "height": 1080,
                "duration_seconds": 12.0,
                "license": pixabay["license"],
                "cached": True,
            }
            Path(downloaded["file"]).write_bytes(b"video")
            review = StockVideoCandidateReview(
                selected_candidate_id="pixabay:201",
                confidence=0.88,
                visible_subject="A technician walking between server cabinets.",
                selection_reason="The preview is a direct and non-misleading match.",
            )
            with (
                patch("media_selection_stage.project_dir", return_value=root),
                patch("stock_video_stage.search_stock_videos", side_effect=fake_search),
                patch("stock_video_stage.save_search_manifest"),
                patch("stock_video_stage.load_fresh_search_cache", return_value=None),
                patch("stock_video_stage.download_stock_video", return_value=downloaded),
                patch(
                    "media_selection_stage.review_stock_video_candidates_with_openai",
                    return_value=review,
                ),
            ):
                decisions, used, downloads, _, _, errors = resolve_stock_video_primaries(
                    video_number=7,
                    story={"story_id": "story_1", "title": "Data center inspection"},
                    windows=[window],
                    query_plan=StoryStockVideoQueryPlan(queries=[planned]),
                    settings=replace(selection_settings(), stock_video_min_review_confidence=0.8),
                    stock_settings=provider_settings(root),
                    remaining_searches={"pexels": 5, "pixabay": 5},
                    remaining_downloads=5,
                )

            self.assertEqual(providers, ["pexels", "pixabay"])
            self.assertEqual(used, {"pexels": 1, "pixabay": 1})
            self.assertEqual(downloads, 0)
            self.assertEqual(errors, [])
            selected_id = decisions[window["window_id"]].selected_asset_id
            self.assertIn("pixabay_201", selected_id)

    def test_end_to_end_video_primary_bypasses_image_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Video 7"
            (project / "04_storyboard").mkdir(parents=True)
            (project / "state.json").write_text(
                json.dumps({"stages": {"manual_separators": {"status": "READY"}}}),
                encoding="utf-8",
            )
            timed = {
                "story_id": "story_1",
                "order": 1,
                "title": "Engineers inspect server racks",
                "narration": {"start": 0, "end": 10, "duration": 10},
                "phrases": [
                    {
                        "index": 0,
                        "text": "Engineers inspected server racks after an outage.",
                        "start": 0,
                        "end": 10,
                    }
                ],
                "sentences": [
                    {
                        "index": 0,
                        "text": "Engineers inspected server racks after an outage.",
                        "start": 0,
                        "end": 10,
                    }
                ],
                "visual_beats": [
                    {
                        "start": 0,
                        "end": 10,
                        "important_claim": (
                            "Engineers inspected server racks after an outage."
                        ),
                        "visual_query": "engineers inspect server racks",
                    }
                ],
                "named_entities": {"companies": [], "products": []},
            }
            (project / "story_timing.json").write_text(
                json.dumps(
                    {
                        "quality": {"manual_separators_validated": True},
                        "stories": [timed],
                    }
                ),
                encoding="utf-8",
            )
            hero = project / "hero.png"
            hero.write_bytes(b"hero")
            (project / "04_storyboard" / "storyboard.json").write_text(
                json.dumps(
                    {
                        "stories": [
                            {
                                "story_id": "story_1",
                                "images": [
                                    {
                                        "image_index": 1,
                                        "time_range": {"start": 0, "end": 3},
                                        "output_file": str(hero),
                                        "generation": {
                                            "file_sha256": sha256_file(hero)
                                        },
                                        "validation": {"status": "passed"},
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            media_root = root / "Media"
            media_root.mkdir()
            local_image = media_root / "server-racks.png"
            local_image.write_bytes(b"local-image")
            local_sha = sha256_file(local_image)
            local_asset = {
                "asset_id": f"asset_{local_sha[:16]}",
                "absolute_path": str(local_image),
                "workspace_relative_path": str(local_image),
                "media_root": "test",
                "folder": str(media_root),
                "filename": local_image.name,
                "sha256": local_sha,
                "width": 1280,
                "height": 720,
                "aspect_ratio": 16 / 9,
                "crop_retention_16_9": 1.0,
                "keywords": ["server", "racks", "engineers"],
                "local_caption": "server racks",
                "license": {
                    "status": "user_confirmed_no_copyright",
                    "review_required": False,
                },
                "quality_flags": [],
                "prior_use_count": 0,
                "usage_events": [],
            }
            index_path = root / "media_index.json"
            index_path.write_text(
                json.dumps(
                    {
                        "index_fingerprint": "fixture",
                        "assets": [local_asset],
                        "retired_assets": [],
                    }
                ),
                encoding="utf-8",
            )
            index_settings = MediaIndexSettings(
                index_path=index_path,
                roots=(MediaRoot("test", media_root, "unknown"),),
            )
            candidate = normalized_candidate("pexels", "101")
            clip = root / "pexels-101.mp4"
            clip.write_bytes(b"downloaded-video")
            downloaded = {
                "file": str(clip),
                "sidecar": str(root / "pexels-101.mp4.metadata.json"),
                "sha256": sha256_file(clip),
                "provider": "pexels",
                "provider_page_id": "101",
                "source_url": candidate["source_page"],
                "creator": "Example Creator",
                "width": 1920,
                "height": 1080,
                "duration_seconds": 12.0,
                "license": candidate["license"],
                "cached": False,
            }
            query_plan = StoryStockVideoQueryPlan(
                queries=[
                    StockVideoWindowQuery(
                        window_id="story_1_archive_001",
                        primary_query="engineer inspecting server racks",
                        visual_subject="engineer and server racks",
                        motion_description="An engineer moves while inspecting racks.",
                        rationale="The exact clause describes this visible activity.",
                    )
                ]
            )
            review = StockVideoCandidateReview(
                selected_candidate_id="pexels:101",
                confidence=0.94,
                visible_subject="An engineer beside rows of server racks.",
                selection_reason="The preview directly matches the visible subject.",
            )

            def forbidden_selector(*_args: object, **_kwargs: object) -> object:
                raise AssertionError("Image selector should not run for a video-filled window")

            with (
                patch("media_selection_stage.project_dir", return_value=project),
                patch(
                    "media_selection_stage.StockVideoSettings.from_environment",
                    return_value=provider_settings(root),
                ),
                patch(
                    "media_selection_stage.plan_stock_video_queries_with_openai",
                    return_value=query_plan,
                ),
                patch(
                    "media_selection_stage.review_stock_video_candidates_with_openai",
                    return_value=review,
                ),
                patch(
                    "stock_video_stage.search_stock_videos",
                    return_value={
                        "schema_version": 1,
                        "provider": "pexels",
                        "created_at_utc": "2026-08-26T00:00:00+00:00",
                        "query": "engineer inspecting server racks",
                        "required_duration": 7.0,
                        "candidates": [candidate],
                    },
                ),
                patch("stock_video_stage.save_search_manifest"),
                patch("stock_video_stage.load_fresh_search_cache", return_value=None),
                patch("stock_video_stage.download_stock_video", return_value=downloaded),
                patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            ):
                plan = run_media_selection(
                    video_number=7,
                    settings=selection_settings(),
                    index_settings=index_settings,
                    selector=forbidden_selector,
                    rescan=False,
                )

            selected = plan["stories"][0]["windows"][0]["selected_asset"]
            self.assertEqual(selected["kind"], "video")
            self.assertEqual(selected["provider"], "pexels")
            self.assertEqual(selected["source_start_seconds"], 2.5)
            self.assertEqual(plan["stock_video_search_counts"]["pexels"], 1)
            self.assertEqual(plan["quality"]["stock_video_count"], 1)


if __name__ == "__main__":
    unittest.main()
