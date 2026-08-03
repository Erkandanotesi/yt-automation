from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pexels_media_stage import (
    PexelsMediaSettings,
    download_candidate,
    search_pexels,
)


def settings(root: Path) -> PexelsMediaSettings:
    return PexelsMediaSettings(
        api_key="test-key-not-used-on-network",
        request_result_limit=5,
        search_limit=5,
        minimum_width=320,
        minimum_height=180,
        minimum_relevance_score=10,
        destination=root,
    )


def photo(
    photo_id: int,
    alt: str,
    *,
    width: int = 1920,
    height: int = 1080,
) -> dict:
    return {
        "id": photo_id,
        "width": width,
        "height": height,
        "url": f"https://www.pexels.com/photo/example-{photo_id}/",
        "photographer": "Example Photographer",
        "photographer_url": "https://www.pexels.com/@example/",
        "alt": alt,
        "src": {
            "landscape": f"https://images.pexels.com/photos/{photo_id}/landscape.jpeg",
            "original": f"https://images.pexels.com/photos/{photo_id}/original.jpeg",
        },
    }


class PexelsMediaStageTests(unittest.TestCase):
    def test_search_keeps_grounded_scene_and_rejects_unrelated_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "photos": [
                    photo(10, "Engineers working beside servers in a data center"),
                    photo(11, "A rocket launching into a clear evening sky"),
                ]
            }
            with patch(
                "pexels_media_stage._fetch_json",
                return_value=(payload, {"remaining": "19999"}),
            ):
                result = search_pexels(
                    query="data center servers engineers",
                    required_entity="data center",
                    context="Engineers reported a data center server outage.",
                    settings=settings(Path(directory)),
                )

            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["candidates"][0]["photo_id"], 10)
            self.assertEqual(
                result["rejected"][0]["reason"],
                "metadata_does_not_ground_query",
            )

    def test_search_uses_landscape_large_filter_and_authorization_is_not_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fetch = unittest.mock.Mock(return_value=({"photos": []}, {}))
            with patch("pexels_media_stage._fetch_json", fetch):
                result = search_pexels(
                    query="privacy security camera",
                    required_entity="security camera",
                    settings=settings(Path(directory)),
                )

            requested_url = fetch.call_args.args[0]
            self.assertIn("orientation=landscape", requested_url)
            self.assertIn("size=large", requested_url)
            self.assertNotIn("test-key", json.dumps(result))

    def test_download_writes_image_and_pexels_attribution_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {
                "provider": "pexels",
                "photo_id": 12,
                "title": "Engineers in a data center",
                "description": "Engineers working beside data center servers",
                "creator": "Example Photographer",
                "creator_url": "https://www.pexels.com/@example/",
                "source_page": "https://www.pexels.com/photo/example-12/",
                "download_url": "https://images.pexels.com/photos/12/landscape.jpeg",
                "original_url": "https://images.pexels.com/photos/12/original.jpeg",
                "relevance_score": 18.0,
                "relevance_evidence": {
                    "query_hits": ["data", "center", "servers"],
                    "entity_hits": ["data", "center"],
                },
            }
            fake_image = b"test-jpeg-data"
            with (
                patch(
                    "pexels_media_stage._fetch_bytes",
                    return_value=(fake_image, "image/jpeg"),
                ),
                patch(
                    "pexels_media_stage.probe_image",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                result = download_candidate(
                    candidate,
                    query="data center servers",
                    required_entity="data center",
                    context="A data center reported an outage.",
                    settings=settings(root),
                )

            image = Path(result["file"])
            sidecar = json.loads(Path(result["sidecar"]).read_text(encoding="utf-8"))
            self.assertEqual(image.read_bytes(), fake_image)
            self.assertEqual(sidecar["provider"], "pexels")
            self.assertEqual(sidecar["creator"], "Example Photographer")
            self.assertTrue(sidecar["attribution_required"])


if __name__ == "__main__":
    unittest.main()
