from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_utils import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_json,
    json_fingerprint,
    sha256_bytes,
    sha256_file,
    sha256_text,
)
from file_locations import PROJECTS_ROOT, project_dir, project_name  # noqa: E402
from tooling import find_executable  # noqa: E402


STAGE_NAME = "storyboard_images"
STORYBOARD_SCHEMA_VERSION = 1
DEFAULT_STYLE = (
    "cinematic photorealistic editorial technology-news still, restrained color "
    "grade, realistic materials and lighting, visually clear at YouTube scale"
)
DEFAULT_NEGATIVE_CONSTRAINTS = (
    "No readable text, captions, logos, trademarks, watermarks, interface screenshots, "
    "charts, diagrams, fake documents, unsupported people, unsupported products, "
    "extra limbs, distorted hands, collage borders, or baked-in TV frame."
)
SUPPORTED_QUALITY = {"low", "medium", "high"}
SUPPORTED_FORMATS = {"png", "jpeg", "webp"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric.") from exc


@dataclass(frozen=True)
class StoryboardSettings:
    planner_model: str = "gpt-5.6-luna"
    planner_reasoning_effort: str = "low"
    image_model: str = "gpt-image-2"
    image_quality: str = "medium"
    image_size: str = "1280x720"
    output_format: str = "png"
    timeout_seconds: float = 180.0
    planner_retries: int = 2
    maximum_source_characters: int = 12000
    maximum_image_requests_per_run: int = 10
    duplicate_hamming_threshold: int = 6
    archive_directory: Path | None = (
        PROJECTS_ROOT / "Assets" / "Media" / "AI Generated"
    )

    @classmethod
    def from_environment(cls) -> "StoryboardSettings":
        configured_archive = os.getenv("AI_IMAGE_ARCHIVE_DIRECTORY", "").strip()
        archive_directory = (
            Path(configured_archive).expanduser()
            if configured_archive
            else PROJECTS_ROOT / "Assets" / "Media" / "AI Generated"
        )
        if not archive_directory.is_absolute():
            archive_directory = SCRIPT_DIR / archive_directory
        return cls(
            planner_model=os.getenv(
                "STORYBOARD_MODEL",
                os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            ).strip(),
            planner_reasoning_effort=os.getenv(
                "STORYBOARD_REASONING_EFFORT",
                "low",
            ).strip().lower(),
            image_model=os.getenv(
                "OPENAI_IMAGE_MODEL",
                "gpt-image-2",
            ).strip(),
            image_quality=os.getenv(
                "OPENAI_IMAGE_QUALITY",
                "medium",
            ).strip().lower(),
            image_size=os.getenv(
                "OPENAI_IMAGE_SIZE",
                "1280x720",
            ).strip().lower(),
            output_format=os.getenv(
                "OPENAI_IMAGE_FORMAT",
                "png",
            ).strip().lower(),
            timeout_seconds=_env_float("OPENAI_IMAGE_TIMEOUT", 180.0),
            planner_retries=_env_int("STORYBOARD_API_RETRIES", 2),
            maximum_source_characters=_env_int(
                "STORYBOARD_MAX_SOURCE_CHARACTERS",
                12000,
            ),
            maximum_image_requests_per_run=_env_int(
                "MAX_IMAGE_REQUESTS_PER_RUN",
                10,
            ),
            duplicate_hamming_threshold=_env_int(
                "IMAGE_DUPLICATE_HAMMING_THRESHOLD",
                6,
            ),
            archive_directory=archive_directory.resolve(),
        )

    @property
    def width(self) -> int:
        return _parse_image_size(self.image_size)[0]

    @property
    def height(self) -> int:
        return _parse_image_size(self.image_size)[1]

    def validate(self) -> None:
        if not self.planner_model:
            raise ValueError("STORYBOARD_MODEL cannot be empty.")
        if self.planner_reasoning_effort not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("STORYBOARD_REASONING_EFFORT is invalid.")
        if not self.image_model.startswith("gpt-image-2"):
            raise ValueError(
                "Milestone 4 currently supports GPT Image 2 aliases/snapshots."
            )
        if self.image_quality not in SUPPORTED_QUALITY:
            raise ValueError("OPENAI_IMAGE_QUALITY must be low, medium, or high.")
        if self.output_format not in SUPPORTED_FORMATS:
            raise ValueError("OPENAI_IMAGE_FORMAT must be png, jpeg, or webp.")
        width, height = _parse_image_size(self.image_size)
        if width % 16 or height % 16:
            raise ValueError("GPT Image 2 dimensions must be multiples of 16.")
        pixels = width * height
        if not 655_360 <= pixels <= 8_294_400:
            raise ValueError("GPT Image 2 pixel count is outside the supported range.")
        if max(width, height) > 3840 or max(width, height) / min(width, height) > 3:
            raise ValueError("GPT Image 2 dimensions exceed an edge/ratio limit.")
        if not math.isclose(width / height, 16 / 9, rel_tol=0, abs_tol=0.002):
            raise ValueError("Milestone 4 hero images must use an exact 16:9 size.")
        if self.timeout_seconds <= 0 or self.planner_retries < 1:
            raise ValueError("API timeout/retry settings must be positive.")
        if self.maximum_source_characters < 1000:
            raise ValueError("STORYBOARD_MAX_SOURCE_CHARACTERS is too small.")
        if self.maximum_image_requests_per_run < 1:
            raise ValueError("MAX_IMAGE_REQUESTS_PER_RUN must be positive.")
        if not 0 <= self.duplicate_hamming_threshold <= 64:
            raise ValueError("Duplicate hash threshold must be between 0 and 64.")


def _descriptive_archive_name(story: dict, image: dict, source: Path) -> str:
    description = str(
        image.get("visual_concept")
        or image.get("beat_text")
        or story.get("title")
        or "technology-news-image"
    )
    words = re.findall(r"[a-z0-9]+", description.lower())[:16]
    slug = "-".join(words) or "technology-news-image"
    checksum = sha256_file(source)[:8]
    return (
        f"ai_{int(story.get('order', 0)):02d}_{int(image.get('image_index', 0)):02d}_"
        f"{slug[:110]}_{checksum}{source.suffix.lower()}"
    )


def archive_approved_images(
    storyboard: dict,
    settings: StoryboardSettings,
) -> list[dict]:
    if settings.archive_directory is None:
        return []
    settings.archive_directory.mkdir(parents=True, exist_ok=True)
    archived = []
    for story in storyboard.get("stories", []):
        for image in story.get("images", []):
            if image.get("validation", {}).get("status") != "passed":
                continue
            source = Path(image.get("output_file", ""))
            if not source.is_file():
                continue
            destination = settings.archive_directory / _descriptive_archive_name(
                story,
                image,
                source,
            )
            source_sha = sha256_file(source)
            if not destination.is_file() or sha256_file(destination) != source_sha:
                atomic_write_bytes(destination, source.read_bytes())
            record = {
                "file": str(destination.resolve()),
                "sha256": source_sha,
                "description": str(
                    image.get("visual_concept") or image.get("beat_text") or ""
                ),
                "archived_at_utc": utc_now(),
            }
            image["asset_archive"] = record
            archived.append(record)
    return archived


class SelectedBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    visual_concept: str = Field(min_length=20, max_length=700)
    image_prompt: str = Field(min_length=40, max_length=2200)
    selection_reason: str = Field(min_length=15, max_length=700)


class StoryBeatSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[SelectedBeat] = Field(min_length=2, max_length=2)


Planner = Callable[[dict, StoryboardSettings], StoryBeatSelection]
ImageGenerator = Callable[[str, StoryboardSettings], tuple[bytes, dict]]


def _parse_image_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value.strip().lower())
    if match is None:
        raise ValueError(f"Invalid image size: {value}")
    return int(match.group(1)), int(match.group(2))


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


