from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
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
)
from media_index import (  # noqa: E402
    MediaIndexSettings,
    load_media_index,
    normalize_search_text,
    tokenize,
    update_usage_events,
)
from file_locations import project_dir, project_name  # noqa: E402
from separator_policy import require_separator_checkpoint  # noqa: E402


MEDIA_PLAN_SCHEMA_VERSION = 1
SELECTOR_CACHE_SCHEMA_VERSION = 1
STAGE_NAME = "archive_visuals"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MediaSelectionSettings:
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "medium"
    shortlist_size: int = 8
    minimum_window_seconds: float = 3.0
    target_window_seconds: float = 5.0
    maximum_window_seconds: float = 7.0
    minimum_local_score: float = 1.0
    api_retries: int = 2
    timeout_seconds: float = 120.0
    copy_selected_assets: bool = False
    wikimedia_fallback_enabled: bool = True
    wikimedia_max_downloads_per_video: int = 10
    wikimedia_max_searches_per_video: int = 6

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
                os.getenv("MEDIA_MIN_WINDOW_SECONDS", "3")
            ),
            target_window_seconds=float(
                os.getenv("MEDIA_TARGET_WINDOW_SECONDS", "5")
            ),
            maximum_window_seconds=float(
                os.getenv("MEDIA_MAX_WINDOW_SECONDS", "7")
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
            wikimedia_fallback_enabled=os.getenv(
                "WIKIMEDIA_FALLBACK_ENABLED",
                "true",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            wikimedia_max_downloads_per_video=int(
                os.getenv("WIKIMEDIA_MAX_DOWNLOADS_PER_VIDEO", "10")
            ),
            wikimedia_max_searches_per_video=int(
                os.getenv("WIKIMEDIA_MAX_SEARCHES_PER_VIDEO", "6")
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
        if not 0 <= self.wikimedia_max_downloads_per_video <= 30:
            raise ValueError(
                "WIKIMEDIA_MAX_DOWNLOADS_PER_VIDEO must be between 0 and 30."
            )
        if not 0 <= self.wikimedia_max_searches_per_video <= 30:
            raise ValueError(
                "WIKIMEDIA_MAX_SEARCHES_PER_VIDEO must be between 0 and 30."
            )


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


def _load_selector_cache(
    path: Path,
    fingerprint: str,
) -> StoryMediaDecisions | None:
    if not path.is_file():
        return None
    try:
        payload = load_json(path, "Media selector cache")
        if (
            payload.get("schema_version") != SELECTOR_CACHE_SCHEMA_VERSION
            or payload.get("input_fingerprint") != fingerprint
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
            and end - windows[-1][0] <= settings.maximum_window_seconds + 0.25
        ):
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((cursor, end))
    return windows


def _overlap(start: float, end: float, item: dict) -> bool:
    item_start = float(item.get("start", 0))
    item_end = float(item.get("end", item_start))
    return item_end > start + 0.001 and item_start < end - 0.001


def _window_text(story: dict, start: float, end: float) -> str:
    for collection in ("phrases", "sentences", "visual_beats"):
        pieces = []
        for item in story.get(collection, []):
            if not _overlap(start, end, item):
                continue
            text = str(
                item.get("text")
                or item.get("important_claim")
                or ""
            ).strip()
            if text and text not in pieces:
                pieces.append(text)
        if pieces:
            return " ".join(pieces)
    return str(story.get("title", "")).strip()


def _window_entities(story: dict, text: str) -> list[str]:
    normalized = normalize_search_text(text)
    entities: list[str] = []
    for values in story.get("named_entities", {}).values():
        for value in values:
            if normalize_search_text(str(value)) in normalized and value not in entities:
                entities.append(str(value))
    return entities


def infer_scene_intent(text: str) -> str:
    tokens = set(tokenize(text))
    rules = (
        ({"ceo", "founder", "executive", "person", "president"}, "person"),
        ({"phone", "mobile", "android", "iphone", "app"}, "mobile_device"),
        ({"rocket", "launch", "space", "satellite", "orbit"}, "space"),
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
        for image in storyboard_story.get("images", [])
        if image.get("validation", {}).get("status") == "passed"
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
            entities = _window_entities(story, text)
            visual_queries = [
                str(item.get("visual_query", "")).strip()
                for item in story.get("visual_beats", [])
                if _overlap(start, end, item) and item.get("visual_query")
            ]
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
    semantic_score = sum(
        1.0 + math.log((asset_count + 1) / (document_frequencies[token] + 1))
        for token in overlap
    )
    normalized_path = normalize_search_text(asset.get("workspace_relative_path", ""))
    entity_hits = [
        entity
        for entity in entities
        if normalize_search_text(entity) in normalized_path
    ]
    exact_entity_bonus = 5.0 * len(entity_hits)
    phrase_bonus = sum(
        1.5
        for token in query_tokens
        if len(token) >= 5 and token in normalize_search_text(asset.get("filename", ""))
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
) -> list[dict]:
    assets = [
        item
        for item in index.get("assets", [])
        if Path(item.get("absolute_path", "")).is_file()
        and item.get("sha256")
    ]
    if not assets:
        raise RuntimeError("The media index contains no usable assets.")
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
                "spoken_text": window["text"],
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
- Match the precise spoken claim and entity, not merely a broad technology category.
- Do not use the same image twice in this story unless no reasonable alternative exists.
- Never use the same image in adjacent windows.
- Prefer crop-safe 16:9 assets without low-resolution or crop-risk flags.
- The `user_confirmed_no_copyright` status is cleared for use.
- An `unknown` license is allowed for planning but must remain visibly recorded.
- If every candidate is unrelated, merely shares a broad company/category, or would
  visually misrepresent the spoken claim, return relevance_status exactly as
  `images_are_irrelevant`, selected_asset_id as null, and supply a concise Wikimedia
  search query plus one exact required_entity visible in the story title, spoken text,
  or supplied entities. Reuse the story's main entity when a short window uses a
  pronoun or splits a sentence across windows.
- Never force a local selection after deciding the images are irrelevant.
- For a normal choice, return relevance_status=`relevant` and choose only an asset_id
  listed in that window's candidates.
- Return exactly one decision for every window, in the same order.
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


def _story_entities(story: dict) -> list[str]:
    candidates: list[str] = []
    for values in (story.get("named_entities") or {}).values():
        if isinstance(values, list):
            candidates.extend(str(value).strip() for value in values)
    for beat in story.get("visual_beats", []):
        for values in (beat.get("named_entities") or {}).values():
            if isinstance(values, list):
                candidates.extend(str(value).strip() for value in values)
    ignored = {
        "a",
        "an",
        "it",
        "its",
        "the",
        "this",
        "thursday",
        "friday",
        "monday",
        "saturday",
        "sunday",
        "tuesday",
        "wednesday",
    }
    return list(
        dict.fromkeys(
            value
            for value in candidates
            if normalize_search_text(value) not in ignored
            and len(normalize_search_text(value)) >= 3
        )
    )


def _ground_irrelevant_decision(
    decision: WindowDecision,
    window: dict,
    story: dict,
) -> WindowDecision:
    proposed = normalize_search_text(decision.required_entity)
    title = str(story.get("title", ""))
    visible_scope = normalize_search_text(
        " ".join([title, window.get("text", ""), *window.get("entities", [])])
    )
    if proposed and proposed in visible_scope:
        return decision

    entities = [*window.get("entities", []), *_story_entities(story)]
    title_normalized = normalize_search_text(title)
    proposed_tokens = set(proposed.split())
    query_tokens = set(normalize_search_text(decision.online_search_query).split())
    ranked = []
    for index, entity in enumerate(dict.fromkeys(entities)):
        normalized = normalize_search_text(entity)
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
) -> dict[str, WindowDecision]:
    supplied: dict[str, WindowDecision] = {}
    for decision in decisions.decisions:
        supplied.setdefault(decision.window_id, decision)
    result = {}
    for window in windows:
        decision = supplied.get(window["window_id"])
        if decision is None:
            chosen = window["shortlist"][0]
            decision = WindowDecision(
                window_id=window["window_id"],
                relevance_status="relevant",
                selected_asset_id=chosen["asset_id"],
                confidence=0.1,
                selection_reason=(
                    "The AI selector omitted this window; the strongest grounded "
                    "local candidate was selected automatically."
                ),
                alternative_asset_ids=[],
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
        decision.alternative_asset_ids = [
            asset_id
            for asset_id in decision.alternative_asset_ids
            if asset_id in allowed
        ][:3]
        if decision.relevance_status == "images_are_irrelevant":
            decision = _ground_irrelevant_decision(decision, window, story)
        result[decision.window_id] = decision
    return result


def _fallback_to_local_candidate(
    decision: WindowDecision,
    window: dict,
    reason: str,
) -> WindowDecision:
    chosen = window["shortlist"][0]
    alternatives = [
        item["asset_id"]
        for item in window["shortlist"][1:4]
    ]
    return WindowDecision(
        window_id=window["window_id"],
        relevance_status="relevant",
        selected_asset_id=chosen["asset_id"],
        confidence=0.15,
        selection_reason=(
            "Automatic recovery selected the strongest valid candidate from this "
            f"window's local shortlist. {reason}"
        )[:700],
        alternative_asset_ids=alternatives,
    )


def resolve_wikimedia_fallbacks(
    *,
    video_number: int,
    windows: list[dict],
    decisions: dict[str, WindowDecision],
    media_index: dict,
    index_settings: MediaIndexSettings,
    settings: MediaSelectionSettings,
    remaining_downloads: int,
    remaining_searches: int,
) -> tuple[dict[str, WindowDecision], dict, int, int]:
    irrelevant = [
        window
        for window in windows
        if decisions[window["window_id"]].relevance_status
        == "images_are_irrelevant"
    ]
    if not irrelevant:
        return decisions, media_index, 0, 0
    if not settings.wikimedia_fallback_enabled:
        reason = (
            "The Wikimedia fallback is disabled or its per-video download cap "
            "has been reached."
        )
        updated = dict(decisions)
        for window in irrelevant:
            current = decisions[window["window_id"]]
            updated[window["window_id"]] = _fallback_to_local_candidate(
                current,
                window,
                reason,
            )
        return updated, media_index, 0, 0

    from online_media_stage import (
        OnlineMediaSettings,
        download_candidate,
        save_search_manifest,
        search_wikimedia_commons,
    )

    online_settings = OnlineMediaSettings.from_environment()
    resolved_assets: dict[str, dict] = {}
    pending_downloads: dict[str, dict] = {}
    updated = dict(decisions)
    downloads_used = 0
    searches_used = 0
    cached_queries: dict[str, dict] = {}
    online_throttled = False

    def existing_asset(required_entity: str) -> dict | None:
        exact = re.sub(
            r"[^a-z0-9]+",
            " ",
            normalize_search_text(required_entity),
        ).strip()
        matches = []
        for asset in media_index.get("assets", []):
            source_url = str(asset.get("source_url", "")).casefold()
            identity = re.sub(
                r"[^a-z0-9]+",
                " ",
                normalize_search_text(
                    " ".join(
                        [
                            asset.get("filename", ""),
                            asset.get("local_caption", ""),
                        ]
                    )
                ),
            ).strip()
            license_status = asset.get("license", {}).get("status", "unknown")
            if (
                "commons.wikimedia.org" in source_url
                and exact in identity
                and license_status not in {"", "unknown"}
            ):
                matches.append(asset)
        return max(
            matches,
            key=lambda item: (
                float(item.get("crop_retention_16_9", 0)),
                int(item.get("width", 0)) * int(item.get("height", 0)),
            ),
            default=None,
        )

    for window in irrelevant:
        current = decisions[window["window_id"]]
        reusable = existing_asset(current.required_entity)
        if reusable is not None:
            resolved_assets[window["window_id"]] = reusable
            continue
        if online_throttled:
            updated[window["window_id"]] = _fallback_to_local_candidate(
                current,
                window,
                "Wikimedia throttled the current run, so further searches stopped.",
            )
            continue
        if searches_used >= remaining_searches:
            updated[window["window_id"]] = _fallback_to_local_candidate(
                current,
                window,
                "The per-video Wikimedia search cap was reached.",
            )
            continue
        cache_key = normalize_search_text(current.required_entity)
        try:
            search = cached_queries.get(cache_key)
            if search is None:
                if online_settings.request_interval_seconds:
                    time.sleep(online_settings.request_interval_seconds)
                search = search_wikimedia_commons(
                    query=current.online_search_query,
                    required_entity=current.required_entity,
                    context=window.get("text", ""),
                    settings=online_settings,
                )
                cached_queries[cache_key] = search
                searches_used += 1
                save_search_manifest(search, video_number=video_number)
            if not search.get("candidates"):
                raise RuntimeError("No exact-title, accepted-license result was found.")
            if downloads_used >= remaining_downloads:
                raise RuntimeError("The per-video Wikimedia download cap was reached.")
            downloaded = download_candidate(
                search["candidates"][0],
                query=current.online_search_query,
                required_entity=current.required_entity,
                context=window.get("text", ""),
                settings=online_settings,
            )
            pending_downloads[window["window_id"]] = {
                "download": downloaded,
                "decision": current,
            }
            downloads_used += 1
        except Exception as exc:  # noqa: BLE001
            if "429" in str(exc) or "too many requests" in str(exc).casefold():
                online_throttled = True
            updated[window["window_id"]] = _fallback_to_local_candidate(
                current,
                window,
                f"Online fallback error: {exc}",
            )

    refreshed = (
        load_media_index(index_settings, rescan=True)
        if pending_downloads
        else media_index
    )
    assets_by_sha = {
        item.get("sha256"): item
        for item in refreshed.get("assets", [])
        if item.get("sha256")
    }
    for window in irrelevant:
        item = pending_downloads.get(window["window_id"])
        asset = resolved_assets.get(window["window_id"])
        if asset is None and item is not None:
            asset = assets_by_sha.get(item["download"]["sha256"])
        if asset is None and item is None:
            continue
        if asset is None:
            updated[window["window_id"]] = _fallback_to_local_candidate(
                item["decision"],
                window,
                "The downloaded Wikimedia file was not found after media re-indexing.",
            )
            continue
        online_shortlist = shortlist_assets(
            {"assets": [asset]},
            window,
            settings,
        )[0]
        online_shortlist["online_fallback"] = True
        window["shortlist"] = [
            online_shortlist,
            *[
                candidate
                for candidate in window["shortlist"]
                if candidate["asset_id"] != online_shortlist["asset_id"]
            ],
        ][: settings.shortlist_size]
        updated[window["window_id"]] = WindowDecision(
            window_id=window["window_id"],
            relevance_status="relevant",
            selected_asset_id=asset["asset_id"],
            confidence=max(
                0.7,
                min(
                    0.95,
                    (item["decision"] if item else decisions[window["window_id"]]).confidence,
                ),
            ),
            selection_reason=(
                "AI returned images_are_irrelevant for every local candidate. "
                "Selected the highest-ranked Wikimedia Commons result after exact-"
                "title entity, license, resolution, and metadata relevance gates."
            ),
            alternative_asset_ids=[
                candidate["asset_id"]
                for candidate in window["shortlist"][1:4]
            ],
        )
    return updated, refreshed, downloads_used, searches_used


def _enforce_selection_constraints(
    windows: list[dict],
    decisions: dict[str, WindowDecision],
    assets_by_id: dict[str, dict],
) -> list[dict]:
    selected: list[dict] = []
    used_sha: set[str] = set()
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
            *[item["asset_id"] for item in window["shortlist"]],
        ]
        deduplicated_ids = list(dict.fromkeys(preferred_ids))

        def acceptable(asset_id: str, *, allow_story_reuse: bool) -> bool:
            candidate = candidate_by_id.get(asset_id)
            if candidate is None:
                return False
            sha = candidate["sha256"]
            if sha == previous_sha:
                return False
            if not allow_story_reuse and sha in used_sha:
                return False
            return "crop_risk" not in candidate.get("quality_flags", [])

        preferred_online = candidate_by_id.get(decision.selected_asset_id or "", {})
        if (
            preferred_online.get("online_fallback") is True
            and "crop_risk" not in preferred_online.get("quality_flags", [])
        ):
            chosen_id = decision.selected_asset_id
            override_reason = (
                "Intentional Wikimedia reuse was preferred over an unrelated local "
                "candidate."
                if preferred_online.get("sha256") in used_sha
                else ""
            )
        else:
            chosen_id = next(
                (
                    asset_id
                    for asset_id in deduplicated_ids
                    if acceptable(asset_id, allow_story_reuse=False)
                ),
                None,
            )
            override_reason = ""
        if chosen_id is None:
            chosen_id = next(
                (
                    asset_id
                    for asset_id in deduplicated_ids
                    if acceptable(asset_id, allow_story_reuse=True)
                ),
                None,
            )
            if chosen_id is not None:
                override_reason = "Story-level reuse was necessary."
        if chosen_id is None:
            chosen_id = next(
                (
                    asset_id
                    for asset_id in deduplicated_ids
                    if candidate_by_id.get(asset_id, {}).get("sha256") != previous_sha
                ),
                deduplicated_ids[0],
            )
            override_reason = "A crop-risk or repeated fallback was necessary."
        if chosen_id != decision.selected_asset_id and not override_reason:
            override_reason = (
                "Selected the best non-repeated, crop-safe candidate from the "
                "AI alternatives/local shortlist."
            )

        candidate = candidate_by_id[chosen_id]
        asset = assets_by_id[chosen_id]
        selected.append(
            {
                **{key: value for key, value in window.items() if key != "shortlist"},
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
                },
                "confidence": round(float(decision.confidence), 4),
                "selection_reason": decision.selection_reason,
                "selection_override": override_reason,
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
        used_sha.add(asset["sha256"])
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

    wikimedia_download_count = 0
    wikimedia_search_count = 0
    selector_cache_hits = 0
    selector_requests_this_run = 0
    for story in selected_timing:
        storyboard_story = storyboard_by_id.get(story["story_id"])
        if storyboard_story is None:
            raise RuntimeError(
                f"Storyboard is missing for {story['story_id']}."
            )
        windows = build_archive_windows(story, storyboard_story, settings)
        used_sha: set[str] = set()
        previous_sha = ""
        for window in windows:
            window["shortlist"] = shortlist_assets(
                media_index,
                window,
                settings,
                already_used_sha=used_sha,
                previous_sha=previous_sha,
            )
            if not window["shortlist"]:
                raise RuntimeError(f"No shortlist for {window['window_id']}.")
            # Shortlists are intentionally computed without committing a choice.
            # Reuse is enforced after the story-level AI sees all windows.
        fingerprint = _story_plan_fingerprint(
            story,
            storyboard_story,
            windows,
            settings,
        )
        cached = planned_by_id.get(story["story_id"], {})
        if not force and _cached_story_current(cached, fingerprint):
            continue

        selector_cache = _selector_cache_path(video_dir, story["story_id"])
        decisions = (
            None
            if force or offline
            else _load_selector_cache(selector_cache, fingerprint)
        )
        if decisions is None:
            decisions = selection_function(story, windows, settings)
            if not offline:
                _save_selector_cache(
                    selector_cache,
                    story_id=story["story_id"],
                    fingerprint=fingerprint,
                    decisions=decisions,
                )
                selector_requests_this_run += 1
        else:
            selector_cache_hits += 1
        decision_by_window = _validate_decisions(decisions, windows, story)
        (
            decision_by_window,
            media_index,
            downloads_used,
            searches_used,
        ) = resolve_wikimedia_fallbacks(
            video_number=video_number,
            windows=windows,
            decisions=decision_by_window,
            media_index=media_index,
            index_settings=index_settings,
            settings=settings,
            remaining_downloads=max(
                0,
                settings.wikimedia_max_downloads_per_video
                - wikimedia_download_count,
            ),
            remaining_searches=max(
                0,
                settings.wikimedia_max_searches_per_video
                - wikimedia_search_count,
            ),
        )
        wikimedia_download_count += downloads_used
        wikimedia_search_count += searches_used
        assets_by_id = {
            item["asset_id"]: item
            for item in media_index.get("assets", [])
        }
        selected_windows = _enforce_selection_constraints(
            windows,
            decision_by_window,
            assets_by_id,
        )
        if settings.copy_selected_assets:
            _copy_selected(
                video_dir,
                int(story["order"]),
                selected_windows,
            )
        hero_ranges = [
            (
                float(image["time_range"]["start"]),
                float(image["time_range"]["end"]),
            )
            for image in storyboard_story["images"]
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
            "hero_ranges": [
                {
                    "image_index": image["image_index"],
                    **image["time_range"],
                    "asset": image["output_file"],
                }
                for image in storyboard_story["images"]
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
                "license_review_count": sum(
                    window["requires_license_review"]
                    for window in selected_windows
                ),
                "crop_risk_count": sum(
                    "crop_risk"
                    in window["selected_asset"].get("quality_flags", [])
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
        "wikimedia_download_count": wikimedia_download_count,
        "wikimedia_search_count": wikimedia_search_count,
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
