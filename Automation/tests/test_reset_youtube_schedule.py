from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.reset_youtube_schedule import next_slot_after, prompt_choice, shift_slots
from youtube_schedule import YouTubeScheduleConfig


TURKEY = ZoneInfo("Europe/Istanbul")
TWO_DAY_SCHEDULE = YouTubeScheduleConfig(publish_hour=20, interval_days=2)


class ResetYoutubeScheduleTests(TestCase):
    def test_next_slot_before_evening_is_evening(self) -> None:
        upcoming = next_slot_after(
            datetime(2026, 8, 4, 11, 59, tzinfo=TURKEY), TWO_DAY_SCHEDULE
        )
        self.assertEqual(upcoming.isoformat(), "2026-08-04T20:00:00+03:00")

    def test_next_slot_between_slots_is_evening(self) -> None:
        upcoming = next_slot_after(
            datetime(2026, 8, 4, 16, 0, tzinfo=TURKEY), TWO_DAY_SCHEDULE
        )
        self.assertEqual(upcoming.isoformat(), "2026-08-04T20:00:00+03:00")

    def test_enter_selects_reset(self) -> None:
        with patch("builtins.input", return_value=""):
            self.assertEqual(prompt_choice(), "1")

    def test_postpone_one_slot_advances_two_days(self) -> None:
        updated = shift_slots(
            datetime(2026, 8, 4, 20, 0, tzinfo=TURKEY), 1, TWO_DAY_SCHEDULE
        )
        self.assertEqual(updated.isoformat(), "2026-08-06T20:00:00+03:00")

    def test_bring_forward_two_slots(self) -> None:
        updated = shift_slots(
            datetime(2026, 8, 5, 20, 0, tzinfo=TURKEY), -2, TWO_DAY_SCHEDULE
        )
        self.assertEqual(updated.isoformat(), "2026-08-01T20:00:00+03:00")