def _require_manual_checkpoint_ready(video_dir: Path) -> tuple[dict, dict]:
    state = load_json(video_dir / "state.json", "Stage state")
    checkpoint = state.get("stages", {}).get("manual_separators", {})
    if checkpoint.get("status") != "READY":
        raise RuntimeError(
            "Manual separator checkpoint must be READY before storyboarding."
        )
    timing = load_json(video_dir / "story_timing.json", "Story timing manifest")
    quality = timing.get("quality", {})
    if quality.get("status") != "passed":
        raise RuntimeError("Story timing quality gate has not passed.")
    if not quality.get("manual_separators_validated", False):
        raise RuntimeError("Validated manual separator timing is missing.")
    return state, timing


def _write_stage(video_dir: Path, state: dict, stage: dict) -> None:
    stage["updated_at_utc"] = utc_now()
    state.setdefault("stages", {})[STAGE_NAME] = stage
    state["updated_at_utc"] = stage["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)


def _trim_source(text: str, maximum: int) -> str:
    normalized = text.strip()
    if len(normalized) <= maximum:
        return normalized
    leading = int(maximum * 0.75)
    trailing = maximum - leading
    return (
        normalized[:leading].rstrip()
        + "\n\n[...middle of source omitted for cost control...]\n\n"
        + normalized[-trailing:].lstrip()
    )


def load_story_contexts(
    video_dir: Path,
    settings: StoryboardSettings,
) -> tuple[dict, dict, list[dict]]:
    state, timing = _require_manual_checkpoint_ready(video_dir)
    project_info = load_json(video_dir / "project_info.json", "Project manifest")
    if project_info.get("status") != "complete":
        raise RuntimeError("News project is incomplete; storyboarding cannot start.")

    timing_by_id = {
        item.get("story_id"): item
        for item in timing.get("stories", [])
        if isinstance(item, dict)
    }
    contexts: list[dict] = []
    for story in sorted(
        project_info.get("stories", []),
        key=lambda item: int(item.get("order", 0)),
    ):
        story_id = story.get("story_id")
        timed = timing_by_id.get(story_id)
        if timed is None:
            raise RuntimeError(f"Timing is missing for story {story_id}.")
        narration_path = Path(story["text"])
        source_path = Path(story["original"])
        if not narration_path.is_file() or not source_path.is_file():
            raise FileNotFoundError(
                f"Narration/source pair is incomplete for story {story_id}."
            )
        narration = narration_path.read_text(encoding="utf-8").strip()
        source = source_path.read_text(encoding="utf-8").strip()
        if not narration or not source:
            raise RuntimeError(f"Narration/source text is empty for story {story_id}.")
        contexts.append(
            {
                "story_id": story_id,
                "order": int(story["order"]),
                "title": story.get("title", ""),
                "source_title": story.get("source_title", ""),
                "source_url": story.get("source_url", ""),
                "score": story.get("score"),
                "ranking_reason": story.get("ranking_reason", ""),
                "narration_file": str(narration_path),
                "source_file": str(source_path),
                "narration": narration,
                "source_text": _trim_source(
                    source,
                    settings.maximum_source_characters,
                ),
                "source_sha256": sha256_file(source_path),
                "narration_sha256": sha256_file(narration_path),
                "timing": timed,
            }
        )
    expected = int(project_info.get("expected_count", len(contexts)))
    if len(contexts) != expected:
        raise RuntimeError(
            f"Project has {len(contexts)} stories; expected {expected}."
        )
    return state, timing, contexts


def candidate_beats(context: dict) -> list[dict]:
    timing = context["timing"]
    raw_candidates = timing.get("visual_beats", [])
    if len(raw_candidates) < 2:
        raw_candidates = timing.get("sentences", [])
    candidates: list[dict] = []
    for index, beat in enumerate(raw_candidates, start=1):
        text = str(
            beat.get("important_claim")
            or beat.get("text")
            or ""
        ).strip()
        start = float(beat.get("start", 0))
        end = float(beat.get("end", start))
        if not text or end <= start:
            continue
        candidates.append(
            {
                "candidate_id": f"beat_{index:02d}",
                "beat_text": text,
                "start": round(start, 6),
                "end": round(end, 6),
                "timeline_start": round(
                    float(timing["narration"]["start"]) + start,
                    6,
                ),
                "timeline_end": round(
                    float(timing["narration"]["start"]) + end,
                    6,
                ),
                "importance": beat.get("importance", "supporting"),
                "visual_query": beat.get("visual_query", ""),
                "named_entities": beat.get("named_entities", {}),
            }
        )
    if len(candidates) < 2:
        raise RuntimeError(
            f"{context['story_id']} has fewer than two timed visual candidates."
        )
    return candidates


def story_planner_fingerprint(
    context: dict,
    candidates: list[dict],
    settings: StoryboardSettings,
) -> str:
    return json_fingerprint(
        {
            "story_id": context["story_id"],
            "source_sha256": context["source_sha256"],
            "narration_sha256": context["narration_sha256"],
            "candidates": candidates,
            "planner_model": settings.planner_model,
            "reasoning_effort": settings.planner_reasoning_effort,
            "image_model": settings.image_model,
            "image_size": settings.image_size,
            "style": DEFAULT_STYLE,
            "negative_constraints": DEFAULT_NEGATIVE_CONSTRAINTS,
        }
    )


def planning_prompt(context: dict, candidates: list[dict]) -> str:
    first_story_instruction = (
        "This is story 01, the highest-ranked story. Select the pair with the "
        "strongest thumbnail-grade visual hook while remaining strictly factual."
        if context["order"] == 1
        else "Keep the pair visually strong but subordinate to story 01."
    )
    return f"""
Create the two-image storyboard for one technology-news narration.

Return exactly two different candidate IDs from the supplied timed candidates.
The two images must communicate different moments or consequences, not two camera
angles of the same idea. Prefer concrete events, products, actions, infrastructure,
or consequences. Avoid moments that require an exact logo, exact app/UI screenshot,
exact document, chart, factual diagram, or a real person's exact likeness; those are
better served by archive media later. Do not add facts that are absent from the
source and narration. {first_story_instruction}

For each selection:
- visual_concept: a concise description of what the viewer should understand;
- image_prompt: only the factual scene description and composition details specific
  to this beat; do not include generic style or negative constraints because the
  application appends those consistently;
- selection_reason: why this beat is important and distinct from the other image.

STORY
ID: {context['story_id']}
ORDER: {context['order']}
TITLE: {context['title']}
SOURCE TITLE: {context['source_title']}
RANKING SCORE: {context['score']}
RANKING REASON: {context['ranking_reason']}

APPROVED NARRATION
{context['narration']}

ORIGINAL SOURCE TEXT
{context['source_text']}

TIMED CANDIDATES
{json.dumps(candidates, ensure_ascii=False, indent=2)}
""".strip()


def plan_story_with_openai(
    context: dict,
    settings: StoryboardSettings,
) -> StoryBeatSelection:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for AI storyboarding.")
    from openai import OpenAI

    candidates = candidate_beats(context)
    client = OpenAI(timeout=settings.timeout_seconds)
    last_error: Exception | None = None
    for attempt in range(1, settings.planner_retries + 1):
        try:
            response = client.responses.parse(
                model=settings.planner_model,
                reasoning={"effort": settings.planner_reasoning_effort},
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a rigorous visual editor for technology news. "
                            "Use only the supplied source and approved narration. "
                            "Never invent a person, product, event, number, or claim."
                        ),
                    },
                    {"role": "user", "content": planning_prompt(context, candidates)},
                ],
                text_format=StoryBeatSelection,
            )
            if response.output_parsed is None:
                raise RuntimeError("OpenAI returned no structured storyboard.")
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
            if attempt < settings.planner_retries:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"Storyboard planning failed: {last_error}") from last_error


