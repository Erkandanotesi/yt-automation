from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unsplash_media_stage import (
    UnsplashMediaSettings,
    download_candidate,
    search_unsplash,
)


def settings(root: Path) -> UnsplashMediaSettings:
    return UnsplashMediaSettings(
        api_key="test-key-not-used-on-network",
        request_result_limit=5,
        search_limit=5,
        minimum_width=320,
        minimum_height=180,
        minimum_relevance_score=10,
        destination=root,
    )


def photo(
    photo_id: str,
    alt: str,
    *,
    width: int = 1920,
    height: int = 1080,
) -> dict:
    return {
        "id": photo_id,
        "width": width,
        "height": height,
        "description": None,
        "alt_description": alt,
        "urls": {
            "regular": f"https://images.unsplash.com/photo-{photo_id}?w=1080",
            "raw": f"https://images.unsplash.com/photo-{photo_id}",
        },
        "links": {
            "html": f"https://unsplash.com/photos/{photo_id}",
            "download_location": (
                f"https://api.unsplash.com/photos/{photo_id}/download"
            ),
        },
        "user": {
            "name": "Example Photographer",
            "links": {"html": "https://unsplash.com/@example"},
        },
        "tags_preview": [],
    }


class UnsplashMediaStageTests(unittest.TestCase):
    def test_search_keeps_grounded_scene_and_rejects_unrelated_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "results": [
                    photo("server-photo", "Engineers working beside servers in a data center"),
                    photo("rocket-photo", "A rocket launching into a clear evening sky"),
                ]
            }
            with patch(
                "unsplash_media_stage._fetch_json",
                return_value=(payload, {"remaining": "49"}),
            ):
                result = search_unsplash(
                    query="data center servers engineers",
                    required_entity="data center",
                    context="Engineers reported a data center server outage.",
                    settings=settings(Path(directory)),
                )

            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(
                result["candidates"][0]["photo_id"],
                "server-photo",
            )
            self.assertEqual(
                result["rejected"][0]["reason"],
                "metadata_does_not_ground_query",
            )

    def test_search_uses_safe_landscape_filters_and_hides_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fetch = unittest.mock.Mock(return_value=({"results": []}, {}))
            with patch("unsplash_media_stage._fetch_json", fetch):
                result = search_unsplash(
                    query="privacy security camera",
                    required_entity="security camera",
                    settings=settings(Path(directory)),
                )

            requested_url = fetch.call_args.args[0]
            self.assertIn("orientation=landscape", requested_url)
            self.assertIn("content_filter=high", requested_url)
            self.assertIn("order_by=relevant", requested_url)
            self.assertNotIn("test-key", json.dumps(result))

    def test_download_is_tracked_once_and_reused_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {
                "provider": "unsplash",
                "photo_id": "data-center-12",
                "title": "Engineers in a data center",
                "description": "Engineers working beside data center servers",
                "creator": "Example Photographer",
                "creator_url": (
                    "https://unsplash.com/@example?"
                    "utm_source=tech_news_automation&utm_medium=referral"
                ),
                "source_page": (
                    "https://unsplash.com/photos/data-center-12?"
                    "utm_source=tech_news_automation&utm_medium=referral"
                ),
                "download_url": (
                    "https://images.unsplash.com/photo-data-center-12?w=1080"
                ),
                "download_location": (
                    "https://api.unsplash.com/photos/data-center-12/download"
                ),
                "original_url": (
                    "https://images.unsplash.com/photo-data-center-12"
                ),
                "relevance_score": 18.0,
                "relevance_evidence": {
                    "query_hits": ["data", "center", "servers"],
                    "entity_hits": ["data", "center"],
                },
            }
            fake_image = b"test-jpeg-data"
            with (
                patch(
                    "unsplash_media_stage._fetch_bytes",
                    return_value=(fake_image, "image/jpeg"),
                ) as fetch_bytes,
                patch(
                    "unsplash_media_stage._track_download",
                    return_value={"url": candidate["download_url"]},
                ) as track_download,
                patch(
                    "unsplash_media_stage.probe_image",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                first = download_candidate(
                    candidate,
                    query="data center servers",
                    required_entity="data center",
                    context="A data center reported an outage.",
                    settings=settings(root),
                )
                second = download_candidate(
                    candidate,
                    query="server room engineers",
                    required_entity="server room",
                    context="Engineers inspected the server room.",
                    settings=settings(root),
                )
                history_before_recovery = json.loads(
                    Path(second["sidecar"]).read_text(encoding="utf-8")
                )["query_history"]
                Path(second["sidecar"]).unlink()
                recovered = download_candidate(
                    candidate,
                    query="data center servers",
                    required_entity="data center",
                    context="A data center reported an outage.",
                    settings=settings(root),
                )

            image = Path(first["file"])
            sidecar = json.loads(
                Path(first["sidecar"]).read_text(encoding="utf-8")
            )
            self.assertEqual(image.read_bytes(), fake_image)
            self.assertEqual(sidecar["provider"], "unsplash")
            self.assertEqual(sidecar["provider_page_id"], "data-center-12")
            self.assertEqual(sidecar["creator"], "Example Photographer")
            self.assertTrue(sidecar["attribution_required"])
            self.assertEqual(len(history_before_recovery), 2)
            self.assertIn("utm_source=tech_news_automation", sidecar["source_url"])
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertTrue(recovered["cached"])
            self.assertEqual(first["file"], second["file"])
            self.assertEqual(first["file"], recovered["file"])
            fetch_bytes.assert_called_once()
            track_download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
