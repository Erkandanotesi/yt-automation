from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import elevenlabs_tts as tts


class ElevenLabsBehaviorTests(unittest.TestCase):
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