def _normalized_words(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 4
    }


def _validate_selection(
    selection: StoryBeatSelection,
    candidates: list[dict],
) -> list[tuple[SelectedBeat, dict]]:
    by_id = {item["candidate_id"]: item for item in candidates}
    if len(selection.images) != 2:
        raise RuntimeError("The planner must return exactly two images.")
    ids = [item.candidate_id for item in selection.images]
    if len(set(ids)) != 2:
        raise RuntimeError("The planner selected the same timed beat twice.")
    selected: list[tuple[SelectedBeat, dict]] = []
    for item in selection.images:
        candidate = by_id.get(item.candidate_id)
        if candidate is None:
            raise RuntimeError(
                f"The planner returned an unknown candidate: {item.candidate_id}"
            )
        selected.append((item, candidate))

    first_words = _normalized_words(selection.images[0].visual_concept)
    second_words = _normalized_words(selection.images[1].visual_concept)
    union = first_words | second_words
    similarity = len(first_words & second_words) / max(1, len(union))
    if similarity > 0.72:
        raise RuntimeError("The two visual concepts are not distinct enough.")
    return selected


def api_image_prompt(
    *,
    context: dict,
    candidate: dict,
    selection: SelectedBeat,
    settings: StoryboardSettings,
) -> str:
    return (
        "Create one editorial still image for a technology-news YouTube video.\n\n"
        f"News beat: {candidate['beat_text']}\n"
        f"Scene to depict: {selection.image_prompt.strip()}\n"
        f"Viewer takeaway: {selection.visual_concept.strip()}\n\n"
        f"Visual style: {DEFAULT_STYLE}.\n"
        f"Composition: native {settings.image_size} 16:9 landscape. Keep the main "
        "subject inside the central 80% crop-safe area. Keep the bottom 22% calm, "
        "uncluttered, and free of important objects so subtitles remain readable. "
        "Use one clear focal point and strong depth separation.\n"
        "Factual boundary: depict only what the supplied news beat supports. Use "
        "generic unbranded devices, interfaces, buildings, and secondary people "
        "when exact real-world details cannot be verified from the beat.\n"
        f"Negative constraints: {DEFAULT_NEGATIVE_CONSTRAINTS}"
    )


