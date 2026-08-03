from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_utils import (  # noqa: E402
    atomic_write_json,
    json_fingerprint,
    report_cli_error,
    sha256_file,
    sha256_text,
)
from media_index import (  # noqa: E402
    MediaIndexSettings,
    is_excluded_in_video_media_path,
    load_media_index,
    normalize_search_text,
    tokenize,
    update_usage_events,
)
from file_locations import project_dir, project_name  # noqa: E402
from separator_policy import require_separator_checkpoint  # noqa: E402
from tooling import find_executable  # noqa: E402


MEDIA_PLAN_SCHEMA_VERSION = 1
SELECTOR_CACHE_SCHEMA_VERSION = 1
PEXELS_QUERY_CACHE_SCHEMA_VERSION = 1
STAGE_NAME = "archive_visuals"

GENERIC_CONTEXT_TOKENS = {
    "ai", "artificial", "company", "computer", "digital", "image", "internet",
    "launch", "launched", "launching", "model", "news", "online", "report",
    "software", "system", "tech", "technology", "video", "website",
}
WEAK_CONTEXT_TOKENS = GENERIC_CONTEXT_TOKENS | {
    "after", "before", "block", "blocked", "blocking", "built", "change",
    "changed", "changes", "commission", "company", "continued", "control",
    "current", "currently", "default", "delayed", "development", "enabled",
    "including", "install", "installed", "installation", "its", "lets",
    "accepted", "business", "but", "can", "could", "december", "enterprise",
    "february", "generated", "including", "keys", "location", "machine", "may",
    "months", "must", "new", "not", "operating", "performance", "policy", "pro",
    "proposed", "providers", "released", "remain", "rule", "rules", "said", "says",
    "service", "settings", "stable", "system", "that", "they", "under", "use",
    "used", "users", "when", "will", "work", "worldwide",
}
ENTITY_NOISE = {
    "ads", "after", "ai", "allies", "american", "august", "before", "built",
    "by", "cameras", "chatbots", "critics", "december", "development",
    "either", "enforcement", "february", "foldable", "hardware", "he", "his",
    "lawyers", "malicious", "manually", "model", "noncompliant", "other",
    "pcs", "pro", "project", "providers", "since", "the nazis", "they", "tom",
    "ultra", "under", "users", "win",
}


def _is_in_video_media_asset(asset: dict) -> bool:
    paths = (
        asset.get("absolute_path", ""),
        asset.get("workspace_relative_path", ""),
    )
    return not any(
        path and is_excluded_in_video_media_path(path)
        for path in paths
    )


def _is_pexels_asset(asset: dict) -> bool:
    identity = normalize_search_text(
        " ".join(
            [
                str(asset.get("source_url", "")),
                str(asset.get("workspace_relative_path", "")),
                str(asset.get("folder", "")),
            ]
        )
    )
    return "pexels.com" in identity or "online retrieved pexels" in identity


def _local_repository_index(index: dict) -> dict:
    """Return only user-owned/generated media for the first selection pass.

    Downloaded Pexels files remain in the persistent media index so they can be
    reused later by the Pexels fallback resolver, but they must not compete with
    the curated local repository in the initial shortlist.
    """
    return {
        **index,
        "assets": [
            asset
            for asset in index.get("assets", [])
            if not _is_pexels_asset(asset)
        ],
    }


def _hero_exclusions_for_story(
    hero_owners: dict[str, set[str]],
    story_id: str,
) -> set[str]:
    """Keep current-video hero pixels out of every archive-image window."""
    del story_id  # Kept in the signature for call-site clarity and compatibility.
    return set(hero_owners)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MediaSelectionSettings:
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "medium"
    shortlist_size: int = 8
    minimum_window_seconds: float = 5.0
    target_window_seconds: float = 7.0
    maximum_window_seconds: float = 9.0
    minimum_local_score: float = 1.0
    api_retries: int = 2
    timeout_seconds: float = 120.0
    copy_selected_assets: bool = False
    pexels_fallback_enabled: bool = True
    pexels_max_downloads_per_video: int = 20
    pexels_max_searches_per_video: int = 20
    pexels_query_model: str = "gpt-5.6-luna"
    pexels_query_reasoning_effort: str = "low"
    maximum_uses_per_video: int = 2
    minimum_ai_confidence: float = 0.45

    @classmethod
    def from_environment(cls) -> "MediaSelectionSettings":
        return cls(
            model=os.getenv(
                "MEDIA_SELECTION_MODEL",
                os.getenv("STORYBOARD_MODEL", "gpt-5.6-luna"),
            ).strip(),
            reasoning_effort=os.getenv(
                "MEDIA_SELECTION_REASONING_EFFORT",
                "medium",
            ).strip().lower(),
            shortlist_size=int(os.getenv("MEDIA_SHORTLIST_SIZE", "8")),
            minimum_window_seconds=float(
                os.getenv("MEDIA_MIN_WINDOW_SECONDS", "5")
            ),
            target_window_seconds=float(
                os.getenv("MEDIA_TARGET_WINDOW_SECONDS", "7")
            ),
            maximum_window_seconds=float(
                os.getenv("MEDIA_MAX_WINDOW_SECONDS", "9")
            ),
            minimum_local_score=float(
                os.getenv("MEDIA_MIN_LOCAL_SCORE", "1")
            ),
            api_retries=int(os.getenv("MEDIA_SELECTION_API_RETRIES", "2")),
            timeout_seconds=float(
                os.getenv("MEDIA_SELECTION_TIMEOUT", "120")
            ),
            copy_selected_assets=os.getenv(
                "MEDIA_COPY_SELECTED",
                "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            pexels_fallback_enabled=os.getenv(
                "PEXELS_FALLBACK_ENABLED",
                "true",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            pexels_max_downloads_per_video=int(
                os.getenv("PEXELS_MAX_DOWNLOADS_PER_VIDEO", "20")
            ),
            pexels_max_searches_per_video=int(
                os.getenv("PEXELS_MAX_SEARCHES_PER_VIDEO", "20")
            ),
            pexels_query_model=os.getenv(
                "PEXELS_QUERY_MODEL",
                "gpt-5.6-luna",
            ).strip(),
            pexels_query_reasoning_effort=os.getenv(
                "PEXELS_QUERY_REASONING_EFFORT",
                "low",
            ).strip().lower(),
            maximum_uses_per_video=int(
                os.getenv("MEDIA_MAX_USES_PER_VIDEO", "2")
            ),
            minimum_ai_confidence=float(
                os.getenv("MEDIA_MIN_AI_CONFIDENCE", "0.45")
            ),
        )

    def validate(self) -> None:
        if not self.model:
            raise ValueError("MEDIA_SELECTION_MODEL cannot be empty.")
        if self.reasoning_effort not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("MEDIA_SELECTION_REASONING_EFFORT is invalid.")
        if self.shortlist_size < 2:
            raise ValueError("MEDIA_SHORTLIST_SIZE must be at least 2.")
        if not (
            0 < self.minimum_window_seconds
            <= self.target_window_seconds
            <= self.maximum_window_seconds
        ):
            raise ValueError("Media window duration settings are inconsistent.")
        if self.api_retries < 1 or self.timeout_seconds <= 0:
            raise ValueError("Media selection API retry/timeout must be positive.")
        if not 0 <= self.pexels_max_downloads_per_video <= 30:
            raise ValueError(
                "PEXELS_MAX_DOWNLOADS_PER_VIDEO must be between 0 and 30."
            )
        if not 0 <= self.pexels_max_searches_per_video <= 30:
            raise ValueError(
                "PEXELS_MAX_SEARCHES_PER_VIDEO must be between 0 and 30."
            )
        if not self.pexels_query_model:
            raise ValueError("PEXELS_QUERY_MODEL cannot be empty.")
        if self.pexels_query_reasoning_effort not in {
            "none", "low", "medium", "high", "xhigh", "max",
        }:
            raise ValueError("PEXELS_QUERY_REASONING_EFFORT is invalid.")
        if not 1 <= self.maximum_uses_per_video <= 10:
            raise ValueError("MEDIA_MAX_USES_PER_VIDEO must be between 1 and 10.")
        if not 0.0 <= self.minimum_ai_confidence <= 1.0:
            raise ValueError("MEDIA_MIN_AI_CONFIDENCE must be between 0 and 1.")


class WindowDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_id: str = Field(min_length=1)
    relevance_status: Literal["relevant", "images_are_irrelevant"] = "relevant"
    selected_asset_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    selection_reason: str = Field(min_length=12, max_length=700)
    alternative_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    online_search_query: str = Field(default="", max_length=180)
    required_entity: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def validate_relevance_outcome(self) -> "WindowDecision":
        if self.relevance_status == "images_are_irrelevant":
            # Structured model output can contain internally inconsistent fields.
            # Normalize them here; deterministic story/window grounding below will
            # repair missing query/entity values without another provider request.
            self.selected_asset_id = None
        return self


class StoryMediaDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[WindowDecision]


class PexelsQueryGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, max_length=80)
    window_ids: list[str] = Field(min_length=1, max_length=20)
    primary_query: str = Field(min_length=3, max_length=100)
    alternative_queries: list[str] = Field(default_factory=list, max_length=2)
    visual_subject: str = Field(min_length=3, max_length=120)
    rationale: str = Field(min_length=12, max_length=400)


class StoryPexelsQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[PexelsQueryGroup] = Field(min_length=1, max_length=12)


Selector = Callable[
    [dict, list[dict], MediaSelectionSettings],
    StoryMediaDecisions,
]


def load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a JSON object: {path}")
    return value


def _selector_cache_path(video_dir: Path, story_id: str) -> Path:
    safe_story_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", story_id)
    return video_dir / "05_visuals" / "selector_cache" / f"{safe_story_id}.json"


def _pexels_query_cache_path(video_dir: Path, story_id: str) -> Path:
    safe_story_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", story_id)
    return (
        video_dir
        / "05_visuals"
        / "pexels_query_cache"
        / f"{safe_story_id}.json"
    )


def _load_pexels_query_cache(
    path: Path,
    fingerprint: str,
) -> StoryPexelsQueryPlan | None:
    if not path.is_file():
        return None
    try:
        payload = load_json(path, "Pexels query cache")
        if (
            payload.get("schema_version") != PEXELS_QUERY_CACHE_SCHEMA_VERSION
            or payload.get("input_fingerprint") != fingerprint
        ):
            return None
        return StoryPexelsQueryPlan.model_validate(payload.get("plan", {}))
    except (RuntimeError, ValueError):
        return None


def _save_pexels_query_cache(
    path: Path,
    *,
    story_id: str,
    fingerprint: str,
    plan: StoryPexelsQueryPlan,
) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": PEXELS_QUERY_CACHE_SCHEMA_VERSION,
            "story_id": story_id,
            "input_fingerprint": fingerprint,
            "saved_at_utc": utc_now(),
            "plan": plan.model_dump(mode="json"),
        },
    )


def _load_selector_cache(
    path: Path,
    fingerprint: str,
    compatible_fingerprints: set[str] | None = None,
) -> StoryMediaDecisions | None:
    if not path.is_file():
        return None
    try:
        payload = load_json(path, "Media selector cache")
        if (
            payload.get("schema_version") != SELECTOR_CACHE_SCHEMA_VERSION
            or (
                payload.get("input_fingerprint") != fingerprint
                and payload.get("input_fingerprint")
                not in (compatible_fingerprints or set())
            )
        ):
            return None
        return StoryMediaDecisions.model_validate(payload.get("decisions", {}))
    except (RuntimeError, ValueError):
        return None


def _save_selector_cache(
    path: Path,
    *,
    story_id: str,
    fingerprint: str,
    decisions: StoryMediaDecisions,
) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": SELECTOR_CACHE_SCHEMA_VERSION,
            "story_id": story_id,
            "input_fingerprint": fingerprint,
            "saved_at_utc": utc_now(),
            "decisions": decisions.model_dump(mode="json"),
        },
    )


def _require_inputs(video_dir: Path) -> tuple[dict, dict, dict]:
    state = load_json(video_dir / "state.json", "Stage state")
    timing = load_json(video_dir / "story_timing.json", "Story timing manifest")
    require_separator_checkpoint(state, timing)
    storyboard = load_json(
        video_dir / "04_storyboard" / "storyboard.json",
        "Storyboard",
    )
    return state, timing, storyboard


