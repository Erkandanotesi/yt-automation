from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import elevenlabs_tts as tts
from content_validation import word_count
from elevenlabs_tts import (
    cached_audio_metadata,
    probe_audio,
    tts_input_fingerprint,
)
from file_utils import sha256_file, sha256_text


class FiveStoryLegacyFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source_fixture = (
            Path(__file__).resolve().parents[2]
            / "Legacy Automation Outputs"
            / "Video1"
        )
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.temporary_directory.name) / "Video1"
        shutil.copytree(source_fixture, cls.fixture)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_five_complete_story_pairs_and_audio_files(self) -> None:
        news_dir = self.fixture / "Haberler"
        audio_dir = self.fixture / "Sesler"
        summaries = sorted(
            path
            for path in news_dir.glob("*.txt")
            if not path.stem.endswith("-orijinal")
        )
        originals = sorted(news_dir.glob("*-orijinal.txt"))
        audio_files = sorted(audio_dir.glob("*.mp3"))

        self.assertEqual(len(summaries), 5)
        self.assertEqual(len(originals), 5)
        self.assertEqual(len(audio_files), 5)

        original_stems = {
            path.stem.removesuffix("-orijinal") for path in originals
        }
        self.assertEqual({path.stem for path in summaries}, original_stems)
        self.assertEqual(
            {path.stem for path in summaries},
            {path.stem for path in audio_files},
        )

        for summary, audio in zip(summaries, audio_files):
            text = summary.read_text(encoding="utf-8").strip()
            self.assertGreaterEqual(word_count(text), 150)
            self.assertLessEqual(word_count(text), 200)
            media = probe_audio(audio)
            self.assertGreater(media["duration_seconds"], 1)
            self.assertEqual(len(sha256_file(audio)), 64)

    def test_existing_audio_can_be_verified_from_local_metadata(self) -> None:
        summary = sorted(
            path
            for path in (self.fixture / "Haberler").glob("*.txt")
            if not path.stem.endswith("-orijinal")
        )[0]
        audio = self.fixture / "Sesler" / f"{summary.stem}.mp3"
        text = summary.read_text(encoding="utf-8").strip()
        fingerprint = tts_input_fingerprint(text)
        previous = {
            "text_file": str(summary),
            "audio_file": str(audio),
            "characters": len(text),
            "text_sha256": sha256_text(text),
            "input_fingerprint": fingerprint,
            "audio_sha256": sha256_file(audio),
        }

        cached = cached_audio_metadata(
            previous_entry=previous,
            output_path=audio,
            input_fingerprint=fingerprint,
        )

        self.assertIsNotNone(cached)
        self.assertEqual(cached["status"], "cached")
        self.assertGreater(cached["duration_seconds"], 1)

    def test_five_story_rerun_uses_zero_provider_calls(self) -> None:
        news_dir = self.fixture / "Haberler"
        audio_dir = self.fixture / "Sesler"
        summaries = sorted(
            path
            for path in news_dir.glob("*.txt")
            if not path.stem.endswith("-orijinal")
        )

        (self.fixture / "project_info.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "expected_count": 5,
                    "stories": [{"text": str(path)} for path in summaries],
                }
            ),
            encoding="utf-8",
        )
        previous_files = []
        for summary in summaries:
            text = summary.read_text(encoding="utf-8").strip()
            audio = audio_dir / f"{summary.stem}.mp3"
            previous_files.append(
                {
                    "text_file": str(summary),
                    "audio_file": str(audio),
                    "characters": len(text),
                    "text_sha256": sha256_text(text),
                    "input_fingerprint": tts_input_fingerprint(text),
                    "audio_sha256": sha256_file(audio),
                }
            )
        (audio_dir / "tts_info.json").write_text(
            json.dumps({"voice_name": "Fixture Voice", "files": previous_files}),
            encoding="utf-8",
        )

        with (
            patch.object(tts, "project_dir", return_value=self.fixture),
            patch.object(
                tts,
                "validate_voice",
                side_effect=AssertionError("provider validation must not run"),
            ),
            patch.object(
                tts,
                "generate_speech",
                side_effect=AssertionError("provider synthesis must not run"),
            ),
        ):
            self.assertEqual(tts.synthesize_video(1, overwrite=False), 0)

        updated = json.loads(
            (audio_dir / "tts_info.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["status"] for item in updated["files"]],
            ["cached"] * 5,
        )


if __name__ == "__main__":
    unittest.main()