def _load_existing_storyboard(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return load_json(path, "Storyboard")
    except RuntimeError:
        return {}


def build_storyboard(
    *,
    video_number: int,
    settings: StoryboardSettings | None = None,
    planner: Planner | None = None,
    story_number: int | None = None,
    force: bool = False,
) -> dict:
    settings = settings or StoryboardSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")
    state, _, contexts = load_story_contexts(video_dir, settings)
    if story_number is not None and not 1 <= story_number <= len(contexts):
        raise ValueError(f"--story must be between 1 and {len(contexts)}.")

    storyboard_dir = video_dir / "04_storyboard"
    storyboard_dir.mkdir(parents=True, exist_ok=True)
    storyboard_path = storyboard_dir / "storyboard.json"
    existing = _load_existing_storyboard(storyboard_path)
    existing_by_id = {
        item.get("story_id"): item
        for item in existing.get("stories", [])
        if isinstance(item, dict)
    }
    selected_contexts = (
        [contexts[story_number - 1]]
        if story_number is not None
        else contexts
    )
    planner = planner or plan_story_with_openai
    planned_by_id: dict[str, dict] = dict(existing_by_id)

    for context in selected_contexts:
        candidates = candidate_beats(context)
        fingerprint = story_planner_fingerprint(context, candidates, settings)
        cached = existing_by_id.get(context["story_id"])
        if (
            not force
            and cached
            and cached.get("input_fingerprint") == fingerprint
            and len(cached.get("images", [])) == 2
        ):
            planned_by_id[context["story_id"]] = cached
            continue

        selection = planner(context, settings)
        selected = _validate_selection(selection, candidates)
        images = []
        for image_index, (chosen, candidate) in enumerate(selected, start=1):
            prompt = api_image_prompt(
                context=context,
                candidate=candidate,
                selection=chosen,
                settings=settings,
            )
            extension = "jpg" if settings.output_format == "jpeg" else settings.output_format
            output_path = (
                video_dir
                / "05_visuals"
                / "generated"
                / f"{context['order']:02d}_hero_{image_index:02d}.{extension}"
            )
            images.append(
                {
                    "image_index": image_index,
                    "candidate_id": candidate["candidate_id"],
                    "beat_text": candidate["beat_text"],
                    "time_range": {
                        "start": candidate["start"],
                        "end": candidate["end"],
                        "timeline_start": candidate["timeline_start"],
                        "timeline_end": candidate["timeline_end"],
                    },
                    "aspect_ratio": "16:9",
                    "size": settings.image_size,
                    "visual_style": DEFAULT_STYLE,
                    "visual_concept": chosen.visual_concept.strip(),
                    "selection_reason": chosen.selection_reason.strip(),
                    "negative_constraints": DEFAULT_NEGATIVE_CONSTRAINTS,
                    "prompt": prompt,
                    "prompt_sha256": sha256_text(prompt),
                    "output_file": str(output_path),
                    "generation": {
                        "status": "pending",
                        "provider": "openai",
                        "model": settings.image_model,
                        "quality": settings.image_quality,
                        "seed": None,
                        "generation_id": None,
                        "request_id": None,
                    },
                    "validation": {"status": "pending"},
                }
            )
        planned_by_id[context["story_id"]] = {
            "story_id": context["story_id"],
            "order": context["order"],
            "title": context["title"],
            "source_title": context["source_title"],
            "source_url": context["source_url"],
            "score": context["score"],
            "highest_ranked": context["order"] == 1,
            "narration_file": context["narration_file"],
            "source_file": context["source_file"],
            "input_fingerprint": fingerprint,
            "planner": {
                "provider": "openai",
                "model": settings.planner_model,
                "reasoning_effort": settings.planner_reasoning_effort,
            },
            "candidate_count": len(candidates),
            "images": images,
        }

    ordered = [
        planned_by_id[context["story_id"]]
        for context in contexts
        if context["story_id"] in planned_by_id
    ]
    complete = len(ordered) == len(contexts) and all(
        len(item.get("images", [])) == 2 for item in ordered
    )
    payload = {
        "schema_version": STORYBOARD_SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "created_at_utc": existing.get("created_at_utc", utc_now()),
        "updated_at_utc": utc_now(),
        "configuration": asdict(settings),
        "story_count": len(contexts),
        "planned_story_count": len(ordered),
        "planned_image_count": sum(len(item.get("images", [])) for item in ordered),
        "stories": ordered,
        "quality": {
            "status": "passed" if complete else "partial",
            "exactly_two_images_per_planned_story": all(
                len(item.get("images", [])) == 2 for item in ordered
            ),
            "all_stories_planned": complete,
            "storyboard_saved_before_generation": True,
        },
    }
    atomic_write_json(storyboard_path, payload)
    _write_stage(
        video_dir,
        state,
        {
            "status": "STORYBOARD_READY" if complete else "PARTIAL",
            "reason": (
                "Every story has exactly two planned hero images."
                if complete
                else "A pilot subset is storyboarded."
            ),
            "storyboard_file": str(storyboard_path),
            "planned_story_count": len(ordered),
            "planned_image_count": payload["planned_image_count"],
        },
    )
    return payload


def probe_image(path: Path) -> dict:
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        raise RuntimeError("FFprobe is required for image validation.")
    completed = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"FFprobe rejected image: {completed.stderr.strip() or path.name}"
        )
    try:
        streams = json.loads(completed.stdout).get("streams", [])
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe returned invalid image metadata.") from exc
    if not streams:
        raise RuntimeError(f"No image stream found in {path.name}.")
    stream = streams[0]
    return {
        "codec": stream.get("codec_name", ""),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "pixel_format": stream.get("pix_fmt", ""),
    }


def _grayscale_grid(path: Path, width: int = 160, height: int = 90) -> bytes:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required for image validation.")
    completed = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={width}:{height},format=gray",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0 or len(completed.stdout) != width * height:
        raise RuntimeError(f"FFmpeg could not decode image pixels: {path.name}")
    return completed.stdout


