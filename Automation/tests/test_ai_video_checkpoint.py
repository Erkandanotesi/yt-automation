from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_video_checkpoint import prepare, skip, status, validate
from file_utils import sha256_file


def write_storyboard(root: Path, count: int = 5) -> None:
    directory = root / "04_storyboard"
    directory.mkdir(parents=True)
    stories = []
    for order in range(1, count + 1):
        stories.append(
            {
                "story_id": f"story_{order}",
                "order": order,
                "title": f"Story {order}",
                "images": [
                    {
                        "image_index": 1,
                        "role": "timed_story_hero",
                        "time_range": {"start": 0.0, "end": 6.0 + order},
                        "ai_video_prompt": (
                            f"A factual cinematic scene for story {order}. The camera "
                            "moves slowly as the supported action develops in natural light."
                        ),
                    }
                ],
            }
        )
    (directory / "storyboard.json").write_text(
        json.dumps({"quality": {"status": "passed"}, "stories": stories}),
        encoding="utf-8",
    )


class AiVideoCheckpointTests(unittest.TestCase):
    def test_prepare_auto_skips_when_ai_images_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 8"
            storyboard_dir = root / "04_storyboard"
            storyboard_dir.mkdir(parents=True)
            (storyboard_dir / "storyboard.json").write_text(
                json.dumps(
                    {
                        "configuration": {"images_per_story": 0},
                        "quality": {
                            "status": "passed",
                            "ai_image_generation_disabled": True,
                        },
                        "stories": [
                            {"story_id": "story_1", "order": 1, "images": []}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("ai_video_checkpoint.project_dir", return_value=root):
                manifest = prepare(8, open_folder=False)
                current = status(8)

            self.assertEqual(manifest["status"], "SKIPPED")
            self.assertEqual(manifest["decision"], "use_stock_media")
            self.assertEqual(current["status"], "SKIPPED")

    def test_prepare_writes_five_numbered_english_prompts_and_simple_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 8"
            write_storyboard(root)
            with patch("ai_video_checkpoint.project_dir", return_value=root):
                manifest = prepare(8, open_folder=False)

            combined = Path(manifest["combined_prompt_file"]).read_text(encoding="utf-8")
            self.assertEqual([item["expected_stem"] for item in manifest["entries"]], ["1", "2", "3", "4", "5"])
            self.assertIn("1- Story 1", combined)
            self.assertIn("Target interval: 0.000s to 7.000s (7.000 seconds)", combined)
            self.assertIn("Save as: 1.mp4", combined)
            self.assertIn("Text-to-video depiction (English):", combined)
            self.assertNotIn("at least 1280x720", combined)
            self.assertNotIn("01_ai_story_video", combined)

    def test_validation_accepts_numbered_files_and_tracks_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 8"
            write_storyboard(root)
            with patch("ai_video_checkpoint.project_dir", return_value=root):
                prepare(8, open_folder=False)
                clip_dir = root / "04_ai_story_videos"
                for order in range(1, 6):
                    (clip_dir / f"{order}.mp4").write_bytes(f"clip-{order}".encode())
                with (
                    patch(
                        "ai_video_checkpoint._probe",
                        return_value={
                            "format": {"duration": "20.0"},
                            "streams": [
                                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}
                            ],
                        },
                    ),
                    patch("ai_video_checkpoint._decode_check"),
                ):
                    manifest = validate(8)
                current = status(8)

            self.assertEqual(manifest["status"], "READY")
            self.assertEqual(current["status"], "READY")
            self.assertEqual(Path(manifest["entries"][0]["file"]).name, "1.mp4")
            self.assertEqual(manifest["entries"][0]["sha256"], sha256_file(clip_dir / "1.mp4"))

    def test_too_short_clip_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 8"
            write_storyboard(root, count=1)
            with patch("ai_video_checkpoint.project_dir", return_value=root):
                prepare(8, open_folder=False)
                (root / "04_ai_story_videos" / "1.mp4").write_bytes(b"short")
                with (
                    patch(
                        "ai_video_checkpoint._probe",
                        return_value={
                            "format": {"duration": "2.0"},
                            "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
                        },
                    ),
                    self.assertRaisesRegex(RuntimeError, "must cover at least"),
                ):
                    validate(8)

    def test_skip_is_idempotent_and_reports_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 8"
            root.mkdir()
            with patch("ai_video_checkpoint.project_dir", return_value=root):
                first = skip(8)
                manifest_path = root / "04_ai_story_videos" / "ai_video_manifest.json"
                first_sha = sha256_file(manifest_path)
                skip(8)
                current = status(8)
            self.assertEqual(current["status"], "SKIPPED")
            self.assertEqual(first_sha, sha256_file(manifest_path))
            self.assertEqual(first, json.loads(manifest_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