def _write_stage(video_dir: Path, state: dict, stage: dict) -> None:
    stage["updated_at_utc"] = utc_now()
    state.setdefault("stages", {})[STAGE_NAME] = stage
    state["updated_at_utc"] = stage["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    valid = sorted((max(0.0, start), max(0.0, end)) for start, end in intervals if end > start)
    merged: list[tuple[float, float]] = []
    for start, end in valid:
        if not merged or start > merged[-1][1] + 0.001:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def subtract_intervals(
    duration: float,
    covered: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    if duration <= 0:
        raise ValueError("Narration duration must be positive.")
    result: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merge_intervals(covered):
        start = min(duration, max(0.0, start))
        end = min(duration, max(start, end))
        if start > cursor + 0.001:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.001:
        result.append((cursor, duration))
    return result


def _boundaries_for_interval(
    story: dict,
    start: float,
    end: float,
) -> list[float]:
    boundaries = {start, end}
    for collection in ("phrases", "sentences", "visual_beats"):
        for item in story.get(collection, []):
            for field in ("start", "end"):
                value = float(item.get(field, -1))
                if start + 0.05 < value < end - 0.05:
                    boundaries.add(value)
    return sorted(boundaries)


def _split_uncovered_interval(
    start: float,
    end: float,
    boundaries: list[float],
    settings: MediaSelectionSettings,
) -> list[tuple[float, float]]:
    windows: list[tuple[float, float]] = []
    cursor = start
    while end - cursor > settings.maximum_window_seconds + 0.001:
        minimum = cursor + settings.minimum_window_seconds
        maximum = cursor + settings.maximum_window_seconds
        target = cursor + settings.target_window_seconds
        choices = [
            value
            for value in boundaries
            if minimum - 0.001 <= value <= maximum + 0.001
        ]
        split = min(choices, key=lambda value: abs(value - target)) if choices else maximum
        split = min(end, max(cursor + 0.1, split))
        windows.append((cursor, split))
        cursor = split
    if end > cursor + 0.001:
        if (
            windows
            and end - cursor < settings.minimum_window_seconds
            and end - windows[-1][0] <= settings.maximum_window_seconds + 0.001
        ):
            windows[-1] = (windows[-1][0], end)
        elif windows and end - cursor < settings.minimum_window_seconds:
            # A sentence-aligned split can leave a tiny tail. Rebalance the last
            # two windows instead of extending one beyond the hard cadence limit.
            replace_from = max(0, len(windows) - 2)
            rebalance_start = windows[replace_from][0]
            span = end - rebalance_start
            segment_count = max(2, math.ceil(span / settings.maximum_window_seconds))
            step = span / segment_count
            windows[replace_from:] = [
                (
                    rebalance_start + step * index,
                    rebalance_start + step * (index + 1),
                )
                for index in range(segment_count)
            ]
        else:
            windows.append((cursor, end))
    if any(
        window_end - window_start > settings.maximum_window_seconds + 0.001
        for window_start, window_end in windows
    ):
        raise RuntimeError("Archive-window splitting exceeded the hard maximum.")
    return windows


def _overlap(start: float, end: float, item: dict) -> bool:
    item_start = float(item.get("start", 0))
    item_end = float(item.get("end", item_start))
    return item_end > start + 0.001 and item_start < end - 0.001


def _item_text(item: dict) -> str:
    return str(item.get("text") or item.get("important_claim") or "").strip()


def _overlapping_text(
    story: dict,
    collection: str,
    start: float,
    end: float,
) -> str:
    pieces: list[str] = []
    for item in story.get(collection, []):
        if not _overlap(start, end, item):
            continue
        text = _item_text(item)
        if text and text not in pieces:
            pieces.append(text)
    return " ".join(pieces)


def _phrase_sentence_context(story: dict, start: float, end: float) -> str:
    """Expand overlapping timed phrases to their punctuation-bounded sentence."""
    phrases = sorted(
        story.get("phrases", []),
        key=lambda item: (float(item.get("start", 0)), float(item.get("end", 0))),
    )
    overlapping = [
        index for index, item in enumerate(phrases) if _overlap(start, end, item)
    ]
    if not overlapping:
        return ""
    left = overlapping[0]
    right = overlapping[-1] + 1
    while left > 0 and not re.search(r"[.!?][\"')\]]?$", _item_text(phrases[left - 1])):
        left -= 1
    while right < len(phrases) and not re.search(
        r"[.!?][\"')\]]?$",
        _item_text(phrases[right - 1]),
    ):
        right += 1
    return " ".join(
        text
        for text in (_item_text(item) for item in phrases[left:right])
        if text
    )


def _window_excerpt(story: dict, start: float, end: float) -> str:
    for collection in ("phrases", "sentences", "visual_beats"):
        text = _overlapping_text(story, collection, start, end)
        if text:
            return text
    return str(story.get("title", "")).strip()


def _window_text(story: dict, start: float, end: float) -> str:
    # Sentence records preserve the named subject when a timestamp begins midway
    # through a sentence. Older manifests without sentences are expanded from
    # phrases until sentence-ending punctuation instead.
    sentence_text = _overlapping_text(story, "sentences", start, end)
    if sentence_text:
        return sentence_text
    phrase_context = _phrase_sentence_context(story, start, end)
    if phrase_context:
        return phrase_context
    visual_text = _overlapping_text(story, "visual_beats", start, end)
    return visual_text or str(story.get("title", "")).strip()


def _phrase_key(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", normalize_search_text(value)))


def _entity_is_usable(value: str) -> bool:
    normalized = _phrase_key(value)
    if not normalized or normalized in ENTITY_NOISE:
        return False
    if normalized.isdigit() or len(normalized) < 3:
        return False
    return True


def _story_entities(story: dict) -> list[str]:
    entities: list[str] = []
    for record in [story, *story.get("visual_beats", [])]:
        for values in (record.get("named_entities") or {}).values():
            if not isinstance(values, list):
                continue
            for value in values:
                cleaned = str(value).strip()
                if _entity_is_usable(cleaned) and cleaned not in entities:
                    entities.append(cleaned)
    return entities


def _entities_in_text(entities: list[str], text: str) -> list[str]:
    normalized = f" {_phrase_key(text)} "
    return [
        entity
        for entity in entities
        if f" {_phrase_key(entity)} " in normalized
    ]


def _window_entities(story: dict, text: str, start: float = 0.0) -> list[str]:
    """Resolve explicit entities, then carry nearby context across split narration."""
    entities = _story_entities(story)
    direct = _entities_in_text(entities, text)
    if direct:
        return direct

    preceding: list[tuple[float, str]] = []
    for collection in ("visual_beats", "sentences", "phrases"):
        for item in story.get(collection, []):
            end = float(item.get("end", item.get("start", 0)))
            if end > start + 0.05:
                continue
            item_text = " ".join(
                str(item.get(field, ""))
                for field in ("text", "important_claim", "visual_query")
            )
            preceding.append((end, item_text))
    for _, prior_text in sorted(preceding, reverse=True):
        carried = _entities_in_text(entities, prior_text)
        if carried:
            return carried

    title_entities = _entities_in_text(entities, str(story.get("title", "")))
    if title_entities:
        return title_entities[:2]
    return entities[:1]


def infer_scene_intent(text: str) -> str:
    tokens = set(tokenize(text))
    rules = (
        ({"ceo", "founder", "executive", "person", "president"}, "person"),
        ({"phone", "mobile", "android", "iphone", "app"}, "mobile_device"),
        ({"rocket", "space", "satellite", "orbit"}, "space"),
        ({"car", "vehicle", "robotaxi", "factory", "ev"}, "vehicle_or_factory"),
        ({"server", "datacenter", "cloud", "compute"}, "data_center"),
        ({"chip", "processor", "gpu", "cpu", "semiconductor"}, "hardware"),
        ({"social", "instagram", "facebook", "tiktok"}, "social_media"),
        ({"game", "gaming", "steam", "xbox"}, "gaming"),
        ({"office", "headquarters", "building"}, "company_location"),
        ({"software", "linux", "model", "ai"}, "technology_concept"),
    )
    for keywords, intent in rules:
        if tokens & keywords:
            return intent
    return "neutral_technology_context"


def _effective_hero_images(
    storyboard_story: dict,
    maximum_hold_seconds: float,
) -> list[dict]:
    """Clamp hero holds and bridge only gaps that preserve the hard ceiling."""
    images = [
        {
            **image,
            "time_range": {
                **image["time_range"],
                "start": float(image["time_range"]["start"]),
                "end": float(image["time_range"]["end"]),
            },
        }
        for image in storyboard_story.get("images", [])
        if image.get("validation", {}).get("status") == "passed"
    ]
    images.sort(key=lambda item: float(item["time_range"]["start"]))
    for image in images:
        start = float(image["time_range"]["start"])
        end = float(image["time_range"]["end"])
        image["time_range"]["end"] = min(end, start + maximum_hold_seconds)
    for current, following in zip(images, images[1:]):
        current_start = float(current["time_range"]["start"])
        current_end = float(current["time_range"]["end"])
        following_start = float(following["time_range"]["start"])
        gap = following_start - current_end
        if (
            0 < gap < 2.0
            and following_start - current_start <= maximum_hold_seconds + 0.001
        ):
            current["time_range"]["end"] = following_start
            current["bridged_gap_seconds"] = round(gap, 6)
    return images


def build_archive_windows(
    story: dict,
    storyboard_story: dict,
    settings: MediaSelectionSettings,
) -> list[dict]:
    settings.validate()
    duration = float(story["narration"]["duration"])
    hero_ranges = [
        (
            float(image["time_range"]["start"]),
            float(image["time_range"]["end"]),
        )
        for image in _effective_hero_images(
            storyboard_story,
            settings.maximum_window_seconds,
        )
    ]
    if not hero_ranges:
        raise RuntimeError(
            f"{story.get('story_id')} needs at least one approved hero image range."
        )
    uncovered = subtract_intervals(duration, hero_ranges)
    windows: list[dict] = []
    for interval_start, interval_end in uncovered:
        boundaries = _boundaries_for_interval(story, interval_start, interval_end)
        for start, end in _split_uncovered_interval(
            interval_start,
            interval_end,
            boundaries,
            settings,
        ):
            text = _window_text(story, start, end)
            timestamp_excerpt = _window_excerpt(story, start, end)
            visual_queries = [
                str(item.get("visual_query", "")).strip()
                for item in story.get("visual_beats", [])
                if _overlap(start, end, item) and item.get("visual_query")
            ]
            entities = _window_entities(
                story,
                " ".join([text, *visual_queries]),
                start,
            )
            windows.append(
                {
                    "window_id": (
                        f"story_{int(story['order']):02d}_"
                        f"archive_{len(windows) + 1:03d}"
                    ),
                    "story_id": story["story_id"],
                    "story_order": int(story["order"]),
                    "start": round(start, 6),
                    "end": round(end, 6),
                    "duration": round(end - start, 6),
                    "timeline_start": round(
                        float(story["narration"]["start"]) + start,
                        6,
                    ),
                    "timeline_end": round(
                        float(story["narration"]["start"]) + end,
                        6,
                    ),
                    "text": text,
                    "timestamp_excerpt": timestamp_excerpt,
                    "entities": entities,
                    "scene_intent": infer_scene_intent(
                        " ".join([text, *entities, *visual_queries])
                    ),
                    "visual_queries": visual_queries,
                }
            )
    return windows


def coverage_quality(
    duration: float,
    hero_ranges: list[tuple[float, float]],
    archive_windows: list[dict],
) -> dict:
    all_intervals = [
        *hero_ranges,
        *[
            (float(window["start"]), float(window["end"]))
            for window in archive_windows
        ],
    ]
    merged = merge_intervals(all_intervals)
    gaps: list[dict] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor + 0.01:
            gaps.append({"start": round(cursor, 6), "end": round(start, 6)})
        cursor = max(cursor, end)
    if cursor < duration - 0.01:
        gaps.append({"start": round(cursor, 6), "end": round(duration, 6)})
    return {
        "status": "passed" if not gaps else "failed",
        "duration_seconds": round(duration, 6),
        "covered_seconds": round(
            sum(end - start for start, end in merged),
            6,
        ),
        "gaps": gaps,
    }


def _document_frequencies(assets: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for asset in assets:
        counts.update(set(asset.get("keywords", [])))
    return counts


def _query_tokens(window: dict) -> set[str]:
    text = " ".join(
        [
            window.get("text", ""),
            " ".join(window.get("entities", [])),
            window.get("scene_intent", ""),
            " ".join(window.get("visual_queries", [])),
        ]
    )
    return set(tokenize(text))


def _asset_score(
    asset: dict,
    *,
    query_tokens: set[str],
    entities: list[str],
    document_frequencies: Counter[str],
    asset_count: int,
    already_used_sha: set[str],
    previous_sha: str,
) -> tuple[float, dict]:
    asset_tokens = set(asset.get("keywords", []))
    overlap = query_tokens & asset_tokens
    meaningful_overlap = overlap - WEAK_CONTEXT_TOKENS
    semantic_score = sum(
        1.0 + math.log((asset_count + 1) / (document_frequencies[token] + 1))
        for token in meaningful_overlap
    )
    searchable_asset_text = _phrase_key(
        " ".join(
            [
                asset.get("workspace_relative_path", ""),
                asset.get("local_caption", ""),
                " ".join(asset.get("keywords", [])),
            ]
        )
    )
    entity_hits = [
        entity
        for entity in entities
        if (
            f" {_phrase_key(entity)} "
            in f" {searchable_asset_text} "
        )
    ]
    exact_entity_bonus = 5.0 * len(entity_hits)
    phrase_bonus = sum(
        1.5
        for token in meaningful_overlap
        if len(token) >= 5 and token in _phrase_key(asset.get("filename", ""))
    )
    retention = float(asset.get("crop_retention_16_9", 0))
    crop_score = max(0.0, retention) * 1.5
    resolution_score = min(
        1.0,
        (int(asset.get("width", 0)) * int(asset.get("height", 0)))
        / (1280 * 720),
    )
    flags = set(asset.get("quality_flags", []))
    quality_penalty = (
        2.5 * ("low_resolution" in flags)
        + 2.0 * ("crop_risk" in flags)
        + 0.8 * ("generic_filename" in flags)
        + 0.5 * ("likely_screenshot" in flags)
    )
    reuse_penalty = min(2.0, float(asset.get("prior_use_count", 0)) * 0.25)
    same_story_penalty = 6.0 if asset.get("sha256") in already_used_sha else 0.0
    immediate_penalty = 10.0 if previous_sha and asset.get("sha256") == previous_sha else 0.0
    license_bonus = (
        0.4
        if asset.get("license", {}).get("status")
        == "user_confirmed_no_copyright"
        else 0.0
    )
    score = (
        semantic_score * 2.4
        + exact_entity_bonus
        + phrase_bonus
        + crop_score
        + resolution_score
        + license_bonus
        - quality_penalty
        - reuse_penalty
        - same_story_penalty
        - immediate_penalty
    )
    return round(score, 6), {
        "overlap_tokens": sorted(overlap),
        "meaningful_overlap_tokens": sorted(meaningful_overlap),
        "entity_hits": entity_hits,
        "semantic_score": round(semantic_score, 6),
        "exact_entity_bonus": round(exact_entity_bonus, 6),
        "phrase_bonus": round(phrase_bonus, 6),
        "crop_score": round(crop_score, 6),
        "resolution_score": round(resolution_score, 6),
        "license_bonus": license_bonus,
        "quality_penalty": round(quality_penalty, 6),
        "reuse_penalty": round(reuse_penalty, 6),
        "same_story_penalty": same_story_penalty,
        "immediate_duplicate_penalty": immediate_penalty,
    }


def shortlist_assets(
    index: dict,
    window: dict,
    settings: MediaSelectionSettings,
    *,
    already_used_sha: set[str] | None = None,
    previous_sha: str = "",
    excluded_sha: set[str] | None = None,
) -> list[dict]:
    usable_assets = [
        item
        for item in index.get("assets", [])
        if Path(item.get("absolute_path", "")).is_file()
        and item.get("sha256")
        and _is_in_video_media_asset(item)
    ]
    if not usable_assets:
        # An empty curated repository is recoverable: the caller creates a
        # context-safe card, then the online fallback gets an opportunity to
        # replace it. Do not turn a missing local match into a stage failure.
        return []
    exclusions = excluded_sha or set()
    assets = [
        item for item in usable_assets if item.get("sha256") not in exclusions
    ]
    if not assets:
        return []
    query = _query_tokens(window)
    frequencies = _document_frequencies(assets)
    ranked = []
    for asset in assets:
        score, components = _asset_score(
            asset,
            query_tokens=query,
            entities=window.get("entities", []),
            document_frequencies=frequencies,
            asset_count=len(assets),
            already_used_sha=already_used_sha or set(),
            previous_sha=previous_sha,
        )
        ranked.append((score, asset, components))
    ranked.sort(
        key=lambda item: (
            item[0],
            float(item[1].get("crop_retention_16_9", 0)),
            -int(item[1].get("prior_use_count", 0)),
            normalize_search_text(item[1].get("workspace_relative_path", "")),
        ),
        reverse=True,
    )
    if ranked and ranked[0][0] < settings.minimum_local_score:
        neutral = next(
            (
                item
                for item in ranked
                if "crop_risk" not in item[1].get("quality_flags", [])
                and (
                    "_combined"
                    in normalize_search_text(
                        item[1].get("workspace_relative_path", "")
                    )
                    or "combined"
                    in normalize_search_text(item[1].get("folder", ""))
                )
            ),
            None,
        )
        if neutral is not None:
            ranked.remove(neutral)
            neutral[2]["neutral_fallback"] = True
            ranked.insert(0, neutral)
    result = []
    for score, asset, components in ranked[: settings.shortlist_size]:
        distinctive_overlap = [
            token
            for token in components.get("meaningful_overlap_tokens", [])
            if len(token) >= 3
        ]
        entity_hits = components.get("entity_hits", [])
        rare_specific_single = (
            len(distinctive_overlap) == 1
            and len(distinctive_overlap[0]) >= 6
            and frequencies[distinctive_overlap[0]] <= 2
        )
        context_grounded = (
            bool(entity_hits)
            or len(distinctive_overlap) >= 2
            or rare_specific_single
        )
        grounding_basis = (
            "exact_entity"
            if entity_hits
            else "distinctive_context"
            if context_grounded
            else "broad_or_weak_match"
        )
        result.append(
            {
                "asset_id": asset["asset_id"],
                "workspace_relative_path": asset["workspace_relative_path"],
                "filename": asset["filename"],
                "folder": asset["folder"],
                "local_caption": asset.get("local_caption", ""),
                "keywords": asset.get("keywords", []),
                "width": asset["width"],
                "height": asset["height"],
                "aspect_ratio": asset["aspect_ratio"],
                "crop_retention_16_9": asset["crop_retention_16_9"],
                "quality_flags": asset.get("quality_flags", []),
                "license_status": asset.get("license", {}).get("status", "unknown"),
                "prior_use_count": asset.get("prior_use_count", 0),
                "sha256": asset["sha256"],
                "local_score": score,
                "score_components": components,
                "context_grounded": context_grounded,
                "grounding_basis": grounding_basis,
                "distinctive_overlap_tokens": distinctive_overlap,
                "neutral_fallback": bool(
                    components.get("neutral_fallback", False)
                ),
            }
        )
    return result


def selection_prompt(story: dict, windows: list[dict]) -> str:
    compact = []
    for window in windows:
        compact.append(
            {
                "window_id": window["window_id"],
                "full_sentence_context": window["text"],
                "timestamp_excerpt": window.get(
                    "timestamp_excerpt",
                    window["text"],
                ),
                "time_range": {
                    "start": window["start"],
                    "end": window["end"],
                },
                "entities": window["entities"],
                "scene_intent": window["scene_intent"],
                "candidates": window["shortlist"],
            }
        )
    return f"""
Evaluate the archive candidates for every narration window in this technology-news
story. Select one only when it is clearly relevant to the exact spoken context.

Rules:
- Use `full_sentence_context` to resolve the company, person, product, and meaning;
  never interpret a mid-sentence `timestamp_excerpt` in isolation.
- You receive metadata, not image pixels. Never claim that a candidate visibly shows
  anything not explicitly supported by its filename, local_caption, and keywords.
- Match the precise spoken claim and entity, not merely a broad technology category.
- A shared generic verb such as block, use, control, change, delay, install, or launch
  is not contextual relevance by itself.
- `context_grounded=false` is a hard rejection: never select that candidate even if
  its broad category, local score, or filename appears superficially useful.
- Prefer `grounding_basis=exact_entity`. A distinctive-context match is acceptable
  only when the visible subject described by its metadata accurately represents the
  spoken claim without implying the wrong company, person, product, or event.
- confidence measures contextual relevance, not image quality. Use less than 0.45
  only when the candidate is effectively unrelated or visually misleading.
- Do not use the same image twice in this story unless no reasonable alternative exists.
- Never use the same image in adjacent windows.
- Prefer crop-safe 16:9 assets without low-resolution or crop-risk flags.
- The `user_confirmed_no_copyright` status is cleared for use.
- An `unknown` license is allowed for planning but must remain visibly recorded.
- If every candidate is unrelated, merely shares a broad company/category, or would
  visually misrepresent the spoken claim, return relevance_status exactly as
  `images_are_irrelevant` and selected_asset_id as null. Supply a concise Pexels
  photographic search idea and the central subject. The program will deliberately
  broaden exact brand/person names into a truthful visual topic before searching.
- Never force a local selection after deciding the images are irrelevant.
- For a normal choice, return relevance_status=`relevant` and choose only an asset_id
  listed in that window's candidates.
- Return exactly one decision for every window, in the same order.
- Alternatives may be used automatically when the first choice reaches its two-use
  limit. Include only genuinely relevant alternatives, in best-first order; never
  list a merely available or generic candidate.
- Alternatives must also come from that window's candidate list.

STORY
ID: {story['story_id']}
ORDER: {story['order']}
TITLE: {story.get('title', '')}

WINDOWS AND LOCAL SHORTLISTS
{json.dumps(compact, ensure_ascii=False, indent=2)}
""".strip()


def select_story_with_openai(
    story: dict,
    windows: list[dict],
    settings: MediaSelectionSettings,
) -> StoryMediaDecisions:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for AI media selection.")
    from openai import OpenAI

    client = OpenAI(timeout=settings.timeout_seconds)
    last_error: Exception | None = None
    for attempt in range(1, settings.api_retries + 1):
        try:
            response = client.responses.parse(
                model=settings.model,
                reasoning={"effort": settings.reasoning_effort},
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a meticulous archive-image editor. Base every "
                            "choice only on the supplied narration window and its "
                            "small local candidate list."
                        ),
                    },
                    {"role": "user", "content": selection_prompt(story, windows)},
                ],
                text_format=StoryMediaDecisions,
            )
            if response.output_parsed is None:
                raise RuntimeError("OpenAI returned no structured media decisions.")
            return response.output_parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            status = getattr(exc, "status_code", None)
            if (
                isinstance(status, int)
                and 400 <= status < 500
                and status not in {408, 409, 429}
            ):
                break
            if attempt < settings.api_retries:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"AI media selection failed: {last_error}") from last_error


