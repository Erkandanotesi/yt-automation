from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from storyboard_stage import (
    SelectedBeat,
    StoryBeatSelection,
    StoryboardSettings,
    archive_approved_images,
    build_storyboard,
    candidate_beats,
    generate_storyboard_images,
    render_storyboard_proof,
    validate_generated_image,
)
from tooling import find_executable


def settings() -> StoryboardSettings:
    return StoryboardSettings(
        planner_model="gpt-5.6-luna",
        planner_reasoning_effort="low",
        image_model="gpt-image-2",
        image_quality="medium",
        image_size="1280x720",
        output_format="png",
        timeout_seconds=30,
        planner_retries=1,
        maximum_source_characters=4000,
        maximum_image_requests_per_run=10,
        duplicate_hamming_threshold=6,
        archive_directory=None,
    )


def make_png_bytes(color: str) -> bytes:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("FFmpeg is unavailable")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fixture.png"
        source = (
            "testsrc2=size=1280x720:rate=1"
            if color == "blue"
            else "smptebars=size=1280x720:rate=1"
            if color == "green"
            else f"color=c={color}:s=1280x720"
        )
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                source,
                "-frames:v",
                "1",
                "-y",
                str(path),
            ],
            check=True,
        )
        return path.read_bytes()


def make_audio(path: Path, duration: float = 35.0) -> None:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("FFmpeg is unavailable")
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-y",
            str(path),
        ],
        check=True,
    )


def make_separator(path: Path, duration: float = 0.5) -> None:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("FFmpeg is unavailable")
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=320x180:r=30:d={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
    )


