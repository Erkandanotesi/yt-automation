from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_completed_upload_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest(root)
            manifest_path = root / "youtube_upload_manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            (root / "client_secret.json").write_text("{}", encoding="utf-8")
            previous = {
                "status": "UPLOAD_COMPLETE",
                "video_id": "existing123",
                "watch_url": "https://www.youtube.com/watch?v=existing123",
                "video_sha256": payload["video_sha256"],
            }
            (root / "youtube_upload_result.json").write_text(
                json.dumps(previous), encoding="utf-8"
            )

            with patch(
                "youtube_upload._google_dependencies",
                side_effect=AssertionError("A completed upload must not contact Google."),
            ):
                result = upload_from_manifest(
                    manifest_path,
                    settings(root),
                    confirm_upload=True,
                )

        self.assertEqual(result, previous)

    def test_thumbnail_pending_resumes_without_duplicate_video_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest(root)
            manifest_path = root / "youtube_upload_manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            (root / "client_secret.json").write_text("{}", encoding="utf-8")
            partial = {
                "schema_version": 1,
                "status": "VIDEO_UPLOADED_THUMBNAIL_PENDING",
                "video_id": "partial123",
                "watch_url": "https://www.youtube.com/watch?v=partial123",
                "video_sha256": payload["video_sha256"],
            }
            (root / "youtube_upload_result.json").write_text(
                json.dumps(partial), encoding="utf-8"
            )

            youtube = MagicMock()
            thumbnail_request = MagicMock()
            thumbnail_request.execute.return_value = {"items": [{"default": {}}]}
            youtube.thumbnails.return_value.set.return_value = thumbnail_request
            google = {
                "build": MagicMock(return_value=youtube),
                "HttpError": RuntimeError,
                "MediaFileUpload": MagicMock(),
            }
            with (
                patch("youtube_upload._google_dependencies", return_value=google),
                patch("youtube_upload._credentials", return_value=object()),
            ):
                result = upload_from_manifest(
                    manifest_path,
                    settings(root),
                    confirm_upload=True,
                )

            stored = json.loads(
                (root / "youtube_upload_result.json").read_text(encoding="utf-8")
            )

        youtube.videos.assert_not_called()
        self.assertEqual(result["status"], "UPLOAD_COMPLETE")
        self.assertEqual(stored["video_id"], "partial123")
        self.assertEqual(thumbnail_request.execute.call_count, 1)

    def test_new_upload_sends_metadata_then_sets_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest(root)
            manifest_path = root / "youtube_upload_manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            (root / "client_secret.json").write_text("{}", encoding="utf-8")

            progress = MagicMock()
            progress.progress.return_value = 0.5
            video_request = MagicMock()
            video_request.next_chunk.side_effect = [
                (progress, None),
                (None, {"id": "new123"}),
            ]
            thumbnail_request = MagicMock()
            thumbnail_request.execute.return_value = {"items": [{"default": {}}]}
            youtube = MagicMock()
            youtube.videos.return_value.insert.return_value = video_request
            youtube.thumbnails.return_value.set.return_value = thumbnail_request
            google = {
                "build": MagicMock(return_value=youtube),
                "HttpError": RuntimeError,
                "MediaFileUpload": MagicMock(),
            }
            with (
                patch("youtube_upload._google_dependencies", return_value=google),
                patch("youtube_upload._credentials", return_value=object()),
            ):
                result = upload_from_manifest(
                    manifest_path,
                    settings(root),
                    confirm_upload=True,
                )

            insert = youtube.videos.return_value.insert
            insert_kwargs = insert.call_args.kwargs

        self.assertEqual(result["status"], "UPLOAD_COMPLETE")
        self.assertEqual(result["video_id"], "new123")
        self.assertEqual(insert_kwargs["part"], "snippet,status")
        self.assertEqual(
            insert_kwargs["body"]["snippet"]["title"],
            payload["metadata"]["snippet"]["title"],
        )
        self.assertEqual(insert_kwargs["body"]["status"]["privacyStatus"], "private")
        self.assertFalse(insert_kwargs["notifySubscribers"])
        youtube.thumbnails.return_value.set.assert_called_once()


if __name__ == "__main__":
    unittest.main()
