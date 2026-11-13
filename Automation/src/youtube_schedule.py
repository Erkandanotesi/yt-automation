"""Single source of truth for YouTube publication scheduling."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = SCRIPT_DIR / ".env"
TURKEY_TIMEZONE = timezone(timedelta(hours=3), name="UTC+03")

# Load once at process startup. Explicit process environment values retain priority
# over .env, matching python-dotenv's standard production-friendly behavior.
load_dotenv(ENV_FILE)


def _bounded_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a whole number, got {raw!r}.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


@dataclass(frozen=True)
class YouTubeScheduleConfig:
    publish_hour: int = 20
    interval_days: int = 2

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "YouTubeScheduleConfig":
        values = os.environ if environ is None else environ
        return cls(
            publish_hour=_bounded_int(
                values, "YOUTUBE_PUBLISH_HOUR", 20, 0, 23
            ),
            interval_days=_bounded_int(
                values, "YOUTUBE_PUBLISH_INTERVAL_DAYS", 2, 1, 365
            ),
        )

    def as_dict(self) -> dict[str, int | str]:
        return {
            **asdict(self),
            "timezone": "UTC+03:00",
        }


SCHEDULE_CONFIG = YouTubeScheduleConfig.from_environment()


def nearest_future_publish_time(
    moment: datetime,
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> datetime:
    """Return the nearest future configured hour, for a manual reset."""
    if moment.tzinfo is None:
        raise ValueError("Scheduling times must include a timezone.")
    local = moment.astimezone(TURKEY_TIMEZONE)
    today = datetime.combine(
        local.date(), time(config.publish_hour), TURKEY_TIMEZONE
    )
    if local < today:
        return today
    return datetime.combine(
        local.date() + timedelta(days=1),
        time(config.publish_hour),
        TURKEY_TIMEZONE,
    )


def shift_publish_slots(
    moment: datetime,
    amount: int,
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> datetime:
    """Move an aware time by a whole number of configured publication slots."""
    if moment.tzinfo is None:
        raise ValueError("Scheduling times must include a timezone.")
    local = moment.astimezone(TURKEY_TIMEZONE)
    return (local + timedelta(days=config.interval_days * amount)).replace(
        hour=config.publish_hour, minute=0, second=0, microsecond=0
    )


def next_cadence_slot(
    last_scheduled: datetime,
    now: datetime,
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> datetime:
    """Advance from the saved baseline until the first future cadence slot."""
    if last_scheduled.tzinfo is None or now.tzinfo is None:
        raise ValueError("Scheduling times must include a timezone.")
    candidate = shift_publish_slots(last_scheduled, 1, config)
    while candidate <= now:
        candidate = shift_publish_slots(candidate, 1, config)
    return candidate


def next_empty_publish_slot(
    now: datetime,
    occupied: Iterable[datetime],
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> datetime:
    """Return the nearest future configured slot not occupied on YouTube."""
    if now.tzinfo is None:
        raise ValueError("Scheduling times must include a timezone.")
    occupied_local: set[datetime] = set()
    for value in occupied:
        if value.tzinfo is None:
            raise ValueError("Occupied scheduling times must include a timezone.")
        occupied_local.add(
            value.astimezone(TURKEY_TIMEZONE).replace(microsecond=0)
        )
    candidate = nearest_future_publish_time(now, config)
    while candidate.replace(microsecond=0) in occupied_local:
        candidate = shift_publish_slots(candidate, 1, config)
    return candidate


def is_cadence_slot(
    candidate: datetime,
    last_scheduled: datetime,
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> bool:
    """Check whether a cached time remains valid under the current .env cadence."""
    if candidate.tzinfo is None or last_scheduled.tzinfo is None:
        return False
    local = candidate.astimezone(TURKEY_TIMEZONE)
    baseline = last_scheduled.astimezone(TURKEY_TIMEZONE)
    day_delta = (local.date() - baseline.date()).days
    return (
        candidate > last_scheduled
        and local.hour == config.publish_hour
        and local.minute == local.second == local.microsecond == 0
        and day_delta > 0
        and day_delta % config.interval_days == 0
    )
