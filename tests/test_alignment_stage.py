from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alignment_stage import (
    build_story_alignment,
    build_story_timing_manifest,
    render_diagnostic,
)
from alignment_utils import build_alignment_artifact
from file_utils import sha256_file
from tooling import find_executable

from tests.test_alignment_utils import proportional_alignment


class AlignmentStageTests(unittest.TestCase):
    def test_story_timeline_reserves_separators_without_overlap(self) -> None:
        records = []
        for index, duration in enumerate((2.0, 3.0), start=1):
            text = f"Story {index} has one clear sentence."
            artifact = build_alignment_artifact(
                story_id=f"story_{index:012x}",
                canonical_text=text,
                raw_alignment=proportional_alignment(text, duration),
                method="test",
                audio_sha256=str(index) * 64,
                audio_duration_seconds=duration,
            )
            records.append(
                {
                    "story_id": f"story_{index:012x}",
                    "title": f"Story {index}",
                    "text_path": Path(f"{index:02d}.txt"),
                    "audio_path": Path(f"{index:02d}.mp3"),
                    "alignment_path": Path(f"{index:02d}.alignment.json"),
                    "alignment": artifact,
                }
            )

        timing = build_story_timing_manifest(
            video_number=5,
            records=records,
            separator_duration_seconds=3.0,
        )

        self.assertEqual(timing["required_separator_count"], 2)
        self.assertEqual(timing["stories"][0]["separator"]["start"], 0.0)
        self.assertEqual(timing["stories"][0]["narration"]["start"], 3.0)
        self.assertEqual(timing["stories"][1]["separator"]["start"], 5.0)
        self.assertEqual(timing["stories"][1]["narration"]["start"], 8.0)
        self.assertTrue(
            timing["quality"]["no_story_or_separator_overlaps"]
        )

    def test_forced_alignment_response_is_cached_atomically(self) -> None:
        ffmpeg = find_executable("ffmpeg")
        if ffmpeg is None:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "voice.mp3"
            subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:a",
                    "libmp3lame",
                    "-y",
                    str(audio),
                ],
                check=True,
            )
            text = "A short timing test."
            sidecar = root / "voice.alignment.json"
            raw = proportional_alignment(text, 1.0)
            with patch(
                "alignment_stage.forced_alignment_request",
                return_value=(
                    raw,
                    {
                        "api_key_slot": "mock",
                        "request_id": "request_mock",
                        "character_cost": "",
                        "retry_history": [],
                    },
                ),
            ) as provider:
                first, metadata = build_story_alignment(
                    story_id="story_123456789abc",
                    text=text,
                    audio_path=audio,
                    alignment_path=sidecar,
                    force=False,
                )
                second, cached_metadata = build_story_alignment(
                    story_id="story_123456789abc",
                    text=text,
                    audio_path=audio,
                    alignment_path=sidecar,
                    force=False,
                )

            self.assertEqual(provider.call_count, 1)
            self.assertEqual(metadata["alignment_status"], "generated")
            self.assertEqual(cached_metadata["alignment_status"], "cached")
            self.assertEqual(first["text_sha256"], second["text_sha256"])
            self.assertEqual(
                json.loads(sidecar.read_text(encoding="utf-8"))["quality"][
                    "status"
                ],
                "passed",
            )

    def test_renders_local_timer_and_sentence_diagnostic(self) -> None:
        ffmpeg = find_executable("ffmpeg")
        if ffmpeg is None:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "voice.mp3"
            subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-c:a",
                    "libmp3lame",
                    "-y",
                    str(audio),
                ],
                check=True,
            )
            text = "First sentence. Second sentence."
            artifact = build_alignment_artifact(
                story_id="story_123456789abc",
                canonical_text=text,
                raw_alignment=proportional_alignment(text, 2.0),
                method="test",
                audio_sha256=sha256_file(audio),
                audio_duration_seconds=2.0,
            )
            alignment_path = root / "alignments" / "voice.alignment.json"
            alignment_path.parent.mkdir()
            timing = build_story_timing_manifest(
                video_number=5,
                records=[
                    {
                        "story_id": "story_123456789abc",
                        "title": "Diagnostic",
                        "text_path": root / "voice.txt",
                        "audio_path": audio,
                        "alignment_path": alignment_path,
                        "alignment": artifact,
                    }
                ],
                separator_duration_seconds=0,
            )

            output = render_diagnostic(root, timing, 1)

            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