def pexels_query_prompt(story: dict, windows: list[dict]) -> str:
    payload = [
        {
            "window_id": window["window_id"],
            "full_sentence_context": window.get("text", ""),
            "timed_excerpt": window.get("timestamp_excerpt", ""),
            "entities": window.get("entities", []),
            "scene_intent": window.get("scene_intent", ""),
        }
        for window in windows
    ]
    return f"""
Plan efficient Pexels photo-search queries for the unresolved visual windows in one
technology-news story. Pexels is a stock-photo library, not a general web search.

Rules:
- Use the complete story title and full_sentence_context. Never plan from an
  isolated clause when its subject appears earlier in the sentence or story.
- Cover every supplied window_id exactly once. Group adjacent windows only when one
  honest visual subject can represent all of them; split the group when the visible
  subject changes.
- primary_query must be a concise Pexels-ready phrase of roughly three to seven
  concrete visible nouns/adjectives. Preserve a famous product or company name when
  it is visually central, then add visible context.
- Provide up to two genuinely different alternative queries, moving from exact to
  descriptive. Example: Google Earth satellite map computer screen; then digital
  globe satellite imagery map. Do not collapse Google Earth into generic AI or
  generic technology.
- Queries must describe what a useful photograph should visibly show. Avoid claims,
  dates, statistics, article prose, unsupported people, abstract verbs, and words
  such as news, report, controversy, allegation, or Wikimedia.
- visual_subject is a short grounding phrase shared by the queries. It must contain
  recognizable visible terms likely to appear in Pexels alt text.
- Slight contextual relevance is acceptable; totally unrelated imagery is not.
- Keep rationale brief. Return only the structured plan.

STORY TITLE:
{story.get('title', '')}

UNRESOLVED WINDOWS:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()


def plan_pexels_queries_with_openai(
    story: dict,
    windows: list[dict],
    settings: MediaSelectionSettings,
) -> StoryPexelsQueryPlan:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for Pexels query planning.")
    from openai import OpenAI

    client = OpenAI(timeout=settings.timeout_seconds)
    last_error: Exception | None = None
    for attempt in range(1, settings.api_retries + 1):
        try:
            response = client.responses.parse(
                model=settings.pexels_query_model,
                reasoning={"effort": settings.pexels_query_reasoning_effort},
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You plan precise, economical stock-photo searches. "
                            "Preserve named visual context and never invent subjects."
                        ),
                    },
                    {"role": "user", "content": pexels_query_prompt(story, windows)},
                ],
                text_format=StoryPexelsQueryPlan,
            )
            if response.output_parsed is None:
                raise RuntimeError("OpenAI returned no Pexels query plan.")
            return response.output_parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            status = getattr(exc, "status_code", None)
            if (
                isinstance(status, int)
                and 400 <= status < 500
                and status not in {408, 409, 429}
            ):
                break
            if attempt < settings.api_retries:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"Pexels query planning failed: {last_error}") from last_error


def local_story_selector(
    story: dict,
    windows: list[dict],
    settings: MediaSelectionSettings,
) -> StoryMediaDecisions:
    del story, settings
    decisions = []
    used_sha: set[str] = set()
    previous_sha = ""
    for window in windows:
        chosen = next(
            (
                candidate
                for candidate in window["shortlist"]
                if candidate["sha256"] != previous_sha
                and candidate["sha256"] not in used_sha
            ),
            window["shortlist"][0],
        )
        alternatives = [
            candidate["asset_id"]
            for candidate in window["shortlist"]
            if candidate["asset_id"] != chosen["asset_id"]
        ][:3]
        decisions.append(
            WindowDecision(
                window_id=window["window_id"],
                relevance_status="relevant",
                selected_asset_id=chosen["asset_id"],
                confidence=min(0.95, max(0.2, 0.45 + chosen["local_score"] / 20)),
                selection_reason=(
                    "Offline deterministic choice using entity, filename, crop, "
                    "quality, and reuse scores."
                ),
                alternative_asset_ids=alternatives,
            )
        )
        used_sha.add(chosen["sha256"])
        previous_sha = chosen["sha256"]
    return StoryMediaDecisions(decisions=decisions)


def _ground_irrelevant_decision(
    decision: WindowDecision,
    window: dict,
    story: dict,
) -> WindowDecision:
    proposed = _phrase_key(decision.required_entity)
    title = str(story.get("title", ""))
    visible_scope = _phrase_key(
        " ".join([title, window.get("text", ""), *window.get("entities", [])])
    )
    if proposed and proposed in visible_scope:
        return decision

    entities = [*window.get("entities", []), *_story_entities(story)]
    title_normalized = _phrase_key(title)
    proposed_tokens = set(proposed.split())
    query_tokens = set(normalize_search_text(decision.online_search_query).split())
    ranked = []
    for index, entity in enumerate(dict.fromkeys(entities)):
        normalized = _phrase_key(entity)
        if not normalized:
            continue
        tokens = set(normalized.split())
        overlap = len(tokens & (proposed_tokens | query_tokens))
        ranked.append(
            (
                normalized in title_normalized,
                overlap,
                len(tokens) > 1,
                -index,
                entity,
            )
        )
    if ranked:
        grounded = max(ranked)[-1]
        decision.required_entity = grounded
        decision.online_search_query = (
            f"{grounded} {window.get('scene_intent', 'technology news')}"
        )[:180]
        decision.selection_reason = (
            decision.selection_reason.rstrip()
            + " Required entity was grounded locally to the story context."
        )[:700]
        return decision

    return _fallback_to_local_candidate(
        decision,
        window,
        "The suggested online entity was not grounded in the story context.",
    )


def _validate_decisions(
    decisions: StoryMediaDecisions,
    windows: list[dict],
    story: dict,
    minimum_confidence: float = 0.0,
) -> dict[str, WindowDecision]:
    supplied: dict[str, WindowDecision] = {}
    for decision in decisions.decisions:
        supplied.setdefault(decision.window_id, decision)
    result = {}
    for window in windows:
        decision = supplied.get(window["window_id"])
        if decision is None:
            decision = _fallback_to_local_candidate(
                WindowDecision(
                    window_id=window["window_id"],
                    relevance_status="relevant",
                    selected_asset_id=window["shortlist"][0]["asset_id"],
                    confidence=0.1,
                    selection_reason=(
                        "The AI selector omitted this narration window entirely."
                    ),
                ),
                window,
                "The AI selector omitted this narration window entirely.",
            )
        allowed = {
            candidate["asset_id"]
            for candidate in window["shortlist"]
        }
        if (
            decision.relevance_status == "relevant"
            and decision.selected_asset_id not in allowed
        ):
            decision = _fallback_to_local_candidate(
                decision,
                window,
                "The AI returned an asset from outside this window's shortlist.",
            )
        selected_candidate = next(
            (
                candidate
                for candidate in window["shortlist"]
                if candidate["asset_id"] == decision.selected_asset_id
            ),
            None,
        )
        if (
            decision.relevance_status == "relevant"
            and selected_candidate is not None
            and (
                not selected_candidate.get("context_grounded", True)
                or decision.confidence < minimum_confidence
            )
        ):
            cause = (
                "The local candidate matched only broad or weak context."
                if not selected_candidate.get("context_grounded", True)
                else (
                    f"The AI relevance confidence {decision.confidence:.2f} was below "
                    f"the required {minimum_confidence:.2f}."
                )
            )
            decision = WindowDecision(
                window_id=window["window_id"],
                relevance_status="images_are_irrelevant",
                confidence=decision.confidence,
                selection_reason=(
                    f"{cause} Use a precisely grounded online fallback instead."
                ),
                online_search_query=" ".join(
                    [
                        *window.get("entities", []),
                        *window.get("visual_queries", []),
                    ]
                )[:180],
                required_entity=(window.get("entities") or [""])[0],
            )
        decision.alternative_asset_ids = [
            asset_id
            for asset_id in decision.alternative_asset_ids
            if asset_id in allowed
            and any(
                candidate["asset_id"] == asset_id
                and (
                    candidate.get("context_grounded", True)
                    or candidate.get("neutral_fallback", False)
                )
                for candidate in window["shortlist"]
            )
        ][:3]
        if decision.relevance_status == "images_are_irrelevant":
            strongest_local = window["shortlist"][0]
            entity_hits = strongest_local.get("score_components", {}).get(
                "entity_hits",
                [],
            )
            is_strong_named_match = bool(
                strongest_local.get("grounding_basis") == "exact_entity"
                and float(strongest_local.get("local_score", 0)) >= 30
                and any(len(tokenize(str(entity))) >= 2 for entity in entity_hits)
            )
            if is_strong_named_match:
                decision = _fallback_to_local_candidate(
                    decision,
                    window,
                    (
                        "The top curated local asset has a strong multi-word "
                        "entity match, so online fallback is unnecessary."
                    ),
                )
            else:
                decision = _ground_irrelevant_decision(decision, window, story)
        result[decision.window_id] = decision
    return result


def _fallback_to_local_candidate(
    decision: WindowDecision,
    window: dict,
    reason: str,
) -> WindowDecision:
    grounded = [
        item
        for item in window["shortlist"]
        if item.get("context_grounded", True)
    ]
    neutral = [
        item
        for item in window["shortlist"]
        if item.get("neutral_fallback", False)
    ]
    eligible = grounded or neutral
    if not eligible:
        return WindowDecision(
            window_id=window["window_id"],
            relevance_status="images_are_irrelevant",
            confidence=0.05,
            selection_reason=(
                "Automatic recovery rejected every local candidate because none "
                f"passed contextual grounding. {reason}"
            )[:700],
            online_search_query=" ".join(
                [
                    *window.get("entities", []),
                    *window.get("visual_queries", []),
                ]
            )[:180],
            required_entity=(window.get("entities") or [""])[0],
        )
    chosen = eligible[0]
    grounding_basis = str(chosen.get("grounding_basis", ""))
    local_score = float(chosen.get("local_score", 0))
    recovery_confidence = (
        0.72
        if grounding_basis == "exact_entity" and local_score >= 5
        else 0.52
        if grounding_basis == "distinctive_context" and local_score >= 12
        else 0.15
    )
    alternatives = [
        item["asset_id"]
        for item in eligible[1:4]
    ]
    return WindowDecision(
        window_id=window["window_id"],
        relevance_status="relevant",
        selected_asset_id=chosen["asset_id"],
        confidence=recovery_confidence,
        selection_reason=(
            "Automatic recovery selected the strongest valid candidate from this "
            f"window's local shortlist. {reason}"
        )[:700],
        alternative_asset_ids=alternatives,
    )


PEXELS_VISUAL_CONCEPTS = (
    (
        "data_center",
        "data center servers technology",
        "data center",
        {"data center", "server", "servers", "cloud infrastructure", "outage"},
    ),
    (
        "smart_television",
        "smart television streaming entertainment",
        "smart television",
        {"television", "smart tv", "streaming", "tv app", "samsung tv"},
    ),
    (
        "cryptocurrency",
        "bitcoin cryptocurrency digital wallet",
        "cryptocurrency",
        {"bitcoin", "crypto", "cryptocurrency", "wallet", "blockchain", "coldcard"},
    ),
    (
        "writing_publishing",
        "writer books publishing desk",
        "writer books",
        {"writer", "author", "book", "novel", "publishing", "copyright", "scalzi"},
    ),
    (
        "cybersecurity",
        "cybersecurity computer security operations",
        "cybersecurity",
        {
            "cybersecurity", "security", "hacker", "hacking", "malware",
            "breach", "attack", "vulnerability", "firmware", "exploit",
        },
    ),
    (
        "privacy_data",
        "digital privacy personal data protection",
        "digital privacy",
        {"privacy", "personal data", "surveillance", "tracking", "face data"},
    ),
    (
        "artificial_intelligence",
        "artificial intelligence computer research",
        "artificial intelligence",
        {
            "artificial intelligence", " ai ", "model", "training", "agent",
            "machine learning", "neural", "llm", "chatbot",
        },
    ),
    (
        "computer_chips",
        "computer chips semiconductor circuit board",
        "computer chips",
        {"gpu", "cpu", "chip", "semiconductor", "nvidia", "amd", "intel"},
    ),
    (
        "gaming",
        "computer gaming esports setup",
        "computer gaming",
        {"gaming", "game", "xbox", "playstation", "steam", "esports"},
    ),
    (
        "autonomous_vehicle",
        "autonomous electric car technology",
        "autonomous car",
        {"waymo", "autonomous car", "self driving", "electric vehicle", "robotaxi"},
    ),
    (
        "social_media",
        "social media smartphone apps",
        "social media",
        {"social media", "facebook", "instagram", "tiktok", "online platform"},
    ),
    (
        "satellite_network",
        "satellite communications network technology",
        "satellite network",
        {"satellite", "starlink", "space internet", "communications network"},
    ),
    (
        "software_workplace",
        "computer software office workplace",
        "computer software",
        {
            "microsoft", "windows", "google", "apple", "openai", "software",
            "operating system", "office", "browser", "application", "app",
        },
    ),
)


def _pexels_visual_concept(
    window: dict,
    decision: WindowDecision,
) -> tuple[str, str, str]:
    """Map an exact news claim to an honest, searchable photographic topic."""
    source = " " + normalize_search_text(
        " ".join(
            [
                str(window.get("text", "")),
                str(window.get("timestamp_excerpt", "")),
                str(window.get("scene_intent", "")),
                " ".join(str(item) for item in window.get("entities", [])),
                decision.online_search_query,
                decision.required_entity,
            ]
        )
    ) + " "
    best: tuple[int, int, str, str, str] | None = None
    for order, (key, query, entity, phrases) in enumerate(PEXELS_VISUAL_CONCEPTS):
        hits = sum(
            1
            for phrase in phrases
            if (
                f" {normalize_search_text(phrase)} " in source
                or (
                    len(tokenize(phrase)) == 1
                    and tokenize(phrase)[0] in set(tokenize(source))
                )
            )
        )
        candidate = (hits, -order, key, query, entity)
        if hits and (best is None or candidate > best):
            best = candidate
    if best is not None:
        return best[2], best[3], best[4]
    return (
        "general_technology",
        "modern computer technology workspace",
        "computer technology",
    )


def _pexels_asset_matches(asset: dict, query: str, entity: str) -> bool:
    source_url = normalize_search_text(str(asset.get("source_url", "")))
    if "pexels.com" not in source_url:
        return False
    provider = asset.get("provider_metadata", {})
    visible_source = (
        " ".join(
            [
                str(provider.get("title", "")),
                str(provider.get("description", "")),
            ]
        )
        if provider
        else " ".join(
            [
                str(asset.get("filename", "")),
                str(asset.get("local_caption", "")),
                " ".join(str(item) for item in asset.get("keywords", [])),
            ]
        )
    )
    visible_tokens = set(
        tokenize(
            visible_source
        )
    )
    ignored = {
        "modern", "technology", "computer", "digital", "photo", "image",
        "screen", "online", "retrieved", "pexels", "interface", "concept",
    }
    terms = {
        token
        for token in tokenize(f"{query} {entity}")
        if token not in ignored and len(token) >= 3
    }
    visible_overlap = visible_tokens.intersection(terms)
    provenance_tokens = set(
        tokenize(
            " ".join(
                [
                    str(provider.get("query", "")),
                    str(provider.get("required_entity", "")),
                ]
            )
        )
    )
    minimum_visible_hits = min(2, max(1, len(terms)))
    if provenance_tokens:
        # A prior download is reusable only when both its original search purpose
        # and its visible description overlap this new visual subject. This keeps
        # a cryptocurrency or television photo out of a Google Earth chapter.
        return bool(provenance_tokens.intersection(terms)) and (
            len(visible_overlap) >= minimum_visible_hits
        )
    # Backward-compatible path for old manually indexed Pexels files without a
    # sidecar query. Require a stronger visible match than a single generic word.
    return len(visible_overlap) >= minimum_visible_hits


def _pexels_candidate_matches_subject(
    candidate: dict,
    query: str,
    entity: str,
) -> bool:
    """Reject API results grounded only by a generic word or company name."""
    ignored = {
        "modern", "technology", "computer", "digital", "photo", "image",
        "screen", "online", "interface", "concept",
    }
    subject_terms = {
        token
        for token in tokenize(f"{query} {entity}")
        if token not in ignored and len(token) >= 3
    }
    visible_terms = set(
        tokenize(
            " ".join(
                [
                    str(candidate.get("title", "")),
                    str(candidate.get("description", "")),
                    str(candidate.get("source_page", "")),
                ]
            )
        )
    )
    required_hits = min(2, max(1, len(subject_terms)))
    return len(subject_terms.intersection(visible_terms)) >= required_hits


def _pexels_candidate(asset: dict, *, query: str, concept: str) -> dict:
    return {
        "asset_id": asset["asset_id"],
        "workspace_relative_path": asset.get("workspace_relative_path", ""),
        "filename": asset.get("filename", ""),
        "folder": asset.get("folder", str(Path(asset["absolute_path"]).parent)),
        "local_caption": asset.get("local_caption", ""),
        "keywords": asset.get("keywords", []),
        "width": asset["width"],
        "height": asset["height"],
        "aspect_ratio": asset["aspect_ratio"],
        "crop_retention_16_9": asset.get("crop_retention_16_9", 1.0),
        "quality_flags": asset.get("quality_flags", []),
        "license_status": asset.get("license", {}).get("status", "pexels_license"),
        "prior_use_count": asset.get("prior_use_count", 0),
        "sha256": asset["sha256"],
        "local_score": 20.0,
        "score_components": {
            "pexels_topic_query": query,
            "pexels_visual_concept": concept,
        },
        "context_grounded": True,
        "grounding_basis": "pexels_topic_concept",
        "distinctive_overlap_tokens": tokenize(concept),
        "neutral_fallback": False,
        "online_fallback": True,
        "online_provider": "pexels",
    }


def _keep_irrelevant(decision: WindowDecision, reason: str) -> WindowDecision:
    return WindowDecision(
        window_id=decision.window_id,
        relevance_status="images_are_irrelevant",
        confidence=decision.confidence,
        selection_reason=(
            decision.selection_reason.rstrip() + " " + reason.strip()
        )[:700],
        online_search_query=decision.online_search_query,
        required_entity=decision.required_entity,
    )


def resolve_pexels_fallbacks(
    *,
    video_number: int,
    windows: list[dict],
    decisions: dict[str, WindowDecision],
    media_index: dict,
    index_settings: MediaIndexSettings,
    settings: MediaSelectionSettings,
    remaining_downloads: int,
    remaining_searches: int,
    video_usage_counts: Counter[str] | None = None,
    search_cache: dict[str, dict] | None = None,
    query_plan: StoryPexelsQueryPlan | None = None,
) -> tuple[dict[str, WindowDecision], dict, int, int]:
    """Fill unresolved windows from reusable, topic-grounded Pexels pools.

    Local files have already been selected before this function runs. Exact names
    are deliberately converted to broad photographic concepts so a brand mention
    cannot become an unrelated rocket, person, or product image.
    """
    updated = dict(decisions)
    unresolved = []
    projected_usage = Counter(video_usage_counts or {})
    projected_previous_sha = ""
    for window in windows:
        current = decisions[window["window_id"]]
        selected = next(
            (
                candidate
                for candidate in window.get("shortlist", [])
                if candidate.get("asset_id") == current.selected_asset_id
            ),
            None,
        )
        selected_is_neutral = bool(
            selected
            and (
                selected.get("neutral_fallback", False)
                or selected.get("context_safe_fallback", False)
                or selected.get("grounding_basis") == "context_safe_neutral"
            )
        )
        selected_sha = str(selected.get("sha256", "")) if selected else ""
        selected_is_conflicted = bool(
            selected_sha
            and (
                projected_usage[selected_sha]
                >= settings.maximum_uses_per_video
                or selected_sha == projected_previous_sha
            )
        )
        selected_is_missing = bool(
            current.relevance_status == "relevant" and selected is None
        )
        if (
            current.relevance_status == "images_are_irrelevant"
            or selected_is_neutral
            or selected_is_conflicted
            or selected_is_missing
        ):
            unresolved.append(window)
            if current.relevance_status != "images_are_irrelevant":
                updated[window["window_id"]] = WindowDecision(
                    window_id=window["window_id"],
                    relevance_status="images_are_irrelevant",
                    confidence=min(0.2, current.confidence),
                    selection_reason=(
                        "The current local choice is neutral, missing, adjacent to "
                        "identical pixels, or beyond the two-use cap; try the "
                        "Pexels topic pool before local recovery."
                    ),
                    online_search_query=current.online_search_query,
                    required_entity=current.required_entity,
                )
        elif selected_sha:
            projected_usage[selected_sha] += 1
            projected_previous_sha = selected_sha
    if not unresolved or not settings.pexels_fallback_enabled:
        return updated, media_index, 0, 0

    from pexels_media_stage import (
        PexelsMediaSettings,
        download_candidate,
        save_search_manifest,
        search_pexels,
    )

    online_settings = PexelsMediaSettings.from_environment()
    try:
        online_settings.validate()
    except ValueError as exc:
        for window in unresolved:
            current = updated[window["window_id"]]
            updated[window["window_id"]] = _keep_irrelevant(
                current,
                f"Pexels is unavailable: {exc}",
            )
        return updated, media_index, 0, 0

    story_context = {
        "text": " ".join(str(window.get("text", "")) for window in windows),
        "timestamp_excerpt": "",
        "scene_intent": " ".join(
            str(window.get("scene_intent", "")) for window in windows
        ),
        "entities": list(
            dict.fromkeys(
                entity
                for window in windows
                for entity in window.get("entities", [])
            )
        ),
    }
    story_context_decision = WindowDecision(
        window_id="story_context",
        relevance_status="images_are_irrelevant",
        confidence=0.5,
        selection_reason="Use the complete story to carry context into short clauses.",
        online_search_query=" ".join(
            updated[window["window_id"]].online_search_query
            for window in unresolved
        )[:180],
        required_entity="",
    )
    story_concept = _pexels_visual_concept(
        story_context,
        story_context_decision,
    )

    groups: dict[str, dict] = {}
    window_groups: dict[str, str] = {}
    unresolved_by_id = {window["window_id"]: window for window in unresolved}
    if query_plan is not None:
        claimed: set[str] = set()
        for planned_group in query_plan.groups:
            planned_windows = [
                unresolved_by_id[window_id]
                for window_id in planned_group.window_ids
                if window_id in unresolved_by_id and window_id not in claimed
            ]
            if not planned_windows:
                continue
            key = "ai_" + re.sub(
                r"[^a-z0-9]+",
                "_",
                normalize_search_text(planned_group.group_id),
            ).strip("_")
            key = key or f"ai_group_{len(groups) + 1}"
            while key in groups:
                key += "_next"
            queries = list(
                dict.fromkeys(
                    query.strip()
                    for query in [
                        planned_group.primary_query,
                        *planned_group.alternative_queries,
                    ]
                    if query.strip()
                )
            )
            groups[key] = {
                "query": queries[0],
                "queries": queries,
                "entity": planned_group.visual_subject,
                "windows": planned_windows,
                "planner": "luna_low",
            }
            for window in planned_windows:
                claimed.add(window["window_id"])
                window_groups[window["window_id"]] = key

    for window in unresolved:
        if window["window_id"] in window_groups:
            continue
        decision = updated[window["window_id"]]
        key, query, entity = _pexels_visual_concept(window, decision)
        if key == "general_technology" and story_concept[0] != key:
            key, query, entity = story_concept
        group = groups.setdefault(
            key,
            {
                "query": query,
                "queries": [query],
                "entity": entity,
                "windows": [],
                "planner": "deterministic_fallback",
            },
        )
        group["windows"].append(window)
        window_groups[window["window_id"]] = key

    downloads_used = 0
    searches_used = 0
    query_cache = search_cache if search_cache is not None else {}
    usage = Counter(video_usage_counts or {})
    # Reserve candidates already chosen by the local/AI phase in this story.
    # Without this preview of future enforcement, a Pexels pool can allocate the
    # same checksum to an unresolved window and only discover the two-use
    # collision later, when local recovery is the sole remaining choice.
    unresolved_ids = {window["window_id"] for window in unresolved}
    for window in windows:
        if window["window_id"] in unresolved_ids:
            continue
        selected_id = updated[window["window_id"]].selected_asset_id
        selected = next(
            (
                candidate
                for candidate in window.get("shortlist", [])
                if candidate.get("asset_id") == selected_id
            ),
            None,
        )
        if selected and selected.get("sha256"):
            usage[selected["sha256"]] += 1
    group_assets: dict[str, list[dict]] = {}
    downloaded_by_group: dict[str, list[str]] = {}
    throttled = False

    for key, group in groups.items():
        query = group["query"]
        entity = group["entity"]
        reusable = [
            asset
            for asset in media_index.get("assets", [])
            if _is_in_video_media_asset(asset)
            and Path(str(asset.get("absolute_path", ""))).is_file()
            and any(
                _pexels_asset_matches(asset, candidate_query, entity)
                for candidate_query in group.get("queries", [query])
            )
            and usage[str(asset.get("sha256", ""))]
            < settings.maximum_uses_per_video
        ]
        reusable.sort(
            key=lambda asset: (
                usage[str(asset.get("sha256", ""))],
                -float(asset.get("crop_retention_16_9", 0)),
                -int(asset.get("width", 0)) * int(asset.get("height", 0)),
            )
        )
        group_assets[key] = reusable
        capacity = sum(
            settings.maximum_uses_per_video - usage[asset["sha256"]]
            for asset in reusable
        )
        needed_capacity = len(group["windows"])
        capacity_shortfall = max(
            0,
            math.ceil((needed_capacity - capacity) / settings.maximum_uses_per_video),
        )
        # Keep one extra distinct photo as an adjacency buffer. A pool can have
        # enough raw two-use capacity while its only available checksum is also
        # the image immediately before a short unresolved clause.
        topic_window_count = (
            len(windows)
            if key == story_concept[0]
            else needed_capacity
        )
        target_unique = math.ceil(
            topic_window_count / settings.maximum_uses_per_video
        ) + 1
        uniqueness_shortfall = max(0, target_unique - len(reusable))
        needed_unique = max(capacity_shortfall, uniqueness_shortfall)
        if needed_unique <= 0 or throttled:
            continue
        try:
            known_photo_ids = {
                match.group(1)
                for asset in reusable
                for match in [re.search(r"_([0-9]+)\.[a-z0-9]+$", asset.get("filename", ""))]
                if match
            }
            combined_context = " ".join(
                str(item.get("text", "")) for item in group["windows"][:4]
            )
            for active_query in group.get("queries", [query]):
                if needed_unique <= 0 or downloads_used >= remaining_downloads:
                    break
                cache_key = normalize_search_text(active_query)
                search = query_cache.get(cache_key)
                if search is None:
                    if searches_used >= remaining_searches:
                        break
                    if online_settings.request_interval_seconds:
                        time.sleep(online_settings.request_interval_seconds)
                    search = search_pexels(
                        query=active_query,
                        required_entity=entity,
                        context=combined_context,
                        settings=online_settings,
                    )
                    query_cache[cache_key] = search
                    searches_used += 1
                    save_search_manifest(search, video_number=video_number)
                for candidate in search.get("candidates", []):
                    if needed_unique <= 0 or downloads_used >= remaining_downloads:
                        break
                    if not _pexels_candidate_matches_subject(
                        candidate,
                        active_query,
                        entity,
                    ):
                        continue
                    photo_id = str(candidate.get("photo_id", ""))
                    if not photo_id or photo_id in known_photo_ids:
                        continue
                    try:
                        downloaded = download_candidate(
                            candidate,
                            query=active_query,
                            required_entity=entity,
                            context=combined_context,
                            settings=online_settings,
                        )
                    except Exception:
                        continue
                    downloaded_by_group.setdefault(key, []).append(
                        downloaded["sha256"]
                    )
                    known_photo_ids.add(photo_id)
                    downloads_used += 1
                    needed_unique -= 1
        except Exception as exc:  # noqa: BLE001
            if "429" in str(exc) or "too many requests" in str(exc).casefold():
                throttled = True
            for window in group["windows"]:
                current = updated[window["window_id"]]
                updated[window["window_id"]] = _keep_irrelevant(
                    current,
                    f"Pexels topic pool error: {exc}",
                )

    refreshed = (
        load_media_index(index_settings, rescan=True)
        if downloaded_by_group
        else media_index
    )
    assets_by_sha = {
        asset.get("sha256"): asset
        for asset in refreshed.get("assets", [])
        if asset.get("sha256")
        and _is_in_video_media_asset(asset)
        and Path(str(asset.get("absolute_path", ""))).is_file()
    }
    for key, checksums in downloaded_by_group.items():
        for checksum in checksums:
            asset = assets_by_sha.get(checksum)
            group = groups[key]
            if (
                asset is not None
                and any(
                    _pexels_asset_matches(
                        asset,
                        candidate_query,
                        group["entity"],
                    )
                    for candidate_query in group.get(
                        "queries",
                        [group["query"]],
                    )
                )
                and all(
                existing.get("sha256") != checksum
                for existing in group_assets[key]
                )
            ):
                group_assets[key].append(asset)

    allocation_usage = Counter(usage)
    previous_sha = ""
    unresolved_by_id = {window["window_id"]: window for window in unresolved}
    for window in windows:
        window_id = window["window_id"]
        if window_id not in unresolved_by_id:
            selected_id = updated[window_id].selected_asset_id
            selected = next(
                (
                    candidate
                    for candidate in window.get("shortlist", [])
                    if candidate.get("asset_id") == selected_id
                ),
                None,
            )
            previous_sha = str(selected.get("sha256", "")) if selected else ""
            continue
        key = window_groups[window_id]
        group = groups[key]
        query = group["query"]
        entity = group["entity"]
        available = [
            asset
            for asset in group_assets.get(key, [])
            if allocation_usage[asset["sha256"]]
            < settings.maximum_uses_per_video
            and asset["sha256"] != previous_sha
        ]
        if not available:
            continue
        available.sort(
            key=lambda asset: (
                allocation_usage[asset["sha256"]],
                -float(asset.get("crop_retention_16_9", 0)),
                -int(asset.get("width", 0)) * int(asset.get("height", 0)),
            )
        )
        selected_asset = available[0]
        allocation_usage[selected_asset["sha256"]] += 1
        previous_sha = selected_asset["sha256"]
        candidates = [
            _pexels_candidate(asset, query=query, concept=entity)
            for asset in available
        ]
        window["shortlist"] = [
            *candidates,
            *[
                candidate
                for candidate in window.get("shortlist", [])
                if candidate.get("asset_id")
                not in {item["asset_id"] for item in candidates}
            ],
        ][: settings.shortlist_size]
        current = updated[window_id]
        updated[window_id] = WindowDecision(
            window_id=window_id,
            relevance_status="relevant",
            selected_asset_id=selected_asset["asset_id"],
            confidence=max(0.65, min(0.88, current.confidence)),
            selection_reason=(
                "Every local candidate was rejected. Selected a Pexels photo "
                f"from the grounded {entity} topic pool; exact names were not "
                "used as literal visual claims."
            ),
            alternative_asset_ids=[
                asset["asset_id"] for asset in available[1:4]
            ],
            online_search_query=query,
            required_entity=entity,
        )
    return updated, refreshed, downloads_used, searches_used
def _create_context_safe_fallback_asset(video_number: int, window: dict) -> dict:
    """Create a visibly designed context graphic when no honest image exists."""
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required for the context-safe image fallback.")
    output_dir = project_dir(video_number) / "05_visuals" / "safe_fallbacks"
    output_dir.mkdir(parents=True, exist_ok=True)
    variant = max(0, int(window.get("_context_safe_variant", 0)))
    variant_suffix = f"_v{variant:02d}" if variant else ""
    output = output_dir / (
        f"{window['window_id']}_context_graphic{variant_suffix}.png"
    )
    identity = "|".join(
        [
            window["window_id"],
            window.get("text", ""),
            " ".join(window.get("entities", [])),
            f"variant={variant}",
        ]
    )
    digest = sha256_text(identity)
    # Dark, restrained, and unique per window. A grid and layered panels prevent
    # this last-resort asset from appearing as a blank frame. The persistent
    # chapter title supplies context without inventing a person, logo, or event.
    red = 14 + int(digest[0:2], 16) % 18
    green = 25 + int(digest[2:4], 16) % 22
    blue = 42 + int(digest[4:6], 16) % 30
    color = f"0x{red:02x}{green:02x}{blue:02x}"
    accent_red = 28 + int(digest[6:8], 16) % 42
    accent_green = 62 + int(digest[8:10], 16) % 58
    accent_blue = 92 + int(digest[10:12], 16) % 72
    accent = f"0x{accent_red:02x}{accent_green:02x}{accent_blue:02x}"
    if not output.is_file():
        visual_filter = (
            f"color=c={color}:s=1280x720:d=0.04,"
            "drawgrid=w=80:h=80:t=1:c=white@0.10,"
            "drawbox=x=90:y=80:w=1100:h=560:t=4:c=white@0.16,"
            f"drawbox=x=155:y=145:w=970:h=430:t=fill:c={accent}@0.16,"
            "drawbox=x=210:y=205:w=860:h=12:t=fill:c=white@0.22,"
            "drawbox=x=210:y=250:w=620:h=8:t=fill:c=white@0.15,"
            "drawbox=x=210:y=500:w=360:h=8:t=fill:c=white@0.15,"
            "vignette=PI/5"
        )
        completed = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                visual_filter,
                "-frames:v",
                "1",
                "-y",
                str(output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or not output.is_file():
            raise RuntimeError(
                "Context-safe fallback rendering failed: "
                + (completed.stderr.strip() or window["window_id"])
            )
    checksum = sha256_file(output)
    return {
        "asset_id": f"safe_fallback_{checksum[:16]}",
        "absolute_path": str(output),
        "workspace_relative_path": str(output),
        "filename": output.name,
        "sha256": checksum,
        "width": 1280,
        "height": 720,
        "aspect_ratio": round(16 / 9, 6),
        "crop_retention_16_9": 1.0,
        "source_url": "",
        "creator": "Tech Reports local renderer",
        "license": {
            "status": "user_confirmed_no_copyright",
            "name": "Locally generated",
            "url": "",
            "attribution_required": False,
        },
        "quality_flags": [],
        "context_safe_fallback": True,
        "context_graphic": True,
    }


def _context_safe_candidate(asset: dict) -> dict:
    return {
        "asset_id": asset["asset_id"],
        "workspace_relative_path": asset["workspace_relative_path"],
        "filename": asset["filename"],
        "folder": str(Path(asset["absolute_path"]).parent),
        "local_caption": "Designed context-safe technology graphic",
        "keywords": ["designed", "context", "technology", "graphic"],
        "width": asset["width"],
        "height": asset["height"],
        "aspect_ratio": asset["aspect_ratio"],
        "crop_retention_16_9": 1.0,
        "quality_flags": [],
        "license_status": "user_confirmed_no_copyright",
        "prior_use_count": 0,
        "sha256": asset["sha256"],
        "local_score": 0.0,
        "score_components": {},
        "context_grounded": False,
        "grounding_basis": "context_safe_neutral",
        "distinctive_overlap_tokens": [],
        "neutral_fallback": True,
        "context_safe_fallback": True,
        "context_graphic": True,
    }


def _resolve_context_safe_fallbacks(
    video_number: int,
    windows: list[dict],
    decisions: dict[str, WindowDecision],
    *,
    video_usage_counts: Counter[str] | None = None,
    maximum_uses_per_video: int = 2,
) -> dict[str, WindowDecision]:
    updated = dict(decisions)
    projected_usage = Counter(video_usage_counts or {})
    previous_sha = ""
    for window in windows:
        current = updated[window["window_id"]]
        if (
            current.relevance_status != "images_are_irrelevant"
            and current.selected_asset_id
        ):
            selected = next(
                (
                    candidate
                    for candidate in window.get("shortlist", [])
                    if candidate.get("asset_id") == current.selected_asset_id
                ),
                None,
            )
            if selected and selected.get("sha256"):
                projected_usage[selected["sha256"]] += 1
                previous_sha = selected["sha256"]
            continue

        salvageable = [
            candidate
            for candidate in window.get("shortlist", [])
            if candidate.get("sha256")
            and not candidate.get("neutral_fallback", False)
            and not candidate.get("context_safe_fallback", False)
            and candidate.get("context_grounded", False)
            and candidate.get("grounding_basis")
            in {"exact_entity", "distinctive_context", "pexels_topic_concept"}
            and projected_usage[candidate["sha256"]] < maximum_uses_per_video
            and candidate["sha256"] != previous_sha
        ]

        def salvage_rank(candidate: dict) -> tuple:
            components = candidate.get("score_components", {})
            entity_hits = components.get("entity_hits", [])
            multiword_entity = any(
                len(tokenize(str(entity))) >= 2 for entity in entity_hits
            )
            identity = normalize_search_text(
                " ".join(
                    [
                        str(candidate.get("filename", "")),
                        str(candidate.get("local_caption", "")),
                    ]
                )
            )
            simple_brand_visual = any(
                token in set(tokenize(identity))
                for token in {"logo", "sign", "office", "headquarters"}
            )
            basis = candidate.get("grounding_basis")
            return (
                3 if basis == "exact_entity" else 2 if basis == "pexels_topic_concept" else 1,
                bool(multiword_entity),
                bool(simple_brand_visual),
                float(candidate.get("local_score", 0)),
                float(candidate.get("crop_retention_16_9", 0)),
            )

        salvageable.sort(key=salvage_rank, reverse=True)
        if salvageable:
            chosen = salvageable[0]
            projected_usage[chosen["sha256"]] += 1
            previous_sha = chosen["sha256"]
            updated[window["window_id"]] = WindowDecision(
                window_id=window["window_id"],
                relevance_status="relevant",
                selected_asset_id=chosen["asset_id"],
                confidence=0.4,
                selection_reason=(
                    "No exact online photograph was available. Used the strongest "
                    "slightly relevant, context-grounded local visual instead of "
                    "showing an empty fallback frame."
                ),
                alternative_asset_ids=[
                    candidate["asset_id"] for candidate in salvageable[1:4]
                ],
                online_search_query=current.online_search_query,
                required_entity=current.required_entity,
            )
            continue

        asset = _create_context_safe_fallback_asset(video_number, window)
        candidate = _context_safe_candidate(asset)
        window["shortlist"] = [
            candidate,
            *[
                item
                for item in window["shortlist"]
                if item["asset_id"] != asset["asset_id"]
            ],
        ]
        window.setdefault("_transient_assets", []).append(asset)
        updated[window["window_id"]] = WindowDecision(
            window_id=window["window_id"],
            relevance_status="relevant",
            selected_asset_id=asset["asset_id"],
            confidence=1.0,
            selection_reason=(
                "No sufficiently grounded local or Pexels image was available. "
                "Used a locally generated, visibly designed context graphic rather "
                "than an empty frame or misleading visual evidence."
            ),
            alternative_asset_ids=[],
        )
    return updated


def _enforce_selection_constraints(
    windows: list[dict],
    decisions: dict[str, WindowDecision],
    assets_by_id: dict[str, dict],
    video_usage_counts: Counter[str],
    maximum_uses_per_video: int,
    fallback_factory: Callable[[dict], dict] | None = None,
) -> list[dict]:
    selected: list[dict] = []
    previous_sha = ""
    for window in windows:
        decision = decisions[window["window_id"]]
        candidate_by_id = {
            item["asset_id"]: item
            for item in window["shortlist"]
        }
        preferred_ids = [
            decision.selected_asset_id,
            *decision.alternative_asset_ids,
        ]
        deduplicated_ids = [
            asset_id
            for asset_id in dict.fromkeys(preferred_ids)
            if asset_id is not None
        ]

        def acceptable(asset_id: str, *, allow_reuse: bool) -> bool:
            candidate = candidate_by_id.get(asset_id)
            if candidate is None:
                return False
            if not (
                candidate.get("context_grounded", True)
                or candidate.get("neutral_fallback", False)
                or candidate.get("online_fallback", False)
            ):
                return False
            sha = candidate["sha256"]
            if sha == previous_sha:
                return False
            if video_usage_counts[sha] >= maximum_uses_per_video:
                return False
            if not allow_reuse and video_usage_counts[sha] > 0:
                return False
            return (
                candidate.get("brand_logo", False)
                or "crop_risk" not in candidate.get("quality_flags", [])
            )

        preferred_candidate = candidate_by_id.get(
            decision.selected_asset_id or "",
            {},
        )
        preferred_priority_reuse = bool(
            preferred_candidate.get("online_fallback") is True
            or (
                preferred_candidate.get("grounding_basis") == "exact_entity"
                and float(preferred_candidate.get("local_score", 0)) >= 30
            )
        )
        if (
            preferred_priority_reuse
            and (
                preferred_candidate.get("brand_logo", False)
                or "crop_risk" not in preferred_candidate.get("quality_flags", [])
            )
            and acceptable(decision.selected_asset_id or "", allow_reuse=True)
        ):
            chosen_id = decision.selected_asset_id
            override_reason = (
                "Intentional exact-context image reuse was preferred over a "
                "weaker unused candidate."
                if video_usage_counts[preferred_candidate.get("sha256", "")] > 0
                else ""
            )
        else:
            chosen_id = next(
                (
                    asset_id
                    for asset_id in deduplicated_ids
                    if acceptable(asset_id, allow_reuse=False)
                ),
                None,
            )
            override_reason = ""
        if chosen_id is None:
            chosen_id = next(
                (
                    asset_id
                    for asset_id in deduplicated_ids
                    if acceptable(asset_id, allow_reuse=True)
                ),
                None,
            )
            if chosen_id is not None:
                override_reason = "Video-level reuse was necessary."
        if chosen_id is None:
            chosen_id = next(
                (
                    asset_id
                    for asset_id in deduplicated_ids
                    if candidate_by_id.get(asset_id, {}).get("sha256") != previous_sha
                    and (
                        candidate_by_id.get(asset_id, {}).get(
                            "context_grounded", True
                        )
                        or candidate_by_id.get(asset_id, {}).get(
                            "neutral_fallback", False
                        )
                        or candidate_by_id.get(asset_id, {}).get(
                            "online_fallback", False
                        )
                    )
                    and video_usage_counts[
                        candidate_by_id.get(asset_id, {}).get("sha256", "")
                    ]
                    < maximum_uses_per_video
                ),
                None,
            )
            override_reason = (
                "A crop-risk fallback under the video reuse cap was necessary."
            )
        if chosen_id is None:
            if fallback_factory is None:
                raise RuntimeError(
                    f"No image remains below the {maximum_uses_per_video}-use video "
                    f"cap for {window['window_id']}."
                )
            fallback_asset = None
            # FFmpeg's RGB-to-YUV conversion can quantize nearby digest-derived
            # colors into identical pixels. Render deterministic variants until the
            # actual file checksum, not merely its filename, is unique for this slot.
            for variant in range(1, 65):
                candidate_asset = fallback_factory(
                    {**window, "_context_safe_variant": variant}
                )
                candidate_sha = candidate_asset["sha256"]
                if (
                    candidate_sha != previous_sha
                    and video_usage_counts[candidate_sha]
                    < maximum_uses_per_video
                ):
                    fallback_asset = candidate_asset
                    break
            if fallback_asset is None:
                raise RuntimeError(
                    "Could not render a unique context-safe fallback after 64 variants for "
                    f"{window['window_id']}."
                )
            fallback_candidate = _context_safe_candidate(fallback_asset)
            window["shortlist"] = [fallback_candidate, *window["shortlist"]]
            window.setdefault("_transient_assets", []).append(fallback_asset)
            candidate_by_id[fallback_asset["asset_id"]] = fallback_candidate
            assets_by_id[fallback_asset["asset_id"]] = fallback_asset
            chosen_id = fallback_asset["asset_id"]
            override_reason = (
                "Every grounded shortlist image had reached the video reuse cap; "
                "used a unique locally generated context graphic."
            )
        if chosen_id != decision.selected_asset_id and not override_reason:
            override_reason = (
                "Selected the best non-repeated, crop-safe candidate from the "
                "AI alternatives/local shortlist."
            )

        candidate = candidate_by_id[chosen_id]
        asset = assets_by_id[chosen_id]
        choice_changed = chosen_id != decision.selected_asset_id
        final_reason = (
            override_reason
            if choice_changed and override_reason
            else decision.selection_reason
        )
        final_confidence = (
            1.0
            if asset.get("context_safe_fallback", False)
            else min(float(decision.confidence), 0.65)
            if choice_changed
            else float(decision.confidence)
        )
        selected.append(
            {
                **{
                    key: value
                    for key, value in window.items()
                    if key not in {"shortlist", "_transient_assets"}
                },
                "selected_asset": {
                    "asset_id": asset["asset_id"],
                    "absolute_path": asset["absolute_path"],
                    "workspace_relative_path": asset["workspace_relative_path"],
                    "sha256": asset["sha256"],
                    "width": asset["width"],
                    "height": asset["height"],
                    "aspect_ratio": asset["aspect_ratio"],
                    "crop_retention_16_9": asset["crop_retention_16_9"],
                    "source_url": asset.get("source_url", ""),
                    "creator": asset.get("creator", ""),
                    "license": asset.get("license", {}),
                    "quality_flags": asset.get("quality_flags", []),
                    "context_safe_fallback": bool(
                        asset.get("context_safe_fallback", False)
                    ),
                    "brand_logo": bool(candidate.get("brand_logo", False)),
                },
                "confidence": round(final_confidence, 4),
                "selection_reason": final_reason,
                "selection_override": override_reason,
                "selector_choice": {
                    "selected_asset_id": decision.selected_asset_id,
                    "confidence": round(float(decision.confidence), 4),
                    "selection_reason": decision.selection_reason,
                    "alternative_asset_ids": decision.alternative_asset_ids,
                },
                "local_score": candidate["local_score"],
                "alternatives": [
                    {
                        "asset_id": item["asset_id"],
                        "workspace_relative_path": item[
                            "workspace_relative_path"
                        ],
                        "local_score": item["local_score"],
                    }
                    for item in window["shortlist"]
                    if item["asset_id"] != chosen_id
                ][:3],
                "shortlist": window["shortlist"],
                "requires_license_review": asset.get("license", {}).get("status")
                in {"", "unknown"},
            }
        )
        video_usage_counts[asset["sha256"]] += 1
        previous_sha = asset["sha256"]
    return selected


def _copy_selected(
    video_dir: Path,
    story_order: int,
    windows: list[dict],
) -> None:
    destination = video_dir / "05_visuals" / "selected_archive"
    destination.mkdir(parents=True, exist_ok=True)
    for index, window in enumerate(windows, start=1):
        source = Path(window["selected_asset"]["absolute_path"])
        target = destination / (
            f"{story_order:02d}_{index:03d}_{source.name}"
        )
        if target.is_file() and sha256_file(target) == window["selected_asset"]["sha256"]:
            pass
        else:
            shutil.copy2(source, target)
        window["selected_asset"]["project_copy"] = str(target)


def _story_plan_fingerprint(
    story: dict,
    storyboard_story: dict,
    windows: list[dict],
    settings: MediaSelectionSettings,
) -> str:
    return json_fingerprint(
        {
            "story_id": story["story_id"],
            "audio_file": story.get("audio_file", ""),
            "alignment_file": story.get("alignment_file", ""),
            "hero_images": [
                {
                    "time_range": image.get("time_range"),
                    "file": image.get("output_file"),
                    "sha256": image.get("generation", {}).get("file_sha256"),
                }
                for image in storyboard_story.get("images", [])
            ],
            "windows": windows,
            "settings": asdict(settings),
        }
    )


def _selector_input_fingerprint(
    story: dict,
    storyboard_story: dict,
    windows: list[dict],
    settings: MediaSelectionSettings,
) -> str:
    """Fingerprint only inputs that can change the local AI selection.

    Pexels query-model and API limits are deliberately absent. Changing the
    online fallback must not repeat the more expensive local selector request.
    """
    return json_fingerprint(
        {
            "story_id": story["story_id"],
            "audio_file": story.get("audio_file", ""),
            "alignment_file": story.get("alignment_file", ""),
            "hero_images": [
                {
                    "time_range": image.get("time_range"),
                    "file": image.get("output_file"),
                    "sha256": image.get("generation", {}).get("file_sha256"),
                }
                for image in storyboard_story.get("images", [])
            ],
            "windows": windows,
            "selector": {
                "model": settings.model,
                "reasoning_effort": settings.reasoning_effort,
                "shortlist_size": settings.shortlist_size,
                "minimum_local_score": settings.minimum_local_score,
                "minimum_ai_confidence": settings.minimum_ai_confidence,
            },
        }
    )


def _windows_requiring_online_fallback(
    windows: list[dict],
    decisions: dict[str, WindowDecision],
    *,
    video_usage_counts: Counter[str],
    maximum_uses: int,
) -> list[dict]:
    """Preview the same local-choice failures handled by the Pexels resolver."""
    unresolved: list[dict] = []
    projected_usage = Counter(video_usage_counts)
    previous_sha = ""
    for window in windows:
        decision = decisions[window["window_id"]]
        selected = next(
            (
                candidate
                for candidate in window.get("shortlist", [])
                if candidate.get("asset_id") == decision.selected_asset_id
            ),
            None,
        )
        selected_sha = str(selected.get("sha256", "")) if selected else ""
        selected_is_neutral = bool(
            selected
            and (
                selected.get("neutral_fallback", False)
                or selected.get("context_safe_fallback", False)
                or selected.get("grounding_basis") == "context_safe_neutral"
            )
        )
        selected_is_conflicted = bool(
            selected_sha
            and (
                projected_usage[selected_sha] >= maximum_uses
                or selected_sha == previous_sha
            )
        )
        if (
            decision.relevance_status == "images_are_irrelevant"
            or selected is None
            or selected_is_neutral
            or selected_is_conflicted
        ):
            unresolved.append(window)
            continue
        projected_usage[selected_sha] += 1
        previous_sha = selected_sha
    return unresolved


def _cached_story_current(story_plan: dict, fingerprint: str) -> bool:
    if story_plan.get("input_fingerprint") != fingerprint:
        return False
    for window in story_plan.get("windows", []):
        selected = window.get("selected_asset", {})
        path = Path(selected.get("absolute_path", ""))
        if (
            not path.is_file()
            or not selected.get("sha256")
            or sha256_file(path) != selected["sha256"]
        ):
            return False
    return True


def run_media_selection(
    *,
    video_number: int,
    settings: MediaSelectionSettings | None = None,
    index_settings: MediaIndexSettings | None = None,
    selector: Selector | None = None,
    story_number: int | None = None,
    rescan: bool = True,
    force: bool = False,
    refresh_fallbacks: bool = False,
    confirm_ai_cost: bool = False,
    offline: bool = False,
) -> dict:
    settings = settings or MediaSelectionSettings.from_environment()
    settings.validate()
    if selector is None and not offline and not confirm_ai_cost:
        raise RuntimeError(
            "AI media selection is blocked. Re-run with --confirm-ai-cost or "
            "use --offline."
        )
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")
    state, timing, storyboard = _require_inputs(video_dir)
    index_settings = index_settings or MediaIndexSettings.from_environment()
    media_index = load_media_index(index_settings, rescan=rescan)
    assets_by_id = {
        item["asset_id"]: item
        for item in media_index.get("assets", [])
        if _is_in_video_media_asset(item)
    }
    timing_stories = timing.get("stories", [])
    storyboard_by_id = {
        item["story_id"]: item
        for item in storyboard.get("stories", [])
    }
    if story_number is not None and not 1 <= story_number <= len(timing_stories):
        raise ValueError(f"--story must be between 1 and {len(timing_stories)}.")
    selected_timing = (
        [timing_stories[story_number - 1]]
        if story_number is not None
        else timing_stories
    )

    output_path = video_dir / "05_visuals" / "media_plan.json"
    existing = {}
    if output_path.is_file():
        try:
            existing = load_json(output_path, "Media plan")
        except RuntimeError:
            existing = {}
    planned_by_id = {
        item.get("story_id"): item
        for item in existing.get("stories", [])
        if isinstance(item, dict)
    }
    if selector is not None:
        selection_function = selector
    elif offline:
        selection_function = local_story_selector
    else:
        selection_function = select_story_with_openai

    pexels_download_count = 0
    pexels_search_count = 0
    pexels_query_planner_requests = 0
    pexels_query_planner_cache_hits = 0
    selector_cache_hits = 0
    selector_requests_this_run = 0
    pexels_search_cache: dict[str, dict] = {}
    selected_story_ids = {item["story_id"] for item in selected_timing}
    video_usage_counts: Counter[str] = Counter()
    current_hero_owners: dict[str, set[str]] = {}

    # Hero images count toward the global cap and are excluded from all archive
    # windows in this video. Generated heroes are also saved to the reusable Assets
    # library, so filename-based selection can otherwise place the same pixels next
    # to their hero range and create one long, visually frozen hold.
    for storyboard_story in storyboard.get("stories", []):
        for image in storyboard_story.get("images", []):
            checksum = str(image.get("generation", {}).get("file_sha256", ""))
            if not checksum:
                image_path = Path(str(image.get("output_file", "")))
                if image_path.is_file():
                    checksum = sha256_file(image_path)
            if checksum:
                video_usage_counts[checksum] += 1
                current_hero_owners.setdefault(checksum, set()).add(
                    storyboard_story["story_id"]
                )

    # A one-story repair must respect selections already saved for other stories.
    for story_id, planned_story in planned_by_id.items():
        if story_id in selected_story_ids:
            continue
        for window in planned_story.get("windows", []):
            checksum = str(window.get("selected_asset", {}).get("sha256", ""))
            if checksum:
                video_usage_counts[checksum] += 1

    overused = {
        checksum: count
        for checksum, count in video_usage_counts.items()
        if count > settings.maximum_uses_per_video
    }
    if overused:
        raise RuntimeError(
            "Existing visual plan already exceeds MEDIA_MAX_USES_PER_VIDEO; "
            "rerun the complete archive-visual stage with --force."
        )

    for story in selected_timing:
        storyboard_story = storyboard_by_id.get(story["story_id"])
        if storyboard_story is None:
            raise RuntimeError(
                f"Storyboard is missing for {story['story_id']}."
            )
        windows = build_archive_windows(story, storyboard_story, settings)
        local_media_index = _local_repository_index(media_index)
        used_sha: set[str] = set()
        previous_sha = ""
        for window in windows:
            window["shortlist"] = shortlist_assets(
                local_media_index,
                window,
                settings,
                already_used_sha=used_sha,
                previous_sha=previous_sha,
                excluded_sha=_hero_exclusions_for_story(
                    current_hero_owners,
                    story["story_id"],
                )
                | {
                    checksum
                    for checksum, count in video_usage_counts.items()
                    if count >= settings.maximum_uses_per_video
                },
            )
            if not window["shortlist"]:
                fallback_asset = _create_context_safe_fallback_asset(
                    video_number,
                    window,
                )
                window["shortlist"] = [_context_safe_candidate(fallback_asset)]
                window.setdefault("_transient_assets", []).append(fallback_asset)
            # Shortlists are intentionally computed without committing a choice.
            # Reuse is enforced after the story-level AI sees all windows.
        fingerprint = _story_plan_fingerprint(
            story,
            storyboard_story,
            windows,
            settings,
        )
        selector_fingerprint = _selector_input_fingerprint(
            story,
            storyboard_story,
            windows,
            settings,
        )
        cached = planned_by_id.get(story["story_id"], {})
        if (
            not force
            and not refresh_fallbacks
            and _cached_story_current(cached, fingerprint)
        ):
            for window in cached.get("windows", []):
                checksum = str(window.get("selected_asset", {}).get("sha256", ""))
                if checksum:
                    video_usage_counts[checksum] += 1
                    if video_usage_counts[checksum] > settings.maximum_uses_per_video:
                        raise RuntimeError(
                            "Cached visual plan exceeds MEDIA_MAX_USES_PER_VIDEO; "
                            "rerun the complete archive-visual stage with --force."
                        )
            continue

        selector_cache = _selector_cache_path(video_dir, story["story_id"])
        decisions = (
            None
            if force or offline
            else _load_selector_cache(
                selector_cache,
                selector_fingerprint,
                compatible_fingerprints=(
                    {
                        fingerprint,
                        str(cached.get("input_fingerprint", "")),
                    }
                    if refresh_fallbacks and cached.get("input_fingerprint")
                    else None
                ),
            )
        )
        if decisions is None:
            decisions = selection_function(story, windows, settings)
            if not offline:
                _save_selector_cache(
                    selector_cache,
                    story_id=story["story_id"],
                    fingerprint=selector_fingerprint,
                    decisions=decisions,
                )
                selector_requests_this_run += 1
        else:
            selector_cache_hits += 1
            if refresh_fallbacks:
                # Migrate an accepted prior-plan cache to the current fallback
                # configuration fingerprint. Otherwise a second repair run would
                # miss the compatible fingerprint and repeat the paid selector.
                _save_selector_cache(
                    selector_cache,
                    story_id=story["story_id"],
                    fingerprint=selector_fingerprint,
                    decisions=decisions,
                )
        decision_by_window = _validate_decisions(
            decisions,
            windows,
            story,
            minimum_confidence=settings.minimum_ai_confidence,
        )
        query_plan: StoryPexelsQueryPlan | None = None
        planner_windows = _windows_requiring_online_fallback(
            windows,
            decision_by_window,
            video_usage_counts=video_usage_counts,
            maximum_uses=settings.maximum_uses_per_video,
        )
        planner_fingerprint = ""
        planner_error = ""
        if planner_windows and not offline:
            planner_fingerprint = json_fingerprint(
                {
                    "story_id": story["story_id"],
                    "story_title": story.get("title", ""),
                    "windows": [
                        {
                            "window_id": window["window_id"],
                            "text": window.get("text", ""),
                            "timestamp_excerpt": window.get(
                                "timestamp_excerpt", ""
                            ),
                            "entities": window.get("entities", []),
                            "scene_intent": window.get("scene_intent", ""),
                        }
                        for window in planner_windows
                    ],
                    "model": settings.pexels_query_model,
                    "reasoning_effort": settings.pexels_query_reasoning_effort,
                }
            )
            planner_cache = _pexels_query_cache_path(
                video_dir,
                story["story_id"],
            )
            query_plan = (
                None
                if force
                else _load_pexels_query_cache(
                    planner_cache,
                    planner_fingerprint,
                )
            )
            if query_plan is None:
                try:
                    query_plan = plan_pexels_queries_with_openai(
                        story,
                        planner_windows,
                        settings,
                    )
                    _save_pexels_query_cache(
                        planner_cache,
                        story_id=story["story_id"],
                        fingerprint=planner_fingerprint,
                        plan=query_plan,
                    )
                    pexels_query_planner_requests += 1
                except Exception as exc:  # noqa: BLE001
                    planner_error = str(exc)
                    query_plan = None
            else:
                pexels_query_planner_cache_hits += 1
        # Resolve in a few deterministic passes. Replacing one over-cap choice can
        # reveal a later collision that was hidden by the original selector plan.
        # The shared topic cache prevents repeated Pexels searches between passes.
        for _repair_pass in range(max(3, len(windows))):
            before_repair = json_fingerprint(
                {
                    key: value.model_dump(mode="json")
                    for key, value in decision_by_window.items()
                }
            )
            (
                decision_by_window,
                media_index,
                pexels_downloads_used,
                pexels_searches_used,
            ) = resolve_pexels_fallbacks(
                video_number=video_number,
                windows=windows,
                decisions=decision_by_window,
                media_index=media_index,
                index_settings=index_settings,
                settings=settings,
                remaining_downloads=max(
                    0,
                    settings.pexels_max_downloads_per_video
                    - pexels_download_count,
                ),
                remaining_searches=max(
                    0,
                    settings.pexels_max_searches_per_video
                    - pexels_search_count,
                ),
                video_usage_counts=video_usage_counts,
                search_cache=pexels_search_cache,
                query_plan=query_plan,
            )
            pexels_download_count += pexels_downloads_used
            pexels_search_count += pexels_searches_used
            after_repair = json_fingerprint(
                {
                    key: value.model_dump(mode="json")
                    for key, value in decision_by_window.items()
                }
            )
            if after_repair == before_repair:
                break
        decision_by_window = _resolve_context_safe_fallbacks(
            video_number,
            windows,
            decision_by_window,
            video_usage_counts=video_usage_counts,
            maximum_uses_per_video=settings.maximum_uses_per_video,
        )
        assets_by_id = {
            item["asset_id"]: item
            for item in media_index.get("assets", [])
            if _is_in_video_media_asset(item)
        }
        for window in windows:
            for asset in window.get("_transient_assets", []):
                assets_by_id[asset["asset_id"]] = asset
        selected_windows = _enforce_selection_constraints(
            windows,
            decision_by_window,
            assets_by_id,
            video_usage_counts,
            settings.maximum_uses_per_video,
            fallback_factory=lambda window: _create_context_safe_fallback_asset(
                video_number,
                window,
            ),
        )
        if settings.copy_selected_assets:
            _copy_selected(
                video_dir,
                int(story["order"]),
                selected_windows,
            )
        effective_hero_images = _effective_hero_images(
            storyboard_story,
            settings.maximum_window_seconds,
        )
        hero_ranges = [
            (
                float(image["time_range"]["start"]),
                float(image["time_range"]["end"]),
            )
            for image in effective_hero_images
        ]
        coverage = coverage_quality(
            float(story["narration"]["duration"]),
            hero_ranges,
            selected_windows,
        )
        if coverage["status"] != "passed":
            raise RuntimeError(
                f"Visual coverage failed for {story['story_id']}: "
                f"{coverage['gaps']}"
            )
        planned_by_id[story["story_id"]] = {
            "story_id": story["story_id"],
            "order": int(story["order"]),
            "title": story.get("title", ""),
            "input_fingerprint": fingerprint,
            "selector": {
                "provider": (
                    "custom"
                    if selector is not None
                    else "offline"
                    if offline
                    else "openai"
                ),
                "model": (
                    "injected_selector"
                    if selector is not None
                    else "local_ranker"
                    if offline
                    else settings.model
                ),
                "reasoning_effort": (
                    None
                    if selector is not None or offline
                    else settings.reasoning_effort
                ),
            },
            "pexels_query_planner": {
                "provider": "openai" if query_plan is not None else "deterministic",
                "model": (
                    settings.pexels_query_model if query_plan is not None else None
                ),
                "reasoning_effort": (
                    settings.pexels_query_reasoning_effort
                    if query_plan is not None
                    else None
                ),
                "input_fingerprint": planner_fingerprint,
                "group_count": len(query_plan.groups) if query_plan else 0,
                "error": planner_error,
            },
            "hero_ranges": [
                {
                    "image_index": image["image_index"],
                    **image["time_range"],
                    "asset": image["output_file"],
                    "bridged_gap_seconds": image.get("bridged_gap_seconds", 0.0),
                }
                for image in effective_hero_images
            ],
            "windows": selected_windows,
            "coverage": coverage,
            "quality": {
                "status": "passed",
                "no_immediate_duplicates": all(
                    first["selected_asset"]["sha256"]
                    != second["selected_asset"]["sha256"]
                    for first, second in zip(
                        selected_windows,
                        selected_windows[1:],
                    )
                ),
                "unique_asset_count": len(
                    {
                        window["selected_asset"]["sha256"]
                        for window in selected_windows
                    }
                ),
                "window_count": len(selected_windows),
                "maximum_asset_use_count": max(
                    (
                        video_usage_counts[window["selected_asset"]["sha256"]]
                        for window in selected_windows
                    ),
                    default=0,
                ),
                "license_review_count": sum(
                    window["requires_license_review"]
                    for window in selected_windows
                ),
                "crop_risk_count": sum(
                    "crop_risk"
                    in window["selected_asset"].get("quality_flags", [])
                    for window in selected_windows
                ),
                "context_safe_fallback_count": sum(
                    window["selected_asset"].get(
                        "context_safe_fallback", False
                    )
                    for window in selected_windows
                ),
            },
        }
        ordered_partial = [
            planned_by_id[item["story_id"]]
            for item in timing_stories
            if item["story_id"] in planned_by_id
        ]
        atomic_write_json(
            output_path,
            {
                "schema_version": MEDIA_PLAN_SCHEMA_VERSION,
                "video_number": video_number,
                "video_name": project_name(video_number),
                "updated_at_utc": utc_now(),
                "configuration": asdict(settings),
                "media_index_path": str(index_settings.index_path),
                "media_index_fingerprint": media_index.get(
                    "index_fingerprint",
                    "",
                ),
                "selection_mode": (
                    "copied" if settings.copy_selected_assets else "immutable_reference"
                ),
                "selector_cache_hits": selector_cache_hits,
                "selector_requests_this_run": selector_requests_this_run,
                "stories": ordered_partial,
                "quality": {"status": "partial"},
            },
        )

    ordered = [
        planned_by_id[item["story_id"]]
        for item in timing_stories
        if item["story_id"] in planned_by_id
    ]
    all_ready = len(ordered) == len(timing_stories) and all(
        item.get("quality", {}).get("status") == "passed"
        for item in ordered
    )
    plan = {
        "schema_version": MEDIA_PLAN_SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "created_at_utc": existing.get("created_at_utc", utc_now()),
        "updated_at_utc": utc_now(),
        "configuration": asdict(settings),
        "media_index_path": str(index_settings.index_path),
        "media_index_fingerprint": media_index.get("index_fingerprint", ""),
        "selection_mode": (
            "copied" if settings.copy_selected_assets else "immutable_reference"
        ),
        "pexels_download_count": pexels_download_count,
        "pexels_search_count": pexels_search_count,
        "pexels_query_planner_requests_this_run": pexels_query_planner_requests,
        "pexels_query_planner_cache_hits": pexels_query_planner_cache_hits,
        "selector_cache_hits": selector_cache_hits,
        "selector_requests_this_run": selector_requests_this_run,
        "story_count": len(timing_stories),
        "planned_story_count": len(ordered),
        "archive_window_count": sum(
            len(item.get("windows", []))
            for item in ordered
        ),
        "stories": ordered,
        "quality": {
            "status": "passed" if all_ready else "partial",
            "all_stories_planned": all_ready,
            "every_narration_interval_has_visual_coverage": all(
                item.get("coverage", {}).get("status") == "passed"
                for item in ordered
            ),
            "no_immediate_duplicates": all(
                item.get("quality", {}).get("no_immediate_duplicates", False)
                for item in ordered
            ),
            "license_review_count": sum(
                item.get("quality", {}).get("license_review_count", 0)
                for item in ordered
            ),
            "crop_risk_count": sum(
                item.get("quality", {}).get("crop_risk_count", 0)
                for item in ordered
            ),
            "context_safe_fallback_count": sum(
                item.get("quality", {}).get("context_safe_fallback_count", 0)
                for item in ordered
            ),
            "maximum_uses_per_image": settings.maximum_uses_per_video,
            "reuse_cap_respected": all(
                count <= settings.maximum_uses_per_video
                for count in video_usage_counts.values()
            ),
        },
    }
    atomic_write_json(output_path, plan)
    usage = [
        (
            (
                f"video_{video_number}:story_{story['order']:02d}:"
                f"{window['window_id']}"
            ),
            window["selected_asset"]["asset_id"],
        )
        for story in ordered
        for window in story.get("windows", [])
    ]
    update_usage_events(
        media_index,
        project_prefix=f"video_{video_number}:",
        selections=usage,
        index_path=index_settings.index_path,
    )
    _write_stage(
        video_dir,
        state,
        {
            "status": "VISUALS_READY" if all_ready else "PARTIAL",
            "reason": (
                "Every non-hero narration interval has a selected archive image."
                if all_ready
                else "A pilot subset has archive-image coverage."
            ),
            "media_plan_file": str(output_path),
            "planned_story_count": len(ordered),
            "archive_window_count": plan["archive_window_count"],
            "media_index_fingerprint": plan["media_index_fingerprint"],
        },
    )
    return plan


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select archive imagery for narration not covered by AI heroes."
    )
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument("--story", type=int)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--confirm-ai-cost", action="store_true")
    parser.add_argument("--no-rescan", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--refresh-fallbacks",
        action="store_true",
        help="Rebuild local/Pexels fallback choices while reusing selector cache.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.video < 1:
            raise ValueError("--video must be 1 or greater.")
        plan = run_media_selection(
            video_number=args.video,
            story_number=args.story,
            rescan=not args.no_rescan,
            force=args.force,
            refresh_fallbacks=args.refresh_fallbacks,
            confirm_ai_cost=args.confirm_ai_cost,
            offline=args.offline,
        )
        summary = {
            "video_number": args.video,
            "planned_story_count": plan["planned_story_count"],
            "archive_window_count": plan["archive_window_count"],
            "quality": plan["quality"],
            "media_plan_file": str(
                project_dir(args.video)
                / "05_visuals"
                / "media_plan.json"
            ),
        }
        print(
            json.dumps(summary, ensure_ascii=False, indent=2)
            if args.json
            else "\n".join(f"{key}: {value}" for key, value in summary.items())
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        report_cli_error("MEDIA SELECTION", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