def perceptual_dhash(path: Path) -> str:
    grid = _grayscale_grid(path, 9, 8)
    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | int(
                grid[offset + column] > grid[offset + column + 1]
            )
    return f"{bits:016x}"


def hash_distance(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def caption_safe_metrics(path: Path) -> dict:
    width, height = 160, 90
    grid = _grayscale_grid(path, width, height)
    start_row = int(height * 0.78)
    lower = list(grid[start_row * width :])
    horizontal_edges = [
        abs(lower[index] - lower[index - 1])
        for index in range(1, len(lower))
        if index % width
    ]
    mean_edge = statistics.fmean(horizontal_edges) if horizontal_edges else 0.0
    deviation = statistics.pstdev(lower) if len(lower) > 1 else 0.0
    status = "passed" if mean_edge <= 36 and deviation <= 74 else "warning"
    return {
        "status": status,
        "lower_22_percent_mean_luma": round(statistics.fmean(lower), 3),
        "lower_22_percent_luma_stdev": round(deviation, 3),
        "lower_22_percent_mean_horizontal_edge": round(mean_edge, 3),
        "note": (
            "Local proxy only; final caption readability is rechecked in the proof."
        ),
    }


def validate_generated_image(
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    other_hashes: list[tuple[str, str]] | None = None,
    duplicate_threshold: int = 6,
) -> dict:
    media = probe_image(path)
    errors: list[str] = []
    if (media["width"], media["height"]) != (expected_width, expected_height):
        errors.append(
            f"Resolution is {media['width']}x{media['height']}; "
            f"expected {expected_width}x{expected_height}."
        )
    dhash = perceptual_dhash(path)
    duplicate_matches = []
    for label, other in other_hashes or []:
        distance = hash_distance(dhash, other)
        if distance < duplicate_threshold:
            duplicate_matches.append(
                {"asset": label, "hamming_distance": distance}
            )
    if duplicate_matches:
        errors.append("Image is a perceptual duplicate of an approved hero image.")
    safe_area = caption_safe_metrics(path)
    return {
        "status": "passed" if not errors else "failed",
        "errors": errors,
        **media,
        "aspect_ratio": round(media["width"] / max(1, media["height"]), 6),
        "perceptual_dhash": dhash,
        "duplicate_matches": duplicate_matches,
        "caption_safe_area": safe_area,
    }


def generate_image_with_openai(
    prompt: str,
    settings: StoryboardSettings,
) -> tuple[bytes, dict]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for image generation.")
    from openai import OpenAI

    client = OpenAI(timeout=settings.timeout_seconds)
    raw = client.images.with_raw_response.generate(
        model=settings.image_model,
        prompt=prompt,
        n=1,
        size=settings.image_size,
        quality=settings.image_quality,
        output_format=settings.output_format,
        background="opaque",
    )
    response = raw.parse()
    if not response.data or not response.data[0].b64_json:
        raise RuntimeError("OpenAI returned no image bytes.")
    image_bytes = base64.b64decode(response.data[0].b64_json)
    usage = getattr(response, "usage", None)
    return image_bytes, {
        "request_id": raw.headers.get("x-request-id", ""),
        "generation_id": raw.headers.get("x-request-id", ""),
        "seed": None,
        "usage": (
            usage.model_dump(mode="json")
            if hasattr(usage, "model_dump")
            else usage
        ),
    }


def image_input_fingerprint(image: dict, settings: StoryboardSettings) -> str:
    return json_fingerprint(
        {
            "prompt_sha256": image["prompt_sha256"],
            "model": settings.image_model,
            "quality": settings.image_quality,
            "size": settings.image_size,
            "format": settings.output_format,
            "background": "opaque",
        }
    )


def _cached_image_is_current(
    image: dict,
    settings: StoryboardSettings,
) -> bool:
    generation = image.get("generation", {})
    validation = image.get("validation", {})
    path = Path(image.get("output_file", ""))
    if (
        generation.get("input_fingerprint")
        != image_input_fingerprint(image, settings)
        or generation.get("status") not in {"generated", "cached"}
        or validation.get("status") != "passed"
        or not path.is_file()
    ):
        return False
    expected_sha = generation.get("file_sha256", "")
    return bool(expected_sha) and sha256_file(path) == expected_sha


def generate_storyboard_images(
    *,
    video_number: int,
    confirm_cost: bool,
    settings: StoryboardSettings | None = None,
    generator: ImageGenerator | None = None,
    story_number: int | None = None,
    maximum_requests: int | None = None,
    force: bool = False,
) -> dict:
    settings = settings or StoryboardSettings.from_environment()
    settings.validate()
    if not confirm_cost:
        raise RuntimeError(
            "Paid image generation is blocked. Re-run with --confirm-cost after "
            "reviewing the planned request count."
        )
    video_dir = project_dir(video_number)
    state, _ = _require_manual_checkpoint_ready(video_dir)
    storyboard_path = video_dir / "04_storyboard" / "storyboard.json"
    storyboard = load_json(storyboard_path, "Storyboard")
    stories = storyboard.get("stories", [])
    if story_number is not None and not 1 <= story_number <= len(stories):
        raise ValueError(f"--story must be between 1 and {len(stories)}.")
    selected_stories = (
        [stories[story_number - 1]] if story_number is not None else stories
    )
    if not selected_stories:
        raise RuntimeError("No planned stories are available for image generation.")
    for story in selected_stories:
        if len(story.get("images", [])) != 2:
            raise RuntimeError(
                f"{story.get('story_id')} does not have exactly two planned images."
            )

    request_limit = (
        maximum_requests
        if maximum_requests is not None
        else settings.maximum_image_requests_per_run
    )
    needed = sum(
        1
        for story in selected_stories
        for image in story["images"]
        if force or not _cached_image_is_current(image, settings)
    )
    if needed > request_limit:
        raise RuntimeError(
            f"This run needs {needed} paid image requests, exceeding the limit "
            f"of {request_limit}."
        )

    generator = generator or generate_image_with_openai
    approved_hashes: list[tuple[str, str]] = []
    for story in stories:
        for image in story.get("images", []):
            validation = image.get("validation", {})
            if validation.get("status") == "passed":
                approved_hashes.append(
                    (
                        image.get("output_file", "unknown"),
                        validation.get("perceptual_dhash", ""),
                    )
                )
    approved_hashes = [(label, value) for label, value in approved_hashes if value]

    completed_requests = 0
    for story in selected_stories:
        for image in story["images"]:
            if not force and _cached_image_is_current(image, settings):
                image["generation"]["status"] = "cached"
                continue
            prompt = image["prompt"]
            output_path = Path(image["output_file"])
            fingerprint = image_input_fingerprint(image, settings)
            comparison_hashes = [
                (label, value)
                for label, value in approved_hashes
                if Path(label) != output_path
            ]
            try:
                image_bytes, provider = generator(prompt, settings)
                completed_requests += 1

                def validate_temporary(temporary: Path) -> None:
                    result = validate_generated_image(
                        temporary,
                        expected_width=settings.width,
                        expected_height=settings.height,
                        other_hashes=comparison_hashes,
                        duplicate_threshold=settings.duplicate_hamming_threshold,
                    )
                    if result["status"] != "passed":
                        raise RuntimeError("; ".join(result["errors"]))

                atomic_write_bytes(
                    output_path,
                    image_bytes,
                    validate_temporary=validate_temporary,
                )
                validation = validate_generated_image(
                    output_path,
                    expected_width=settings.width,
                    expected_height=settings.height,
                    other_hashes=comparison_hashes,
                    duplicate_threshold=settings.duplicate_hamming_threshold,
                )
                if validation["status"] != "passed":
                    raise RuntimeError("; ".join(validation["errors"]))
                approved_hashes.append(
                    (str(output_path), validation["perceptual_dhash"])
                )
                image["generation"] = {
                    "status": "generated",
                    "provider": "openai",
                    "model": settings.image_model,
                    "quality": settings.image_quality,
                    "size": settings.image_size,
                    "format": settings.output_format,
                    "input_fingerprint": fingerprint,
                    "request_id": provider.get("request_id") or None,
                    "generation_id": provider.get("generation_id") or None,
                    "seed": provider.get("seed"),
                    "usage": provider.get("usage"),
                    "generated_at_utc": utc_now(),
                    "file_sha256": sha256_file(output_path),
                    "bytes": output_path.stat().st_size,
                }
                image["validation"] = validation
                atomic_write_json(storyboard_path, storyboard)
            except Exception as exc:
                image["generation"] = {
                    **image.get("generation", {}),
                    "status": "failed",
                    "input_fingerprint": fingerprint,
                    "failed_at_utc": utc_now(),
                    "error": str(exc),
                }
                image["validation"] = {
                    "status": "failed",
                    "errors": [str(exc)],
                }
                storyboard["updated_at_utc"] = utc_now()
                atomic_write_json(storyboard_path, storyboard)
                _write_stage(
                    video_dir,
                    state,
                    {
                        "status": "FAILED",
                        "reason": str(exc),
                        "storyboard_file": str(storyboard_path),
                        "requests_completed_this_run": completed_requests,
                    },
                )
                raise

    all_ready = bool(stories) and all(
        len(story.get("images", [])) == 2
        and all(
            image.get("validation", {}).get("status") == "passed"
            and Path(image["output_file"]).is_file()
            for image in story["images"]
        )
        for story in stories
    )
    storyboard["updated_at_utc"] = utc_now()
    storyboard["generated_image_count"] = sum(
        image.get("validation", {}).get("status") == "passed"
        for story in stories
        for image in story.get("images", [])
    )
    storyboard["quality"] = {
        **storyboard.get("quality", {}),
        "all_images_generated_and_validated": all_ready,
        "status": "passed" if all_ready else "partial",
    }
    storyboard["archived_assets"] = archive_approved_images(storyboard, settings)
    atomic_write_json(storyboard_path, storyboard)
    _write_stage(
        video_dir,
        state,
        {
            "status": "VISUALS_READY" if all_ready else "PARTIAL",
            "reason": (
                "Every story has two approved hero images."
                if all_ready
                else "The requested pilot images are approved; other stories remain."
            ),
            "storyboard_file": str(storyboard_path),
            "generated_image_count": storyboard["generated_image_count"],
            "requests_completed_this_run": completed_requests,
        },
    )
    return storyboard


def _media_probe(path: Path) -> dict:
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        raise RuntimeError("FFprobe is required for proof validation.")
    completed = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"FFprobe rejected {path.name}: "
            f"{completed.stderr.strip() or 'unknown media error'}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe returned invalid proof metadata.") from exc
    video = next(
        (
            stream
            for stream in payload.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )
    audio = next(
        (
            stream
            for stream in payload.get("streams", [])
            if stream.get("codec_type") == "audio"
        ),
        None,
    )
    if video is None:
        raise RuntimeError(f"{path.name} has no video stream.")
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": video.get("codec_name", ""),
        "pixel_format": video.get("pix_fmt", ""),
        "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name", "") if audio else "",
        "duration_seconds": float(
            payload.get("format", {}).get("duration")
            or video.get("duration")
            or 0
        ),
    }


