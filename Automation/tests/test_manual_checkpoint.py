from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manual_checkpoint import (
    SeparatorSpec,
    accepted_separator_paths,
    cancel_checkpoint,
    prepare_checkpoint,
    open_checkpoint_folders,
    prompt_checkpoint,
    skip_checkpoint,
    validate_checkpoint,
    validate_separator_file,
    validate_separator_set,
    write_combined_original_news,
)
from tooling import find_executable


def test_spec(audio_policy: str = "consistent") -> SeparatorSpec:
    return SeparatorSpec(
        width=320,
        height=180,
        fps=30.0,
        fps_tolerance=0.05,
        allowed_codecs=("h264",),
        allowed_pixel_formats=("yuv420p",),
        audio_policy=audio_policy,
        minimum_duration_seconds=0.1,
    )


def write_timing_project(root: Path, story_count: int) -> None:
    stories = []
    cursor = 0.0
    for index in range(1, story_count + 1):
        audio = root / f"voice_{index}.mp3"
        alignment = root / f"voice_{index}.alignment.json"
        text = root / f"story_{index}.txt"
        original = root / f"story_{index}-orijinal.txt"
        audio.write_bytes(f"audio-{index}".encode())
        text.write_text(f"Summary {index}", encoding="utf-8")
        original.write_text(f"Original news {index}.", encoding="utf-8")
        alignment.write_text(
            json.dumps({"story": index}),
            encoding="utf-8",
        )
        stories.append(
            {
                "order": index,
                "story_id": f"story_{index:012x}",
                "title": f"Story {index}",
                "text_file": str(text),
                "audio_file": str(audio),
                "alignment_file": str(alignment),
                "separator": {
                    "start": cursor,
                    "end": cursor + 3,
                    "duration": 3,
                    "expected_filename": f"{index:02d}_separator.mp4",
                },
                "narration": {
                    "start": cursor + 3,
                    "end": cursor + 5,
                    "duration": 2,
                },
                "sentences": [
                    {"index": 0, "text": "Sentence.", "start": 0.0, "end": 2.0}
                ],
                "phrases": [
                    {"index": 0, "text": "Sentence.", "start": 0.0, "end": 2.0}
                ],
                "visual_beats": [
                    {
                        "index": 0,
                        "important_claim": "Sentence.",
                        "start": 0.0,
                        "end": 2.0,
                    }
                ],
                "important_claims": [
                    {
                        "text": "Sentence.",
                        "importance": "must_show",
                        "start": 0.0,
                        "end": 2.0,
                    }
                ],
            }
        )
        cursor += 5
    (root / "story_timing.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "video_number": 5,
                "required_separator_count": story_count,
                "duration_seconds": cursor,
                "quality": {"status": "passed"},
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )


def make_separator(
    path: Path,
    *,
    width: int = 320,
    height: int = 180,
    fps: int = 30,
    audio: bool = False,
) -> None:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("FFmpeg is unavailable")
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s={width}x{height}:r={fps}:d=0.3",
    ]
    if audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.3",
                "-shortest",
            ]
        )
    command.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ]
    )
    subprocess.run(command, check=True)


