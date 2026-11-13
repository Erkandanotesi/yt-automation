from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_utils


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_json_serializes_windows_safe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            configured_path = Path(directory) / "Assets" / "Media"

            file_utils.atomic_write_json(target, {"archive_directory": configured_path})

            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["archive_directory"], str(configured_path))

    def test_atomic_write_replaces_complete_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_text("old", encoding="utf-8")

            file_utils.atomic_write_text(target, "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_interrupted_replace_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_text("known-good", encoding="utf-8")

            with patch("file_utils.os.replace", side_effect=OSError("simulated stop")):
                with self.assertRaisesRegex(OSError, "simulated stop"):
                    file_utils.atomic_write_text(target, "partial-new-data")

            self.assertEqual(target.read_text(encoding="utf-8"), "known-good")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_failed_temporary_validation_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "voice.mp3"
            target.write_bytes(b"known-good")

            def reject(_: Path) -> None:
                raise RuntimeError("invalid audio")

            with self.assertRaisesRegex(RuntimeError, "invalid audio"):
                file_utils.atomic_write_bytes(
                    target,
                    b"bad replacement",
                    validate_temporary=reject,
                )

            self.assertEqual(target.read_bytes(), b"known-good")

    def test_story_id_is_stable_and_url_specific(self) -> None:
        first = file_utils.stable_story_id("https://example.com/news")
        self.assertEqual(first, file_utils.stable_story_id("https://example.com/news"))
        self.assertNotEqual(
            first,
            file_utils.stable_story_id("https://example.com/other-news"),
        )
        self.assertRegex(first, r"^story_[0-9a-f]{12}$")


if __name__ == "__main__":
    unittest.main()
