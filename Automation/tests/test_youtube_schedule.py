from datetime import datetime, timezone
from unittest import TestCase

from youtube_schedule import (
    TURKEY_TIMEZONE,
    YouTubeScheduleConfig,
    is_cadence_slot,
    nearest_future_publish_time,
    next_cadence_slot,
    next_empty_publish_slot,
)


class YouTubeScheduleTests(TestCase):
    def test_environment_values_define_schedule(self) -> None:
        config = YouTubeScheduleConfig.from_environment({
            "YOUTUBE_PUBLISH_HOUR": "18",
            "YOUTUBE_PUBLISH_INTERVAL_DAYS": "3",
        })
        self.assertEqual(config.publish_hour, 18)
        self.assertEqual(config.interval_days, 3)

    def test_invalid_environment_value_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a whole number"):
            YouTubeScheduleConfig.from_environment({
                "YOUTUBE_PUBLISH_HOUR": "evening",
            })

    def test_custom_config_drives_reset_and_cadence(self) -> None:
        config = YouTubeScheduleConfig(publish_hour=18, interval_days=3)
        baseline = datetime(2026, 8, 6, 20, tzinfo=TURKEY_TIMEZONE)
        now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
        self.assertEqual(
            nearest_future_publish_time(now, config).isoformat(),
            "2026-08-07T18:00:00+03:00",
        )
        candidate = next_cadence_slot(baseline, now, config)
        self.assertEqual(candidate.isoformat(), "2026-08-09T18:00:00+03:00")
        self.assertTrue(is_cadence_slot(candidate, baseline, config))

    def test_cached_time_from_old_cadence_is_rejected(self) -> None:
        config = YouTubeScheduleConfig(publish_hour=20, interval_days=2)
        baseline = datetime(2026, 8, 6, 20, tzinfo=TURKEY_TIMEZONE)
        stale = datetime(2026, 8, 7, 20, tzinfo=TURKEY_TIMEZONE)
        self.assertFalse(is_cadence_slot(stale, baseline, config))

    def test_next_empty_slot_uses_actual_occupied_dates(self) -> None:
        config = YouTubeScheduleConfig(publish_hour=20, interval_days=1)
        now = datetime(2026, 8, 31, 0, 30, tzinfo=TURKEY_TIMEZONE)
        occupied = [
            datetime(2026, 9, 2, 20, tzinfo=TURKEY_TIMEZONE),
            datetime(2026, 9, 3, 20, tzinfo=TURKEY_TIMEZONE),
        ]

        self.assertEqual(
            next_empty_publish_slot(now, occupied, config).isoformat(),
            "2026-08-31T20:00:00+03:00",
        )

    def test_next_empty_slot_advances_only_over_real_occupancy(self) -> None:
        config = YouTubeScheduleConfig(publish_hour=20, interval_days=1)
        now = datetime(2026, 8, 31, 0, 30, tzinfo=TURKEY_TIMEZONE)
        occupied = [
            datetime(2026, 8, 31, 17, tzinfo=timezone.utc),
            datetime(2026, 9, 1, 17, tzinfo=timezone.utc),
        ]

        self.assertEqual(
            next_empty_publish_slot(now, occupied, config).isoformat(),
            "2026-09-02T20:00:00+03:00",
        )
