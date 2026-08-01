from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from file_utils import sha256_file
from youtube_upload import (
    YouTubeUploadSettings,
    upload_from_manifest,
    validate_chapters,
    validate_upload_manifest,
)


def settings(root: Path) -> YouTubeUploadSettings:
    return YouTubeUploadSettings(
        client_secrets_file=root / "client_secret.json",
        token_file=root / "token.json",
    )


def manifest(root: Path) -> dict:
    video = root / "final.mp4"
    thumbnail = root / "thumbnail.png"
    video.write_bytes(b"video")
    thumbnail.write_bytes(b"thumbnail")
    description = (
        "Episode copy.\n\n"
        "CHAPTERS\n"
        "0:00 First story\n"
        "1:39 Second story\n"
        "3:19 Third story\n\n"
        "SOURCES\nhttps://example.com\n"
    )
    return {
        "provider": "youtube_data_api_v3",
        "video_file": str(video),
        "video_sha256": sha256_file(video),
        "thumbnail_file": str(thumbnail),
        "thumbnail_sha256": sha256_file(thumbnail),
        "metadata": {
            "snippet": {
                "title": "Opening One Outlook Email May Run Attacker JavaScript",
                "description": description,
                "tags": ["technology news", "cybersecurity"],
                "categoryId": "28",
                "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
            },
        },
        "upload": {
            "mode": "resumable",
            "explicit_confirmation_required": True,
        },
    }


class YouTubeUploadTests(unittest.TestCase):
    def test_manifest_validates_without_credentials_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = validate_upload_manifest(manifest(root), settings(root))

        self.assertEqual(result["status"], "UPLOAD_VALIDATED")
        self.assertEqual(result["chapter_count"], 3)
        self.assertEqual(result["privacy_status"], "private")

    def test_chapters_must_begin_at_zero(self) -> None:
        description = (
            "CHAPTERS\n0:01 First\n0:20 Second\n0:40 Third\n\nSOURCES\n"
        )
        with self.assertRaisesRegex(ValueError, "must start at 0:00"):
            validate_chapters(description)

    def test_upload_requires_explicit_confirmation_before_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "youtube_upload_manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "--confirm-upload"):
                upload_from_manifest(
                    manifest_path,
                    settings(root),
                    confirm_upload=False,
                )


if __name__ == "__main__":
    unittest.main()