class ManualCheckpointTests(unittest.TestCase):
    def test_skip_starts_narration_and_images_at_each_chapter_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 2)
            with patch("manual_checkpoint.project_dir", return_value=root):
                stage = skip_checkpoint(5, test_spec())

            timing = json.loads(
                (root / "story_timing.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stage["status"], "SKIPPED")
            self.assertTrue(timing["quality"]["manual_separators_skipped"])
            self.assertTrue(timing["quality"]["images_start_at_chapter_start"])
            self.assertEqual(timing["required_separator_count"], 0)
            self.assertEqual(timing["stories"][0]["narration"]["start"], 0.0)
            self.assertEqual(timing["stories"][0]["separator"]["duration"], 0.0)
            self.assertEqual(timing["stories"][1]["narration"]["start"], 4.0)
            self.assertEqual(
                timing["stories"][1]["sentences"][0]["timeline_start"],
                4.0,
            )

    def test_combined_news_file_uses_requested_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 3)
            timing = json.loads(
                (root / "story_timing.json").read_text(encoding="utf-8")
            )
            (root / "03_manual_separators").mkdir()

            result = write_combined_original_news(root, timing)

            self.assertEqual(result.name, "ORIGINAL_NEWS.txt")
            self.assertEqual(
                result.read_text(encoding="utf-8"),
                "Story 1\n\nOriginal news 1.\n\n"
                "---------------\n\n"
                "Story 2\n\nOriginal news 2.\n\n"
                "---------------\n\n"
                "Story 3\n\nOriginal news 3.\n",
            )

    def test_checkpoint_opens_combined_news_then_separator_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            news = root / "ORIGINAL_NEWS.txt"
            separators = root / "03_manual_separators"
            news.write_text("News", encoding="utf-8")
            separators.mkdir()
            opened = []

            result = open_checkpoint_folders(
                {
                    "combined_news_file": str(news),
                    "separator_directory": str(separators),
                },
                opener=opened.append,
            )

            self.assertEqual(result, [news, separators])
            self.assertEqual(opened, [news, separators])

    def test_prepare_creates_folder_state_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 5)
            with patch("manual_checkpoint.project_dir", return_value=root):
                _, _, first, _ = prepare_checkpoint(5, test_spec())
                _, _, second, _ = prepare_checkpoint(5, test_spec())

            separator_dir = root / "03_manual_separators"
            self.assertTrue(separator_dir.is_dir())
            combined_news = separator_dir / "ORIGINAL_NEWS.txt"
            self.assertTrue(combined_news.is_file())
            self.assertEqual(first["combined_news_file"], str(combined_news))
            self.assertEqual(first["status"], "WAITING")
            self.assertEqual(first["created_at_utc"], second["created_at_utc"])
            self.assertEqual(
                [Path(value).name for value in first["expected_files"]],
                [
                    "01.mp4",
                    "02.mp4",
                    "03.mp4",
                    "04.mp4",
                    "05.mp4",
                ],
            )
            self.assertEqual(
                first["accepted_filenames"][0],
                ["1.mp4", "01.mp4", "01_separator.mp4"],
            )
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state["stages"]["manual_separators"]["status"],
                "WAITING",
            )

    def test_missing_and_corrupt_are_rejected_but_resolution_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.mp4"
            corrupt = root / "corrupt.mp4"
            wrong = root / "wrong.mp4"
            corrupt.write_bytes(b"not a video")
            make_separator(wrong, width=640, height=360)

            self.assertIn(
                "missing",
                validate_separator_file(missing, test_spec())["errors"][0].lower(),
            )
            self.assertIn(
                "FFprobe rejected",
                validate_separator_file(corrupt, test_spec())["errors"][0],
            )
            wrong_result = validate_separator_file(wrong, test_spec())
            self.assertEqual(wrong_result["status"], "passed")
            self.assertTrue(wrong_result["normalization_required"])
            self.assertTrue(
                any("scaled and padded" in warning for warning in wrong_result["warnings"])
            )

    def test_audio_presence_must_be_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silent = root / "01_separator.mp4"
            audible = root / "02_separator.mp4"
            make_separator(silent, audio=False)
            make_separator(audible, audio=True)

            result = validate_separator_set(
                [silent, audible],
                test_spec("consistent"),
            )

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("inconsistent" in error for error in result["global_errors"])
            )

    def test_valid_files_mark_ready_and_apply_actual_durations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 2)
            separator_dir = root / "03_manual_separators"
            separator_dir.mkdir()
            make_separator(separator_dir / "1.mp4")
            make_separator(separator_dir / "02.mp4")

            with patch("manual_checkpoint.project_dir", return_value=root):
                success, stage = validate_checkpoint(5, test_spec())

            self.assertTrue(success)
            self.assertEqual(stage["status"], "READY")
            timing = json.loads(
                (root / "story_timing.json").read_text(encoding="utf-8")
            )
            first = timing["stories"][0]
            second = timing["stories"][1]
            self.assertTrue(timing["quality"]["manual_separators_validated"])
            self.assertEqual(
                first["separator"]["start"],
                first["narration"]["start"],
            )
            self.assertEqual(
                first["narration"]["end"] + 2.0,
                second["separator"]["start"],
            )
            self.assertEqual(
                second["separator"]["start"],
                second["narration"]["start"],
            )
            self.assertTrue(timing["quality"]["separator_overlays_narration"])
            self.assertTrue(timing["quality"]["inter_story_gaps_are_exact"])
            self.assertEqual(Path(first["separator"]["file"]).name, "1.mp4")
            self.assertEqual(Path(second["separator"]["file"]).name, "02.mp4")

    def test_accepted_separator_names_keep_legacy_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            candidates = accepted_separator_paths(root, 2)

            self.assertEqual(
                [path.name for path in candidates[0]],
                ["1.mp4", "01.mp4", "01_separator.mp4"],
            )
            self.assertEqual(
                [path.name for path in candidates[1]],
                ["2.mp4", "02.mp4", "02_separator.mp4"],
            )

    def test_changed_ready_file_returns_checkpoint_to_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 1)
            separator_dir = root / "03_manual_separators"
            separator_dir.mkdir()
            separator = separator_dir / "01_separator.mp4"
            make_separator(separator)

            with patch("manual_checkpoint.project_dir", return_value=root):
                success, _ = validate_checkpoint(5, test_spec())
                self.assertTrue(success)
                separator.write_bytes(separator.read_bytes() + b"changed")
                _, _, stage, _ = prepare_checkpoint(5, test_spec())

            self.assertEqual(stage["status"], "WAITING")
            self.assertIn("changed", stage["reason"])

    def test_ready_checkpoint_reapplies_durations_after_timing_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 1)
            placeholder = (root / "story_timing.json").read_text(encoding="utf-8")
            separator_dir = root / "03_manual_separators"
            separator_dir.mkdir()
            make_separator(separator_dir / "01_separator.mp4")

            with patch("manual_checkpoint.project_dir", return_value=root):
                success, _ = validate_checkpoint(5, test_spec())
                self.assertTrue(success)
                (root / "story_timing.json").write_text(
                    placeholder,
                    encoding="utf-8",
                )
                _, _, stage, timing = prepare_checkpoint(5, test_spec())

            self.assertEqual(stage["status"], "READY")
            self.assertTrue(timing["quality"]["manual_separators_validated"])

    def test_cancel_preserves_all_files_and_can_be_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 1)
            marker = root / "keep-me.txt"
            marker.write_text("preserve", encoding="utf-8")
            with patch("manual_checkpoint.project_dir", return_value=root):
                cancelled = cancel_checkpoint(5, test_spec())
                _, _, resumed, _ = prepare_checkpoint(5, test_spec())

            self.assertEqual(cancelled["status"], "CANCELLED")
            self.assertEqual(resumed["status"], "CANCELLED")
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_closed_console_leaves_checkpoint_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 1)
            with (
                patch("manual_checkpoint.project_dir", return_value=root),
                patch("builtins.input", side_effect=EOFError),
            ):
                result = prompt_checkpoint(5, test_spec())

            self.assertEqual(result, "WAITING")
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state["stages"]["manual_separators"]["status"],
                "WAITING",
            )

    def test_cancelled_checkpoint_resumes_directly_with_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_timing_project(root, 1)
            separator_dir = root / "03_manual_separators"
            separator_dir.mkdir()
            make_separator(separator_dir / "01_separator.mp4")
            with patch("manual_checkpoint.project_dir", return_value=root):
                cancel_checkpoint(5, test_spec())
                with patch("builtins.input", return_value="1"):
                    result = prompt_checkpoint(5, test_spec())

            self.assertEqual(result, "READY")
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state["stages"]["manual_separators"]["status"],
                "READY",
            )


if __name__ == "__main__":
    unittest.main()
