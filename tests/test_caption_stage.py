from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alignment_utils import build_alignment_artifact
from caption_stage import (
    CaptionSettings,
    format_srt_timestamp,
    generate_captions,
    normalize_text,
    parse_srt,
    wrap_caption,
)


def settings() -> CaptionSettings:
    return CaptionSettings(
        max_characters_per_line=42,
        max_lines=2,
        max_words_per_caption=9,
        minimum_duration_seconds=0.35,
    )


def proportional_alignment(text: str, duration: float) -> dict:
    unit = duration / len(text)
    return {
        "characters": list(text),
        "character_start_times_seconds": [
            round(index * unit, 6) for index in range(len(text))
        ],
        "character_end_times_seconds": [
            round((index + 1) * unit, 6) for index in range(len(text))
        ],
    }


def write_project(root: Path) -> list[str]:
    texts = [
        (
            "OpenAI announced a compact privacy update. "
            "Users can disable the optional feature."
        ),
        (
            "SpaceX prepared Falcon 9 inside its factory. "
            "The rocket then moved to the launch site."
        ),
    ]
    durations = [4.0, 5.0]
    separators = [(0.0, 2.0), (6.0, 7.5)]
    narrations = [(2.0, 6.0), (7.5, 12.5)]
    stories = []
    for order, (text, duration) in enumerate(
        zip(texts, durations, strict=True),
        start=1,
    ):
        story_id = f"story_{order:012x}"
        text_path = root / f"{order:02d}_story.txt"
        alignment_path = root / f"{order:02d}.alignment.json"
        text_path.write_text(text, encoding="utf-8")
        artifact = build_alignment_artifact(
            story_id=story_id,
            canonical_text=text,
            raw_alignment=proportional_alignment(text, duration),
            method="fixture",
            audio_sha256=str(order) * 64,
            audio_duration_seconds=duration,
        )
        alignment_path.write_text(json.dumps(artifact), encoding="utf-8")
        stories.append(
            {
                "story_id": story_id,
                "order": order,
                "title": f"Story {order}",
                "text_file": str(text_path),
                "alignment_file": str(alignment_path),
                "separator": {
                    "start": separators[order - 1][0],
                    "end": separators[order - 1][1],
                    "duration": (
                        separators[order - 1][1]
                        - separators[order - 1][0]
                    ),
                },
                "narration": {
                    "start": narrations[order - 1][0],
                    "end": narrations[order - 1][1],
                    "duration": duration,
                },
            }
        )
    (root / "state.json").write_text(
        json.dumps(
            {"stages": {"manual_separators": {"status": "READY"}}}
        ),
        encoding="utf-8",
    )
    (root / "story_timing.json").write_text(
        json.dumps(
            {
                "video_number": 5,
                "duration_seconds": 12.5,
                "manual_separators_validated_at_utc": "2026-07-30T00:00:00Z",
                "quality": {
                    "status": "passed",
                    "manual_separators_validated": True,
                },
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return texts


class CaptionStageTests(unittest.TestCase):
    def test_srt_timestamp_rounding_and_wrap(self) -> None:
        self.assertEqual(format_srt_timestamp(3661.2346), "01:01:01,235")
        wrapped = wrap_caption(
            "This caption should balance cleanly across two readable lines.",
            settings(),
        )
        self.assertEqual(len(wrapped.splitlines()), 2)
        self.assertTrue(all(len(line) <= 42 for line in wrapped.splitlines()))

    def test_generate_combined_and_per_story_srt_with_timeline_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            texts = write_project(root)
            with patch("caption_stage.project_dir", return_value=root):
                manifest = generate_captions(
                    video_number=5,
                    settings=settings(),
                )

            self.assertEqual(manifest["quality"]["status"], "passed")
            self.assertTrue(manifest["quality"]["canonical_text_matches"])
            self.assertEqual(len(manifest["stories"]), 2)
            cues = parse_srt(
                (root / "06_captions" / "captions.srt").read_text(
                    encoding="utf-8"
                )
            )
            self.assertGreaterEqual(cues[0]["start"], 2.0)
            self.assertLessEqual(cues[-1]["end"], 12.5)
            self.assertFalse(
                any(
                    cue["start"] < 7.5 and cue["end"] > 6.0
                    for cue in cues
                )
            )
            self.assertEqual(
                normalize_text(" ".join(cue["plain_text"] for cue in cues)),
                normalize_text(" ".join(texts)),
            )
            self.assertTrue((root / "06_captions" / "story_01.srt").is_file())
            self.assertTrue((root / "06_captions" / "story_02.srt").is_file())
            state = json.loads(
                (root / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["stages"]["captions"]["status"],
                "CAPTIONS_READY",
            )

    def test_valid_caption_package_is_cached_without_rewriting_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            with patch("caption_stage.project_dir", return_value=root):
                first = generate_captions(
                    video_number=5,
                    settings=settings(),
                )
                srt_path = root / "06_captions" / "captions.srt"
                first_mtime = srt_path.stat().st_mtime_ns
                second = generate_captions(
                    video_number=5,
                    settings=settings(),
                )

            self.assertEqual(first["run_status"], "generated")
            self.assertEqual(second["run_status"], "cached")
            self.assertEqual(srt_path.stat().st_mtime_ns, first_mtime)

    def test_changed_final_separator_timing_invalidates_caption_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            with patch("caption_stage.project_dir", return_value=root):
                first = generate_captions(
                    video_number=5,
                    settings=settings(),
                )
                timing_path = root / "story_timing.json"
                timing = json.loads(timing_path.read_text(encoding="utf-8"))
                timing["manual_separators_validated_at_utc"] = (
                    "2026-07-30T00:01:00Z"
                )
                timing["stories"][0]["separator"]["end"] = 2.5
                timing["stories"][0]["separator"]["duration"] = 2.5
                timing["stories"][0]["narration"]["start"] = 2.5
                timing["stories"][0]["narration"]["end"] = 6.5
                timing["stories"][1]["separator"]["start"] = 6.5
                timing["stories"][1]["separator"]["end"] = 8.0
                timing["stories"][1]["narration"]["start"] = 8.0
                timing["stories"][1]["narration"]["end"] = 13.0
                timing["duration_seconds"] = 13.0
                timing_path.write_text(json.dumps(timing), encoding="utf-8")
                second = generate_captions(
                    video_number=5,
                    settings=settings(),
                )

            self.assertNotEqual(
                first["input_fingerprint"],
                second["input_fingerprint"],
            )
            self.assertEqual(second["run_status"], "generated")
            cues = parse_srt(
                (root / "06_captions" / "captions.srt").read_text(
                    encoding="utf-8"
                )
            )
            self.assertGreaterEqual(cues[0]["start"], 2.5)

    def test_stale_alignment_text_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            (root / "01_story.txt").write_text(
                "This narration changed after alignment.",
                encoding="utf-8",
            )
            with patch("caption_stage.project_dir", return_value=root):
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    generate_captions(
                        video_number=5,
                        settings=settings(),
                    )


if __name__ == "__main__":
    unittest.main()
