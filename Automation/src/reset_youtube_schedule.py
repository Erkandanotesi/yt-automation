"""Interactively override the date used by the next YouTube upload."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from youtube_schedule import (
    SCHEDULE_CONFIG,
    TURKEY_TIMEZONE,
    YouTubeScheduleConfig,
    nearest_future_publish_time,
    next_cadence_slot,
    shift_publish_slots,
)


SCRIPT_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = SCRIPT_DIR / "data" / "youtube_schedule_state.json"
OVERRIDE_KEY = "next_scheduled_publish_at_override"


def next_slot_after(
    moment: datetime,
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> datetime:
    return nearest_future_publish_time(moment, config)


def shift_slots(
    moment: datetime,
    amount: int,
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> datetime:
    return shift_publish_slots(moment, amount, config)


def current_next_upload(
    state: dict[str, object],
    now: datetime,
    config: YouTubeScheduleConfig = SCHEDULE_CONFIG,
) -> datetime:
    raw_override = state.get(OVERRIDE_KEY)
    if raw_override:
        parsed = datetime.fromisoformat(str(raw_override).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"{OVERRIDE_KEY} must include a timezone.")
        if parsed > now:
            return parsed.astimezone(TURKEY_TIMEZONE)

    raw_last = state.get("last_scheduled_publish_at")
    if not raw_last:
        raise ValueError("last_scheduled_publish_at is missing from the schedule state.")
    last = datetime.fromisoformat(str(raw_last).replace("Z", "+00:00"))
    if last.tzinfo is None:
        raise ValueError("last_scheduled_publish_at must include a timezone.")
    return next_cadence_slot(last, now, config)


def atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as temporary:
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def prompt_choice() -> str:
    print("What would you like to do?")
    print(
        "  Enter or 1 - Reset to the nearest future "
        f"{SCHEDULE_CONFIG.publish_hour:02d}:00 slot"
    )
    print("  2 - Bring the next upload forward")
    print("  3 - Postpone the next upload")
    choice = input("Choose Enter/1, 2, or 3: ").strip() or "1"
    if choice not in {"1", "2", "3"}:
        raise ValueError("Invalid choice. Enter 1, 2, or 3.")
    return choice


def prompt_amount() -> int:
    raw = input("How many upload slots? ").strip()
    try:
        amount = int(raw)
    except ValueError as error:
        raise ValueError("The number of slots must be a whole number.") from error
    if amount < 1:
        raise ValueError("The number of slots must be at least 1.")
    return amount


def main() -> int:
    if not STATE_FILE.is_file():
        raise FileNotFoundError(f"YouTube schedule state was not found: {STATE_FILE}")
    with STATE_FILE.open("r", encoding="utf-8-sig") as source:
        state = json.load(source)
    if not isinstance(state, dict):
        raise ValueError("YouTube schedule state must be a JSON object.")

    now = datetime.now(timezone.utc)
    current = current_next_upload(state, now)
    print(f"Current next upload: {current.isoformat()}")
    print()
    choice = prompt_choice()

    if choice == "1":
        updated = next_slot_after(now)
    else:
        amount = prompt_amount()
        updated = shift_slots(current, -amount if choice == "2" else amount)
        if updated <= now:
            print()
            print("[WARNING] That choice would move the upload into the past.")
            print("The schedule file was not changed. Choose fewer slots or reset.")
            return 2

    # Check again immediately before writing in case a slot boundary passed
    # while the user was answering the prompts.
    write_time = datetime.now(timezone.utc)
    if updated <= write_time:
        print()
        print(f"[WARNING] {updated.isoformat()} is now a stale upload date.")
        print("The schedule file was not changed. Run this command again.")
        return 2

    # next_publish_slot uses this exact one-time value. A successful upload
    # rewrites the state file, automatically removing the override.
    state[OVERRIDE_KEY] = updated.isoformat()
    state.pop("ignore_manifest_reservations_once", None)
    state["updated_at_utc"] = write_time.isoformat()
    atomic_write_json(STATE_FILE, state)

    print()
    print(f"Next upload changed: {current.isoformat()} -> {updated.isoformat()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as error:
        print()
        print(f"[WARNING] {error}")
        print("The schedule file was not changed.")
        raise SystemExit(2)