def find_temporary_archive_filler() -> Path | None:
    roots = [
        PROJECTS_ROOT / "Assets" / "Media",
        PROJECTS_ROOT / "Media (No-Copyright)",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                try:
                    media = probe_image(path)
                except RuntimeError:
                    continue
                if media["width"] >= 320 and media["height"] >= 180:
                    return path
    return None


def _run_ffmpeg(command: list[str], *, timeout: int, label: str) -> None:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{label} failed: {completed.stderr.strip() or 'unknown FFmpeg error'}"
        )


def _scale_filter(width: int, height: int, fps: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
    )


def render_storyboard_proof(
    *,
    video_number: int,
    story_number: int = 1,
    settings: StoryboardSettings | None = None,
    duration_seconds: float = 30.0,
    width: int | None = None,
    height: int | None = None,
    fps: int = 30,
    archive_filler: Path | None = None,
) -> Path:
    settings = settings or StoryboardSettings.from_environment()
    settings.validate()
    if duration_seconds <= 1:
        raise ValueError("Proof duration must exceed one second.")
    video_dir = project_dir(video_number)
    _require_manual_checkpoint_ready(video_dir)
    storyboard_path = video_dir / "04_storyboard" / "storyboard.json"
    storyboard = load_json(storyboard_path, "Storyboard")
    stories = storyboard.get("stories", [])
    if not 1 <= story_number <= len(stories):
        raise ValueError(f"--story must be between 1 and {len(stories)}.")
    story = stories[story_number - 1]
    images = story.get("images", [])
    if len(images) != 2 or not all(
        image.get("validation", {}).get("status") == "passed"
        and Path(image.get("output_file", "")).is_file()
        for image in images
    ):
        raise RuntimeError("The selected story needs two approved hero images.")

    timing = load_json(video_dir / "story_timing.json", "Story timing manifest")
    timed_story = next(
        (
            item
            for item in timing.get("stories", [])
            if item.get("story_id") == story.get("story_id")
        ),
        None,
    )
    if timed_story is None:
        raise RuntimeError("Story timing is missing for the proof.")
    separator_path = Path(timed_story["separator"].get("file", ""))
    narration_path = Path(timed_story.get("audio_file", ""))
    if not separator_path.is_file() or not narration_path.is_file():
        raise FileNotFoundError("Proof separator or narration audio is missing.")

    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required to render the proof.")
    output_width = width or settings.width
    output_height = height or settings.height
    if not math.isclose(
        output_width / output_height,
        16 / 9,
        rel_tol=0,
        abs_tol=0.002,
    ):
        raise ValueError("Proof output must be 16:9.")

    separator_media = _media_probe(separator_path)
    separator_duration = min(
        float(separator_media["duration_seconds"]),
        max(0.1, duration_seconds - 0.1),
    )
    narration_duration = float(timed_story["narration"]["duration"])
    body_duration = min(
        narration_duration,
        max(0.1, duration_seconds - separator_duration),
    )
    final_duration = separator_duration + body_duration
    archive_filler = archive_filler or find_temporary_archive_filler()
    proof_dir = video_dir / "04_storyboard"
    output_path = proof_dir / f"story_{story_number:02d}_30s_proof.mp4"

    with tempfile.TemporaryDirectory(prefix="proof_", dir=proof_dir) as temporary:
        temporary_dir = Path(temporary)
        normalized_separator = temporary_dir / "separator.mp4"
        body = temporary_dir / "body.mp4"
        scale = _scale_filter(output_width, output_height, fps)

        separator_command = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(separator_path),
        ]
        if separator_media["has_audio"]:
            separator_command.extend(
                [
                    "-filter_complex",
                    (
                        f"[0:v]{scale},trim=duration={separator_duration:.6f},"
                        "setpts=PTS-STARTPTS[v];"
                        f"[0:a]aresample=48000,aformat=channel_layouts=stereo,"
                        f"atrim=duration={separator_duration:.6f},"
                        "asetpts=PTS-STARTPTS,apad[a]"
                    ),
                    "-map",
                    "[v]",
                    "-map",
                    "[a]",
                ]
            )
        else:
            separator_command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=48000:cl=stereo",
                    "-filter_complex",
                    (
                        f"[0:v]{scale},trim=duration={separator_duration:.6f},"
                        "setpts=PTS-STARTPTS[v];"
                        f"[1:a]atrim=duration={separator_duration:.6f},"
                        "asetpts=PTS-STARTPTS[a]"
                    ),
                    "-map",
                    "[v]",
                    "-map",
                    "[a]",
                ]
            )
        separator_command.extend(
            [
                "-t",
                f"{separator_duration:.6f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(normalized_separator),
            ]
        )
        _run_ffmpeg(
            separator_command,
            timeout=max(120, int(separator_duration * 6)),
            label="Separator normalization",
        )

        if archive_filler and archive_filler.is_file():
            base_input = [
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-i",
                str(archive_filler),
            ]
            filler_record = str(archive_filler)
        else:
            base_input = [
                "-f",
                "lavfi",
                "-i",
                (
                    f"color=c=0x10131a:s={output_width}x{output_height}:"
                    f"r={fps}:d={body_duration:.6f}"
                ),
            ]
            filler_record = "solid_color_fallback"
        body_command = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *base_input,
            "-i",
            str(narration_path),
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(images[0]["output_file"]),
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(images[1]["output_file"]),
        ]
        first_start = body_duration * 0.20
        split = body_duration * 0.58
        second_end = body_duration * 0.96
        body_filter = (
            f"[0:v]{scale}[base];"
            f"[2:v]{scale}[hero1];"
            f"[3:v]{scale}[hero2];"
            f"[base][hero1]overlay=0:0:enable='between(t,{first_start:.6f},"
            f"{split:.6f})'[v1];"
            f"[v1][hero2]overlay=0:0:enable='between(t,{split:.6f},"
            f"{second_end:.6f})'[v];"
            f"[1:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"atrim=duration={body_duration:.6f},asetpts=PTS-STARTPTS,"
            f"apad,atrim=duration={body_duration:.6f}[a]"
        )
        body_command.extend(
            [
                "-filter_complex",
                body_filter,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-t",
                f"{body_duration:.6f}",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(body),
            ]
        )
        _run_ffmpeg(
            body_command,
            timeout=max(180, int(body_duration * 8)),
            label="Proof story body",
        )

        final_command = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(normalized_separator),
            "-i",
            str(body),
            "-filter_complex",
            "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{final_duration:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(output_path),
        ]
        _run_ffmpeg(
            final_command,
            timeout=max(180, int(final_duration * 8)),
            label="30-second proof render",
        )

    media = _media_probe(output_path)
    errors = []
    if (media["width"], media["height"]) != (output_width, output_height):
        errors.append("Proof resolution is incorrect.")
    if not media["has_audio"]:
        errors.append("Proof has no audio.")
    if abs(media["duration_seconds"] - final_duration) > 0.35:
        errors.append("Proof duration is outside tolerance.")
    if errors:
        raise RuntimeError(" ".join(errors))

    storyboard["proof"] = {
        "status": "passed",
        "story_id": story["story_id"],
        "story_number": story_number,
        "file": str(output_path),
        "sha256": sha256_file(output_path),
        "duration_seconds": round(media["duration_seconds"], 6),
        "separator_seconds": round(separator_duration, 6),
        "narration_seconds": round(body_duration, 6),
        "temporary_archive_filler": filler_record,
        "hero_preview_schedule": [
            {
                "image_index": 1,
                "start": round(separator_duration + first_start, 6),
                "end": round(separator_duration + split, 6),
            },
            {
                "image_index": 2,
                "start": round(separator_duration + split, 6),
                "end": round(separator_duration + second_end, 6),
            },
        ],
        "note": (
            "Proof scheduling is compressed to show both heroes. Production timing "
            "remains the exact narration ranges stored on each image."
        ),
        "media": media,
    }
    storyboard["updated_at_utc"] = utc_now()
    atomic_write_json(storyboard_path, storyboard)
    return output_path