def write_project(root: Path, story_count: int = 1) -> None:
    stories = []
    timed = []
    cursor = 0.0
    separator_dir = root / "03_manual_separators"
    separator_dir.mkdir(parents=True)
    for order in range(1, story_count + 1):
        story_id = f"story_{order:012x}"
        narration = root / f"{order:02d}_story.txt"
        source = root / f"{order:02d}_story-original.txt"
        audio = root / f"{order:02d}_story.mp3"
        alignment = root / f"{order:02d}_story.alignment.json"
        separator = separator_dir / f"{order:02d}_separator.mp4"
        narration.write_text(
            "A company introduced a new processor. "
            "The chip cuts power use in data centers.",
            encoding="utf-8",
        )
        source.write_text(
            "The company announced a processor for data centers. "
            "The source says it reduces energy use under tested server workloads.",
            encoding="utf-8",
        )
        make_audio(audio)
        alignment.write_text("{}", encoding="utf-8")
        make_separator(separator)
        stories.append(
            {
                "story_id": story_id,
                "order": order,
                "title": f"Processor story {order}",
                "source_title": "Original processor announcement",
                "source_url": f"https://example.com/{order}",
                "score": 90 - order,
                "ranking_reason": "Concrete product and consequence.",
                "text": str(narration),
                "original": str(source),
            }
        )
        separator_duration = 0.5
        narration_start = cursor + separator_duration
        timed.append(
            {
                "story_id": story_id,
                "order": order,
                "title": f"Processor story {order}",
                "audio_file": str(audio),
                "alignment_file": str(alignment),
                "separator": {
                    "file": str(separator),
                    "start": cursor,
                    "end": narration_start,
                    "duration": separator_duration,
                },
                "narration": {
                    "start": narration_start,
                    "end": narration_start + 35,
                    "duration": 35,
                },
                "visual_beats": [
                    {
                        "index": 0,
                        "important_claim": "A new processor was introduced.",
                        "start": 1.0,
                        "end": 8.0,
                        "importance": "must_show",
                    },
                    {
                        "index": 1,
                        "important_claim": "The chip cuts data-center power use.",
                        "start": 18.0,
                        "end": 27.0,
                        "importance": "must_show",
                    },
                ],
                "sentences": [],
            }
        )
        cursor = narration_start + 35
    (root / "project_info.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "expected_count": story_count,
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    (root / "story_timing.json").write_text(
        json.dumps(
            {
                "quality": {
                    "status": "passed",
                    "manual_separators_validated": True,
                },
                "stories": timed,
            }
        ),
        encoding="utf-8",
    )
    (root / "state.json").write_text(
        json.dumps(
            {
                "stages": {
                    "manual_separators": {
                        "status": "READY",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def planner(context: dict, _: StoryboardSettings) -> StoryBeatSelection:
    return StoryBeatSelection(
        images=[
            SelectedBeat(
                candidate_id="beat_01",
                visual_concept=(
                    "A newly introduced generic processor presented in a lab."
                ),
                image_prompt=(
                    "An unbranded processor package on a clean engineering bench, "
                    "with a server lab softly visible in the background."
                ),
                selection_reason=(
                    "This establishes the concrete product announcement."
                ),
            ),
            SelectedBeat(
                candidate_id="beat_02",
                visual_concept=(
                    "A data center operating with visibly restrained energy demand."
                ),
                image_prompt=(
                    "A wide modern data-center aisle with efficient cooling airflow "
                    "and a restrained, calm operational atmosphere."
                ),
                selection_reason=(
                    "This shows the separate consequence of lower energy use."
                ),
            ),
        ]
    )


class StoryboardStageTests(unittest.TestCase):
    def test_manual_separator_hidden_beats_are_not_ai_candidates(self) -> None:
        context = {
            "story_id": "story_1",
            "timing": {
                "narration": {"start": 0},
                "separator": {"duration": 5, "skipped": False},
                "visual_beats": [
                    {"important_claim": "hidden", "start": 0, "end": 3},
                    {"important_claim": "visible one", "start": 6, "end": 9},
                    {"important_claim": "visible two", "start": 12, "end": 15},
                ],
                "sentences": [],
            },
        }
        candidates = candidate_beats(context)
        self.assertEqual(
            [item["beat_text"] for item in candidates],
            ["visible one", "visible two"],
        )

    def test_skipped_separator_keeps_chapter_start_ai_candidates(self) -> None:
        context = {
            "story_id": "story_1",
            "timing": {
                "narration": {"start": 0},
                "separator": {"duration": 0, "skipped": True},
                "visual_beats": [
                    {"important_claim": "opening", "start": 0, "end": 3},
                    {"important_claim": "later", "start": 7, "end": 10},
                ],
                "sentences": [],
            },
        }
        candidates = candidate_beats(context)
        self.assertEqual(candidates[0]["beat_text"], "opening")

    def test_boundary_crossing_candidate_is_clamped_after_separator(self) -> None:
        context = {
            "story_id": "story_1",
            "timing": {
                "narration": {"start": 10},
                "separator": {"duration": 5, "skipped": False},
                "visual_beats": [
                    {"important_claim": "crossing", "start": 2, "end": 8},
                    {"important_claim": "visible", "start": 9, "end": 12},
                ],
                "sentences": [],
            },
        }
        candidates = candidate_beats(context)
        self.assertEqual(candidates[0]["start"], 5.0)
        self.assertEqual(candidates[0]["timeline_start"], 15.0)

    def test_long_visual_beat_splits_at_clause_punctuation_with_sentence_context(self) -> None:
        full_sentence = (
            "Microsoft confirmed the report, rejected the allegations, and promised "
            "a detailed response after its internal review."
        )
        context = {
            "story_id": "story_1",
            "timing": {
                "narration": {"start": 10},
                "separator": {"duration": 0, "skipped": True},
                "visual_beats": [
                    {"important_claim": full_sentence, "start": 0, "end": 18},
                    {"important_claim": "A second beat.", "start": 18, "end": 21},
                ],
                "phrases": [
                    {"text": "Microsoft confirmed the report,", "start": 0, "end": 5},
                    {"text": "rejected the allegations,", "start": 5, "end": 10},
                    {"text": "and promised a detailed response", "start": 10, "end": 14},
                    {"text": "after its internal review.", "start": 14, "end": 18},
                    {"text": "A second beat.", "start": 18, "end": 21},
                ],
                "sentences": [
                    {"text": full_sentence, "start": 0, "end": 18},
                    {"text": "A second beat.", "start": 18, "end": 21},
                ],
            },
        }

        candidates = candidate_beats(context, maximum_duration_seconds=7)
        long_beat_candidates = [item for item in candidates if item["start"] < 18]

        self.assertEqual(
            [item["timestamp_excerpt"] for item in long_beat_candidates],
            [
                "Microsoft confirmed the report,",
                "rejected the allegations,",
                "and promised a detailed response",
                "after its internal review.",
            ],
        )
        self.assertTrue(
            all(item["end"] - item["start"] <= 7 for item in long_beat_candidates)
        )
        self.assertTrue(
            all(item["beat_text"] == full_sentence for item in long_beat_candidates)
        )

    def test_approved_image_is_archived_with_descriptive_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "01_hero_01.png"
            source.write_bytes(b"approved-image")
            storyboard = {
                "stories": [
                    {
                        "order": 1,
                        "images": [
                            {
                                "image_index": 1,
                                "visual_concept": "Person opening a malicious email in a secure mailbox",
                                "output_file": str(source),
                                "validation": {"status": "passed"},
                            }
                        ],
                    }
                ]
            }
            archived = archive_approved_images(
                storyboard,
                replace(settings(), archive_directory=root / "archive"),
            )
            destination = Path(archived[0]["file"])
            self.assertTrue(destination.is_file())
            self.assertIn("person-opening-a-malicious-email", destination.name)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    @classmethod
    def setUpClass(cls) -> None:
        cls.red = make_png_bytes("red")
        cls.blue = make_png_bytes("blue")
        cls.green = make_png_bytes("green")

    def test_storyboard_is_saved_with_two_exact_timed_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            configured = replace(settings(), archive_directory=root / "archive")
            with patch("storyboard_stage.project_dir", return_value=root):
                result = build_storyboard(
                    video_number=5,
                    settings=configured,
                    planner=planner,
                )

            path = root / "04_storyboard" / "storyboard.json"
            self.assertTrue(path.is_file())
            self.assertEqual(result["planned_image_count"], 2)
            images = result["stories"][0]["images"]
            self.assertEqual(len(images), 2)
            self.assertEqual(images[0]["time_range"]["start"], 1.0)
            self.assertEqual(images[1]["time_range"]["end"], 25.0)
            self.assertTrue(
                all(
                    image["time_range"]["end"] - image["time_range"]["start"]
                    <= configured.maximum_image_seconds
                    for image in images
                )
            )
            self.assertIn("bottom 22%", images[0]["prompt"])
            self.assertIn("No readable text", images[0]["prompt"])
            self.assertEqual(images[0]["generation"]["status"], "pending")
            self.assertEqual(
                result["configuration"]["archive_directory"],
                str(root / "archive"),
            )

    def test_configured_ai_image_count_controls_storyboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            configured = replace(
                settings(),
                images_per_story=1,
                archive_directory=root / "archive",
            )

            def one_image_planner(
                context: dict,
                planner_settings: StoryboardSettings,
            ) -> StoryBeatSelection:
                planned = planner(context, planner_settings)
                return StoryBeatSelection(images=planned.images[:1])

            with patch("storyboard_stage.project_dir", return_value=root):
                result = build_storyboard(
                    video_number=5,
                    settings=configured,
                    planner=one_image_planner,
                )

        self.assertEqual(result["planned_image_count"], 1)
        self.assertEqual(result["quality"]["images_per_story"], 1)
        self.assertEqual(len(result["stories"][0]["images"]), 1)

    def test_generation_requires_explicit_cost_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            with patch("storyboard_stage.project_dir", return_value=root):
                build_storyboard(
                    video_number=5,
                    settings=settings(),
                    planner=planner,
                )
                with self.assertRaisesRegex(RuntimeError, "confirm-cost"):
                    generate_storyboard_images(
                        video_number=5,
                        confirm_cost=False,
                        settings=settings(),
                        generator=lambda *_: (_ for _ in ()).throw(
                            AssertionError("provider must not run")
                        ),
                    )

    def test_mock_generation_validates_and_then_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            calls = []

            def generator(_: str, __: StoryboardSettings) -> tuple[bytes, dict]:
                calls.append(1)
                payload = self.red if len(calls) == 1 else self.blue
                return payload, {"request_id": f"mock-{len(calls)}"}

            with patch("storyboard_stage.project_dir", return_value=root):
                build_storyboard(
                    video_number=5,
                    settings=settings(),
                    planner=planner,
                )
                first = generate_storyboard_images(
                    video_number=5,
                    confirm_cost=True,
                    settings=settings(),
                    generator=generator,
                )
                second = generate_storyboard_images(
                    video_number=5,
                    confirm_cost=True,
                    settings=settings(),
                    generator=lambda *_: (_ for _ in ()).throw(
                        AssertionError("cached images must not call provider")
                    ),
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual(first["generated_image_count"], 2)
            self.assertTrue(first["quality"]["all_images_generated_and_validated"])
            self.assertEqual(
                [image["generation"]["status"] for image in second["stories"][0]["images"]],
                ["cached", "cached"],
            )

    def test_perceptual_duplicate_is_rejected_without_auto_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            calls = []

            def generator(_: str, __: StoryboardSettings) -> tuple[bytes, dict]:
                calls.append(1)
                return self.red, {"request_id": f"mock-{len(calls)}"}

            with patch("storyboard_stage.project_dir", return_value=root):
                build_storyboard(
                    video_number=5,
                    settings=settings(),
                    planner=planner,
                )
                with self.assertRaisesRegex(RuntimeError, "duplicate"):
                    generate_storyboard_images(
                        video_number=5,
                        confirm_cost=True,
                        settings=settings(),
                        generator=generator,
                    )

            self.assertEqual(len(calls), 2)

    def test_renders_thirty_second_separator_voice_and_two_hero_proof(self) -> None:
        ffmpeg = find_executable("ffmpeg")
        if ffmpeg is None:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            filler = root / "archive_filler.png"
            filler.write_bytes(self.green)
            calls = []

            def generator(_: str, __: StoryboardSettings) -> tuple[bytes, dict]:
                calls.append(1)
                return (
                    self.red if len(calls) == 1 else self.blue,
                    {"request_id": f"mock-{len(calls)}"},
                )

            with patch("storyboard_stage.project_dir", return_value=root):
                build_storyboard(
                    video_number=5,
                    settings=settings(),
                    planner=planner,
                )
                generate_storyboard_images(
                    video_number=5,
                    confirm_cost=True,
                    settings=settings(),
                    generator=generator,
                )
                proof = render_storyboard_proof(
                    video_number=5,
                    story_number=1,
                    settings=settings(),
                    duration_seconds=30,
                    width=320,
                    height=180,
                    archive_filler=filler,
                )

            self.assertTrue(proof.is_file())
            metadata = validate_generated_image(
                Path(root / "05_visuals" / "generated" / "01_hero_01.png"),
                expected_width=1280,
                expected_height=720,
            )
            self.assertEqual(metadata["status"], "passed")
            stored = json.loads(
                (root / "04_storyboard" / "storyboard.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(stored["proof"]["status"], "passed")
            self.assertAlmostEqual(stored["proof"]["duration_seconds"], 30, delta=0.35)
            self.assertEqual(stored["proof"]["temporary_archive_filler"], str(filler))


if __name__ == "__main__":
    unittest.main()
