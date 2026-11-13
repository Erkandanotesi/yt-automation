from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from file_utils import sha256_file
from youtube_upload import (
    YouTubeUploadSettings,
    _youtube_scheduled_publish_times,
    next_publish_slot,
    upload_from_manifest,
    validate_chapters,
    validate_upload_manifest,
)
from youtube_schedule import YouTubeScheduleConfig


TWO_DAY_SCHEDULE = YouTubeScheduleConfig(publish_hour=20, interval_days=2)


def settings(root: Path) -> YouTubeUploadSettings:
    schedule_state = root / "youtube_schedule_state.json"
    if not schedule_state.exists():
        schedule_state.write_text(json.dumps({
            "last_scheduled_publish_at": "2026-08-01T20:00:00+03:00",
        }), encoding="utf-8")
    return YouTubeUploadSettings(
        client_secrets_file=root / "client_secret.json",
        token_file=root / "token.json",
        schedule_state_file=schedule_state,
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

    def test_manifest_from_old_schedule_configuration_is_recalculated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest(root)
            payload["scheduled_publish_at"] = "2099-08-03T20:00:00+03:00"
            payload["schedule_configuration"] = {
                "publish_hour": 20,
                "interval_days": 99,
                "timezone": "UTC+03:00",
            }
            expected = datetime(
                2099, 8, 5, 20, 0, tzinfo=timezone(timedelta(hours=3))
            )
            with patch("youtube_upload.next_publish_slot", return_value=expected):
                result = validate_upload_manifest(payload, settings(root))

        self.assertEqual(result["scheduled_publish_at"], expected.isoformat())

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
                patch(
                    "youtube_upload._youtube_scheduled_publish_times",
                    side_effect=AssertionError(
                        "thumbnail recovery must not recalculate scheduling"
                    ),
                ),
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
                patch(
                    "youtube_upload._youtube_scheduled_publish_times",
                    return_value=set(),
                ),
            ):
                result = upload_from_manifest(
                    manifest_path,
                    settings(root),
                    confirm_upload=True,
                )

            insert = youtube.videos.return_value.insert
            insert_kwargs = insert.call_args.kwargs
            schedule_state = json.loads(
                (root / "youtube_schedule_state.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "UPLOAD_COMPLETE")
        self.assertEqual(result["video_id"], "new123")
        self.assertEqual(schedule_state["last_youtube_video_id"], "new123")
        self.assertEqual(
            schedule_state["last_scheduled_publish_at"],
            result["scheduled_publish_at"],
        )
        self.assertEqual(insert_kwargs["part"], "snippet,status")
        self.assertEqual(
            insert_kwargs["body"]["snippet"]["title"],
            payload["metadata"]["snippet"]["title"],
        )
        self.assertEqual(insert_kwargs["body"]["status"]["privacyStatus"], "private")
        self.assertIn("publishAt", insert_kwargs["body"]["status"])
        self.assertEqual(insert_kwargs["body"]["snippet"]["defaultLanguage"], "en")
        self.assertEqual(insert_kwargs["body"]["snippet"]["defaultAudioLanguage"], "en")
        self.assertNotIn("SOURCES", insert_kwargs["body"]["snippet"]["description"])
        self.assertFalse(insert_kwargs["notifySubscribers"])
        youtube.thumbnails.return_value.set.assert_called_once()

    def test_local_last_uploaded_schedule_does_not_reserve_a_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "youtube_schedule_state.json"
            state.write_text(json.dumps({
                "last_scheduled_publish_at": "2026-08-01T20:00:00+03:00",
            }), encoding="utf-8")
            turkey = timezone(timedelta(hours=3))
            scheduled = next_publish_slot(
                now=datetime(2026, 8, 1, 11, 0, tzinfo=turkey),
                state_file=state,
                schedule_config=TWO_DAY_SCHEDULE,
            )
        self.assertEqual(scheduled.isoformat(), "2026-08-01T20:00:00+03:00")

    def test_next_slot_crosses_month_boundary_without_skipping_august_31(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "youtube_schedule_state.json"
            state.write_text(json.dumps({
                "last_scheduled_publish_at": "2026-08-30T20:00:00+03:00",
            }), encoding="utf-8")
            turkey = timezone(timedelta(hours=3))
            scheduled = next_publish_slot(
                now=datetime(2026, 8, 30, 21, 0, tzinfo=turkey),
                state_file=state,
                schedule_config=YouTubeScheduleConfig(
                    publish_hour=20,
                    interval_days=1,
                ),
            )
        self.assertEqual(scheduled.isoformat(), "2026-08-31T20:00:00+03:00")

    def test_schedule_does_not_require_a_saved_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing_schedule_state.json"
            scheduled = next_publish_slot(
                now=datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc),
                state_file=missing,
                schedule_config=YouTubeScheduleConfig(
                    publish_hour=20,
                    interval_days=1,
                ),
            )
        self.assertEqual(scheduled.isoformat(), "2026-08-31T20:00:00+03:00")

    def test_past_saved_schedule_advances_from_current_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "youtube_schedule_state.json"
            state.write_text(json.dumps({
                "last_scheduled_publish_at": "2026-08-01T20:00:00+03:00",
            }), encoding="utf-8")
            turkey = timezone(timedelta(hours=3))
            scheduled = next_publish_slot(
                now=datetime(2026, 8, 2, 11, 0, tzinfo=turkey),
                state_file=state,
                schedule_config=TWO_DAY_SCHEDULE,
            )
        self.assertEqual(scheduled.isoformat(), "2026-08-02T20:00:00+03:00")

    def test_stale_local_reservation_flags_are_irrelevant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "youtube_schedule_state.json"
            state.write_text(json.dumps({
                "last_scheduled_publish_at": "2026-08-04T12:00:00+03:00",
                "ignore_manifest_reservations_once": True,
            }), encoding="utf-8")
            turkey = timezone(timedelta(hours=3))
            scheduled = next_publish_slot(
                now=datetime(2026, 8, 4, 16, 0, tzinfo=turkey),
                state_file=state,
                schedule_config=TWO_DAY_SCHEDULE,
            )
        self.assertEqual(scheduled.isoformat(), "2026-08-04T20:00:00+03:00")

    def test_actual_youtube_occupancy_skips_only_occupied_slots(self) -> None:
        turkey = timezone(timedelta(hours=3))
        scheduled = next_publish_slot(
            now=datetime(2026, 8, 31, 1, 0, tzinfo=turkey),
            schedule_config=YouTubeScheduleConfig(
                publish_hour=20,
                interval_days=1,
            ),
            occupied_publish_times={
                datetime(2026, 9, 2, 20, 0, tzinfo=turkey),
                datetime(2026, 9, 3, 20, 0, tzinfo=turkey),
                datetime(2026, 9, 4, 20, 0, tzinfo=turkey),
            },
        )
        self.assertEqual(scheduled.isoformat(), "2026-08-31T20:00:00+03:00")

    def test_youtube_lookup_ignores_public_videos_with_stale_local_dates(self) -> None:
        youtube = MagicMock()
        channels_request = MagicMock()
        channels_request.execute.return_value = {
            "items": [{
                "contentDetails": {
                    "relatedPlaylists": {"uploads": "uploads-playlist"},
                },
            }],
        }
        playlist_request = MagicMock()
        playlist_request.execute.return_value = {
            "items": [
                {"contentDetails": {"videoId": "published-early"}},
                {"contentDetails": {"videoId": "actually-scheduled"}},
            ],
        }
        videos_request = MagicMock()
        videos_request.execute.return_value = {
            "items": [
                {
                    "id": "published-early",
                    "status": {
                        "privacyStatus": "public",
                        "publishAt": "2026-08-31T17:00:00Z",
                    },
                },
                {
                    "id": "actually-scheduled",
                    "status": {
                        "privacyStatus": "private",
                        "publishAt": "2026-09-02T17:00:00Z",
                    },
                },
            ],
        }
        youtube.channels.return_value.list.return_value = channels_request
        youtube.playlistItems.return_value.list.return_value = playlist_request
        youtube.videos.return_value.list.return_value = videos_request

        occupied = _youtube_scheduled_publish_times(
            youtube,
            {"HttpError": RuntimeError},
            maximum_retries=0,
        )

        self.assertEqual(
            occupied,
            {datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)},
        )

    def test_exact_next_upload_override_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "youtube_schedule_state.json"
            state.write_text(json.dumps({
                "last_scheduled_publish_at": "2026-08-05T20:00:00+03:00",
                "next_scheduled_publish_at_override": "2026-08-04T20:00:00+03:00",
            }), encoding="utf-8")
            turkey = timezone(timedelta(hours=3))
            scheduled = next_publish_slot(
                now=datetime(2026, 8, 4, 17, 0, tzinfo=turkey),
                state_file=state,
            )
        self.assertEqual(scheduled.isoformat(), "2026-08-04T20:00:00+03:00")


if __name__ == "__main__":
    unittest.main()
