from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from file_utils import atomic_write_json, atomic_write_text, json_fingerprint, sha256_file


SCHEMA_VERSION = 1
SELECTION_FILENAME = "lead_story_selection.json"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def selection_record_path(video_dir: Path) -> Path:
    return video_dir / "orchestration" / SELECTION_FILENAME


def _ordered_stories(project: dict) -> list[dict]:
    stories = project.get("stories", [])
    try:
        expected_count = int(project.get("expected_count", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Lead-story selection requires a positive expected_count."
        ) from exc
    if expected_count < 1:
        raise RuntimeError("Lead-story selection requires a positive expected_count.")
    if not isinstance(stories, list) or len(stories) != expected_count:
        raise RuntimeError(
            f"Lead-story selection requires exactly {expected_count} stories."
        )
    if not all(isinstance(item, dict) for item in stories):
        raise RuntimeError("The project manifest contains an invalid story record.")
    ordered = sorted(stories, key=lambda item: int(item.get("order", 0)))
    orders = [int(item.get("order", 0)) for item in ordered]
    if orders != list(range(1, expected_count + 1)):
        raise RuntimeError(f"Story order is not contiguous: {orders}")
    story_ids = [str(item.get("story_id", "")).strip() for item in ordered]
    if any(not story_id for story_id in story_ids) or len(set(story_ids)) != len(
        story_ids
    ):
        raise RuntimeError("Lead-story selection requires unique stable story IDs.")
    for story in ordered:
        for path_key, hash_key in (
            ("text", "narration_sha256"),
            ("original", "source_sha256"),
        ):
            path = Path(str(story.get(path_key, "")))
            expected_hash = str(story.get(hash_key, ""))
            if not path.is_file() or not expected_hash or sha256_file(path) != expected_hash:
                raise RuntimeError(
                    f"Story artifact is missing or changed before lead selection: {path}"
                )
    return ordered


def _candidate_fingerprint(stories: list[dict]) -> str:
    # The set, rather than the current display order, makes retries idempotent
    # after the selected story has already been moved to position one.
    return json_fingerprint(sorted(str(item["story_id"]) for item in stories))


def selection_is_pending(video_dir: Path) -> bool:
    project = _read_json(video_dir / "project_info.json")
    if project.get("status") != "complete":
        return False
    return bool(project.get("lead_story_selection_required")) and not selection_is_consistent(
        project
    )


def selection_is_consistent(project: dict) -> bool:
    if not project.get("lead_story_selection_required"):
        return True
    selection = project.get("lead_story_selection", {})
    stories = project.get("stories", [])
    return bool(
        isinstance(selection, dict)
        and selection.get("status") == "selected"
        and stories
        and stories[0].get("story_id") == selection.get("selected_story_id")
        and [int(item.get("order", 0)) for item in stories]
        == list(range(1, len(stories) + 1))
    )


def finalize_lead_story(
    video_dir: Path,
    *,
    choose: Callable[..., str],
    timeout_seconds: float = 120.0,
) -> dict:
    """Persist and apply one lead-story decision, safely reusable after retries."""
    project_path = video_dir / "project_info.json"
    project = _read_json(project_path)
    if project.get("status") != "complete":
        raise RuntimeError("Lead-story selection requires a complete news manifest.")
    stories = _ordered_stories(project)
    fingerprint = _candidate_fingerprint(stories)
    record_path = selection_record_path(video_dir)
    record = _read_json(record_path)
    story_ids = [str(item["story_id"]) for item in stories]

    if (
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("candidate_set_fingerprint") == fingerprint
        and record.get("selected_story_id") in story_ids
    ):
        selected_story_id = str(record["selected_story_id"])
        decision_reused = True
    else:
        options = {
            str(index): str(story.get("title") or story.get("source_title") or story["story_id"])
            for index, story in enumerate(stories, start=1)
        }
        selected_key = choose(
            "Which news should be first and drive the YouTube title and thumbnail?",
            options,
            default="1",
            timeout_seconds=timeout_seconds,
            separator="-",
        )
        if selected_key not in options:
            raise RuntimeError(f"Invalid lead-story choice returned: {selected_key}")
        selected_story_id = story_ids[int(selected_key) - 1]
        record = {
            "schema_version": SCHEMA_VERSION,
            "status": "selected",
            "candidate_set_fingerprint": fingerprint,
            "candidate_story_ids_in_display_order": story_ids,
            "default_story_id": story_ids[0],
            "selected_story_id": selected_story_id,
            "selected_display_position": int(selected_key),
            "selected_at_utc": datetime.now(timezone.utc).isoformat(),
            "timeout_seconds": timeout_seconds,
        }
        # This record is the write-ahead decision log. If the process stops
        # before the manifest commit, the next run reuses it without asking.
        atomic_write_json(record_path, record)
        decision_reused = False

    selected = next(item for item in stories if item["story_id"] == selected_story_id)
    reordered = [selected, *[item for item in stories if item is not selected]]
    for order, story in enumerate(reordered, start=1):
        story["order"] = order

    combined = "\n\n".join(
        Path(str(story["text"])).read_text(encoding="utf-8").strip()
        for story in reordered
    ).strip()
    atomic_write_text(video_dir / "video_script.txt", combined + "\n")

    applied = {
        **record,
        "status": "selected",
        "applied_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision_reused": decision_reused,
    }
    project["stories"] = reordered
    project["lead_story_selection_required"] = True
    project["lead_story_selection"] = applied
    atomic_write_json(project_path, project)
    return applied