def run_live_image_smoke(
    settings: StoryboardSettings | None = None,
    generator: ImageGenerator | None = None,
) -> dict:
    base = settings or StoryboardSettings.from_environment()
    smoke = replace(
        base,
        image_quality="low",
        image_size="1280x720",
        output_format="png",
        maximum_image_requests_per_run=1,
    )
    smoke.validate()
    generator = generator or generate_image_with_openai
    prompt = (
        "Create one restrained photorealistic editorial technology-news still: "
        "a modern unbranded data center aisle with a single technician seen from "
        "behind, cool neutral lighting, one clear focal point, native 1280x720 "
        "16:9. Keep the bottom 22% calm and empty for subtitles. No text, logos, "
        "watermarks, UI, charts, diagrams, or brand marks."
    )
    image_bytes, provider = generator(prompt, smoke)
    with tempfile.TemporaryDirectory(prefix="news_studio_image_smoke_") as directory:
        path = Path(directory) / "smoke.png"
        path.write_bytes(image_bytes)
        validation = validate_generated_image(
            path,
            expected_width=smoke.width,
            expected_height=smoke.height,
            duplicate_threshold=smoke.duplicate_hamming_threshold,
        )
        if validation["status"] != "passed":
            raise RuntimeError("; ".join(validation["errors"]))
    return {
        "status": "passed",
        "request_count": 1,
        "model": smoke.image_model,
        "quality": smoke.image_quality,
        "size": smoke.image_size,
        "request_id": provider.get("request_id") or None,
        "generation_id": provider.get("generation_id") or None,
        "seed": provider.get("seed"),
        "bytes": len(image_bytes),
        "sha256": sha256_bytes(image_bytes),
        "validation": validation,
        "temporary_image_deleted": True,
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan and generate exactly two timed AI hero images per story."
    )
    parser.add_argument("--video", type=int)
    parser.add_argument(
        "--action",
        choices=("plan", "generate", "archive", "proof", "all", "status", "smoke-image"),
        default="plan",
    )
    parser.add_argument("--story", type=int)
    parser.add_argument("--confirm-cost", action="store_true")
    parser.add_argument("--force-plan", action="store_true")
    parser.add_argument("--force-images", action="store_true")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument(
        "--quality",
        choices=tuple(sorted(SUPPORTED_QUALITY)),
        help="Override OPENAI_IMAGE_QUALITY for this run.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        settings = StoryboardSettings.from_environment()
        if args.quality:
            settings = replace(settings, image_quality=args.quality)
        if args.action == "smoke-image":
            result = run_live_image_smoke(settings)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.video is None or args.video < 1:
            raise ValueError("--video must be provided and be 1 or greater.")
        if args.action == "status":
            path = project_dir(args.video) / "04_storyboard" / "storyboard.json"
            payload = load_json(path, "Storyboard")
            summary = {
                "video_number": args.video,
                "quality": payload.get("quality", {}),
                "planned_image_count": payload.get("planned_image_count", 0),
                "generated_image_count": payload.get("generated_image_count", 0),
                "proof": payload.get("proof", {}),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.action == "archive":
            path = project_dir(args.video) / "04_storyboard" / "storyboard.json"
            payload = load_json(path, "Storyboard")
            archived = archive_approved_images(payload, settings)
            payload["archived_assets"] = archived
            payload["updated_at_utc"] = utc_now()
            atomic_write_json(path, payload)
            print(f"Archived approved images: {len(archived)}")
            print(f"Asset directory: {settings.archive_directory}")
            return 0
        if args.action in {"plan", "all"}:
            result = build_storyboard(
                video_number=args.video,
                settings=settings,
                story_number=args.story,
                force=args.force_plan,
            )
            print(
                f"Storyboard saved before generation: "
                f"{project_dir(args.video) / '04_storyboard' / 'storyboard.json'}"
            )
            print(f"Planned images: {result['planned_image_count']}")
        if args.action in {"generate", "all"}:
            result = generate_storyboard_images(
                video_number=args.video,
                confirm_cost=args.confirm_cost,
                settings=settings,
                story_number=args.story,
                maximum_requests=args.max_requests,
                force=args.force_images,
            )
            print(f"Approved images: {result.get('generated_image_count', 0)}")
        if args.action == "proof":
            path = render_storyboard_proof(
                video_number=args.video,
                story_number=args.story or 1,
                settings=settings,
            )
            print(f"Proof video: {path}")
        if args.json and args.action in {"plan", "generate", "all"}:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[STORYBOARD ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
