from __future__ import annotations

import unittest

from file_locations import (
    AUTOMATION_ROOT,
    CONFIG_ROOT,
    DATA_ROOT,
    EDITING_REFERENCE_FILE,
    PROCESSED_URLS_FILE,
    NEWS_HISTORY_RESET_FILE,
    NEWS_ARCHIVE_ROOT,
    SOURCE_ROOT,
    YOUTUBE_SCHEDULE_STATE_FILE,
    parse_video_number,
)


class FileLocationTests(unittest.TestCase):
    def test_modular_roots_are_under_automation(self) -> None:
        self.assertEqual(SOURCE_ROOT.parent, AUTOMATION_ROOT)
        self.assertEqual(CONFIG_ROOT, AUTOMATION_ROOT / "config")
        self.assertEqual(DATA_ROOT, AUTOMATION_ROOT / "data")
        self.assertEqual(PROCESSED_URLS_FILE, DATA_ROOT / "processed_urls.json")
        self.assertEqual(
            NEWS_HISTORY_RESET_FILE,
            DATA_ROOT / "news_history_reset.json",
        )
        self.assertEqual(NEWS_ARCHIVE_ROOT, DATA_ROOT / "news_archive")
        self.assertEqual(
            YOUTUBE_SCHEDULE_STATE_FILE, DATA_ROOT / "youtube_schedule_state.json"
        )
        self.assertEqual(
            EDITING_REFERENCE_FILE,
            AUTOMATION_ROOT / "references" / "capcut_screenshot.png",
        )

    def test_video_reference_accepts_number_or_folder_name(self) -> None:
        self.assertEqual(parse_video_number(5), 5)
        self.assertEqual(parse_video_number("5"), 5)
        self.assertEqual(parse_video_number("Video 5"), 5)
        with self.assertRaises(ValueError):
            parse_video_number("video-five")


if __name__ == "__main__":
    unittest.main()
