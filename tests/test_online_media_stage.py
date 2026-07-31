from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from online_media_stage import (
    OnlineMediaSettings,
    download_candidate,
    search_wikimedia_commons,
)


def settings(root: Path) -> OnlineMediaSettings:
    return OnlineMediaSettings(
        user_agent="TechNewsAutomationTest/1.0 (local tests)",
        search_limit=5,
        request_result_limit=5,
        minimum_width=320,
        minimum_height=180,
        destination=root,
    )


def metadata(value: str) -> dict:
    return {"value": value}


def page(
    page_id: int,
    title: str,
    description: str,
    *,
    license_name: str = "CC BY-SA 4.0",
) -> dict:
    return {
        "pageid": page_id,
        "title": f"File:{title}",
        "categories": [{"title": "Category:Microsoft Outlook"}],
        "imageinfo": [
            {
                "mime": "image/png",
                "width": 1920,
                "height": 1080,
                "url": f"https://upload.wikimedia.org/{page_id}.png",
                "thumburl": f"https://upload.wikimedia.org/{page_id}-1920px.png",
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}",
                "extmetadata": {
                    "LicenseShortName": metadata(license_name),
                    "LicenseUrl": metadata("https://creativecommons.org/licenses/by-sa/4.0/"),
                    "Artist": metadata("Example Creator"),
                    "ImageDescription": metadata(description),
                    "ObjectName": metadata(title),
                },
            }
        ],
    }


class OnlineMediaStageTests(unittest.TestCase):
    def test_search_rejects_wrong_entity_and_noncommercial_license(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "query": {
                    "pages": [
                        page(
                            1,
                            "Microsoft Outlook Web Access interface.png",
                            "Microsoft Outlook webmail and email interface.",
                        ),
                        {
                            **page(
                                2,
                                "Outlook weather chart.png",
                                "A weather outlook mentioning Microsoft products.",
                            ),
                            "categories": [{"title": "Category:Weather outlooks"}],
                        },
                        page(
                            3,
                            "Microsoft Outlook proprietary photo.png",
                            "Microsoft Outlook interface.",
                            license_name="CC BY-NC 4.0",
                        ),
                    ]
                }
            }
            with patch("online_media_stage._fetch_json", return_value=payload):
                result = search_wikimedia_commons(
                    query="Microsoft Outlook Web Access",
                    required_entity="Microsoft Outlook",
                    context="email attack against OWA",
                    settings=settings(root),
                )

            self.assertEqual(result["quality"]["status"], "passed")
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["candidates"][0]["page_id"], 1)
            reasons = {item["reason"] for item in result["rejected"]}
            self.assertIn("required_entity_not_proven_in_title", reasons)
            self.assertIn("license_not_accepted", reasons)

    def test_download_writes_isolated_image_and_attribution_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {
                "page_id": 10,
                "title": "Microsoft Outlook Web Access interface.png",
                "description": "Microsoft Outlook webmail.",
                "categories": ["Microsoft Outlook"],
                "creator": "Example Creator",
                "source_page": "https://commons.wikimedia.org/wiki/File:Example.png",
                "download_url": "https://upload.wikimedia.org/example.png",
                "original_url": "https://upload.wikimedia.org/original.png",
                "relevance_score": 25.0,
                "relevance_evidence": {
                    "required_entity": "Microsoft Outlook",
                    "entity_hits": ["microsoft", "outlook"],
                },
                "license": {
                    "allowed": True,
                    "status": "creative_commons_commercial_reuse",
                    "name": "CC BY-SA 4.0",
                    "url": "https://creativecommons.org/licenses/by-sa/4.0/",
                    "attribution_required": True,
                },
            }
            fake_image = b"test-png-data"
            with (
                patch(
                    "online_media_stage._fetch_bytes",
                    return_value=(fake_image, "image/png"),
                ),
                patch(
                    "online_media_stage.probe_image",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                result = download_candidate(
                    candidate,
                    query="Microsoft Outlook Web Access",
                    required_entity="Microsoft Outlook",
                    context="email attack against OWA",
                    settings=replace(settings(root), minimum_relevance_score=10),
                )

            image_path = Path(result["file"])
            sidecar_path = Path(result["sidecar"])
            self.assertTrue(image_path.is_file())
            self.assertTrue(sidecar_path.is_file())
            self.assertEqual(image_path.read_bytes(), fake_image)
            self.assertIn("CC BY-SA 4.0", sidecar_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
