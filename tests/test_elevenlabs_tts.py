from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import elevenlabs_tts as tts
import requests
from tests.test_alignment_utils import proportional_alignment
from tooling import find_executable


class ElevenLabsBehaviorTests(unittest.TestCase):
    def test_timestamp_response_decodes_audio_and_original_alignment(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "audio_base64": base64.b64encode(b"audio").decode("ascii"),
                "alignment": {
                    "characters": ["H", "i"],
                    "character_start_times_seconds": [0, 0.1],
                    "character_end_times_seconds": [0.1, 0.2],
                },
                "normalized_alignment": None,
            }
        ).encode("utf-8")
        audio, alignment = tts.decode_timestamp_response(response)
        self.assertEqual(audio, b"audio")
        self.assertEqual(alignment["characters"], ["H", "i"])

    def test_speech_request_uses_with_timestamps_endpoint(self) -> None:
        sentinel = requests.Response()
        with patch.object(tts, "request_with_retry", return_value=sentinel) as request:
            result = tts.speech_request("Tiny test.", "hidden-key")
        self.assertIs(result, sentinel)
        args, kwargs = request.call_args
        self.assertTrue(args[1].endswith("/with-timestamps"))
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")

    def test_mocked_timestamp_generation_saves_audio_and_alignment(self) -> None:
        ffmpeg = find_executable("ffmpeg")
        if ffmpeg is None:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            news = project / "Haberler"
            news.mkdir()
            text_path = news / "01_story_123456789abc_test.txt"
            text = "A tiny timestamp test."
            text_path.write_text(text, encoding="utf-8")
            (project / "project_info.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "expected_count": 1,
                        "stories": [
                            {
                                "story_id": "story_123456789abc",
                                "text": str(text_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source_audio = project / "source.mp3"
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
                    str(source_audio),
                ],
                check=True,
            )
            audio_bytes = source_audio.read_bytes()
            with (
                patch.object(tts, "project_dir", return_value=project),
                patch.object(tts, "project_name", return_value="Video 5"),
                patch.object(tts, "validate_voice", return_value={"name": "Mock"}),
                patch.object(
                    tts,
                    "generate_speech",
                    return_value=(
                        audio_bytes,
                        {
                            "api_key_slot": "mock",
                            "character_cost": "22",
                            "request_id": "request_mock",
                            "content_type": "audio/mpeg",
                            "response_bytes": len(audio_bytes),
                        },
                        proportional_alignment(text, 1.0),
                    ),
                ),
            ):
                self.assertEqual(tts.synthesize_video(5, overwrite=False), 0)

            info = json.loads(
                (project / "Sesler" / "tts_info.json").read_text(encoding="utf-8")
            )
            entry = info["files"][0]
            alignment_path = Path(entry["alignment_file"])
            self.assertTrue(alignment_path.is_file())
            self.assertEqual(entry["alignment_method"], "tts_with_timestamps")
            alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
            self.assertEqual(alignment["canonical_text"], text)
            self.assertEqual(alignment["quality"]["status"], "passed")

    def test_fingerprint_changes_with_text_or_settings(self) -> None:
        first = tts.tts_input_fingerprint("A tiny test.")
        second = tts.tts_input_fingerprint("A different tiny test.")
        self.assertNotEqual(first, second)

        with patch.object(tts, "SPEED", tts.SPEED + 0.01):
            changed_settings = tts.tts_input_fingerprint("A tiny test.")
        self.assertNotEqual(first, changed_settings)

    def test_quota_error_switches_to_next_configured_slot_without_network(self) -> None:
        original_keys = tts.API_KEYS
        original_index = tts.ACTIVE_KEY_INDEX
        original_remaining = dict(tts.KNOWN_REMAINING_CREDITS)
        calls: list[str] = []
        try:
            tts.API_KEYS = [("1", "hidden-one"), ("6", "hidden-six")]
            tts.ACTIVE_KEY_INDEX = 0
            tts.KNOWN_REMAINING_CREDITS.clear()

            def operation(key: str) -> str:
                calls.append(key)
                if key == "hidden-one":
                    raise tts.ElevenLabsAPIError(
                        429,
                        "quota_exceeded",
                        "You have 0 credits remaining",
                    )
                return "ok"

            result, label = tts.with_key_fallback(operation)

            self.assertEqual(result, "ok")
            self.assertEqual(label, "6")
            self.assertEqual(calls, ["hidden-one", "hidden-six"])
        finally:
            tts.API_KEYS = original_keys
            tts.ACTIVE_KEY_INDEX = original_index
            tts.KNOWN_REMAINING_CREDITS.clear()
            tts.KNOWN_REMAINING_CREDITS.update(original_remaining)

    def test_non_switchable_error_stops_immediately(self) -> None:
        original_keys = tts.API_KEYS
        original_index = tts.ACTIVE_KEY_INDEX
        try:
            tts.API_KEYS = [("1", "hidden-one"), ("2", "hidden-two")]
            tts.ACTIVE_KEY_INDEX = 0
            with self.assertRaises(tts.ElevenLabsAPIError):
                tts.with_key_fallback(
                    lambda _: (_ for _ in ()).throw(
                        tts.ElevenLabsAPIError(400, "bad_request", "invalid text")
                    )
                )
            self.assertEqual(tts.ACTIVE_KEY_INDEX, 0)
        finally:
            tts.API_KEYS = original_keys
            tts.ACTIVE_KEY_INDEX = original_index

    def test_incomplete_project_manifest_blocks_tts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "Haberler").mkdir()
            (project / "project_info.json").write_text(
                json.dumps({"status": "incomplete", "stories": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                tts.summary_files(project)

    def test_complete_manifest_ignores_stale_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            news = project / "Haberler"
            news.mkdir()
            selected = news / "01_story_abc_script.txt"
            selected.write_text("selected", encoding="utf-8")
            (news / "99_stale.txt").write_text("stale", encoding="utf-8")
            (project / "project_info.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "expected_count": 1,
                        "stories": [{"text": str(selected)}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(tts.summary_files(project), [selected])


if __name__ == "__main__":
    unittest.main()
