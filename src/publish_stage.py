from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

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
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    json_fingerprint,
    report_cli_error,
    sha256_file,
)
from file_locations import PROJECTS_ROOT, project_dir, project_name  # noqa: E402
from storyboard_stage import probe_image  # noqa: E402
from tooling import find_executable  # noqa: E402


SCHEMA_VERSION = 1
STAGE_NAME = "publish"
YOUTUBE_TITLE_LIMIT = 100
YOUTUBE_MINIMUM_CHAPTERS = 3
YOUTUBE_MINIMUM_CHAPTER_SECONDS = 10


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PublishSettings:
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "low"
    timeout_seconds: float = 120.0
    api_retries: int = 2
    width: int = 1280
    height: int = 720
    phone_width: int = 320
    phone_height: int = 180
    font_file: Path = Path("C:/Windows/Fonts/impact.ttf")
    thumbnail_image_model: str = "gpt-image-2"
    thumbnail_image_quality: str = "medium"
    thumbnail_image_size: str = "1280x720"
    thumbnail_image_format: str = "png"

    @classmethod
    def from_environment(cls) -> "PublishSettings":
        return cls(
            model=os.getenv("PUBLISH_MODEL", "gpt-5.6-luna").strip(),
            reasoning_effort=os.getenv(
                "PUBLISH_REASONING_EFFORT", "low"
            ).strip().lower(),
            timeout_seconds=float(os.getenv("PUBLISH_TIMEOUT", "120")),
            api_retries=int(os.getenv("PUBLISH_API_RETRIES", "2")),
            width=int(os.getenv("THUMBNAIL_WIDTH", "1280")),
            height=int(os.getenv("THUMBNAIL_HEIGHT", "720")),
            phone_width=int(os.getenv("THUMBNAIL_PHONE_WIDTH", "320")),
            phone_height=int(os.getenv("THUMBNAIL_PHONE_HEIGHT", "180")),
            font_file=Path(
                os.getenv("THUMBNAIL_FONT_FILE", "C:/Windows/Fonts/impact.ttf")
            ).expanduser().resolve(),
            thumbnail_image_model=os.getenv(
                "THUMBNAIL_IMAGE_MODEL",
                os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2"),
            ).strip(),
            thumbnail_image_quality=os.getenv(
                "THUMBNAIL_IMAGE_QUALITY", "medium"
            ).strip().lower(),
            thumbnail_image_size=os.getenv(
                "THUMBNAIL_IMAGE_SIZE", "1280x720"
            ).strip().lower(),
            thumbnail_image_format=os.getenv(
                "THUMBNAIL_IMAGE_FORMAT", "png"
            ).strip().lower(),
        )

    def validate(self) -> None:
        if not self.model:
            raise ValueError("PUBLISH_MODEL cannot be empty.")
        if self.reasoning_effort not in {
            "none", "low", "medium", "high", "xhigh", "max"
        }:
            raise ValueError("PUBLISH_REASONING_EFFORT is invalid.")
        if self.timeout_seconds <= 0 or self.api_retries < 1:
            raise ValueError("Publishing API retry/timeout must be positive.")
        if (self.width, self.height) != (1280, 720):
            raise ValueError("The final YouTube thumbnail must be 1280x720.")
        if self.phone_width <= 0 or self.phone_height <= 0:
            raise ValueError("Phone-preview dimensions must be positive.")
        if not self.font_file.is_file():
            raise FileNotFoundError(f"Thumbnail font is missing: {self.font_file}")
        if not self.thumbnail_image_model.startswith("gpt-image-2"):
            raise ValueError("THUMBNAIL_IMAGE_MODEL must be a GPT Image 2 model.")
        if self.thumbnail_image_quality not in {"low", "medium", "high"}:
            raise ValueError("THUMBNAIL_IMAGE_QUALITY must be low, medium, or high.")
        if self.thumbnail_image_size != "1280x720":
            raise ValueError("THUMBNAIL_IMAGE_SIZE must be 1280x720.")
        if self.thumbnail_image_format not in {"png", "jpeg", "webp"}:
            raise ValueError("THUMBNAIL_IMAGE_FORMAT must be png, jpeg, or webp.")


class TitleCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=15, max_length=YOUTUBE_TITLE_LIMIT)
    rationale: str = Field(min_length=20, max_length=400)
    clarity: int = Field(ge=1, le=10)
    curiosity: int = Field(ge=1, le=10)
    specificity: int = Field(ge=1, le=10)
    factual_risk: int = Field(ge=1, le=10)


class ThumbnailConcept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept_id: str = Field(pattern=r"^concept_[1-3]$")
    visual: str = Field(min_length=30, max_length=600)
    text_options: list[str] = Field(min_length=2, max_length=4)
    rationale: str = Field(min_length=20, max_length=400)


class PublishingCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_candidates: list[TitleCandidate] = Field(min_length=5, max_length=10)
    recommended_title: str = Field(min_length=15, max_length=YOUTUBE_TITLE_LIMIT)
    recommended_title_reason: str = Field(min_length=20, max_length=500)
    thumbnail_concepts: list[ThumbnailConcept] = Field(min_length=2, max_length=3)
    recommended_concept_id: str
    recommended_thumbnail_text: str = Field(min_length=4, max_length=24)
    lead_description: str = Field(min_length=80, max_length=650)
    episode_description: str = Field(min_length=80, max_length=650)
    tags: list[str] = Field(min_length=8, max_length=25)
    hashtags: list[str] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def validate_recommendations(self) -> "PublishingCopy":
        titles = {item.title for item in self.title_candidates}
        if self.recommended_title not in titles:
            raise ValueError("Recommended title must be one of the title candidates.")
        concepts = {item.concept_id: item for item in self.thumbnail_concepts}
        if self.recommended_concept_id not in concepts:
            raise ValueError("Recommended thumbnail concept is missing.")
        options = concepts[self.recommended_concept_id].text_options
        if self.recommended_thumbnail_text not in options:
            raise ValueError(
                "Recommended thumbnail text must belong to the selected concept."
            )
        normalized_tags = [item.strip().casefold() for item in self.tags]
        if len(normalized_tags) != len(set(normalized_tags)):
            raise ValueError("Publishing tags must be unique.")
        return self


def load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object: {path}")
    return payload


def _require_inputs(video_dir: Path) -> tuple[dict, dict, dict, dict]:
    state = load_json(video_dir / "state.json", "Stage state")
    if state.get("stages", {}).get("render", {}).get("status") != "RENDER_READY":
        raise RuntimeError("Milestone 8 must be RENDER_READY before publishing.")
    render_report = load_json(
        video_dir / "08_render" / "render_report.json", "Render report"
    )
    if render_report.get("quality", {}).get("status") != "passed":
        raise RuntimeError("The current final render must pass QA before publishing.")
    project = load_json(video_dir / "project_info.json", "Project manifest")
    timing = load_json(video_dir / "story_timing.json", "Story timing")
    storyboard = load_json(
        video_dir / "04_storyboard" / "storyboard.json", "Storyboard"
    )
    stories = sorted(project.get("stories", []), key=lambda item: int(item["order"]))
    if len(stories) < YOUTUBE_MINIMUM_CHAPTERS:
        raise RuntimeError("YouTube chapters require at least three stories.")
    if not stories or int(stories[0]["order"]) != 1:
        raise RuntimeError("Story 01 is missing from the project manifest.")
    if any(not item.get("source_url") for item in stories):
        raise RuntimeError("Every story requires a source URL for publishing.")
    return state, project, timing, storyboard


def _lead_story(project: dict, storyboard: dict) -> tuple[dict, dict, str, str]:
    stories = sorted(project["stories"], key=lambda item: int(item["order"]))
    lead = stories[0]
    highest_score = max(float(item.get("score", 0)) for item in stories)
    if float(lead.get("score", 0)) != highest_score:
        raise RuntimeError("Story 01 must be the highest-rated story.")
    planned = next(
        (
            item
            for item in storyboard.get("stories", [])
            if item.get("story_id") == lead.get("story_id")
        ),
        None,
    )
    if planned is None or planned.get("highest_ranked") is not True:
        raise RuntimeError("Lead storyboard does not mark story 01 as highest-ranked.")
    narration_path = Path(lead["text"])
    source_path = Path(lead["original"])
    narration = narration_path.read_text(encoding="utf-8").strip()
    original = source_path.read_text(encoding="utf-8").strip()
    if not narration or not original:
        raise RuntimeError("Lead narration/source text cannot be empty.")
    return lead, planned, narration, original


def _publishing_prompt(
    project: dict,
    lead: dict,
    planned: dict,
    narration: str,
    original: str,
) -> str:
    episode_stories = [
        {
            "order": int(item["order"]),
            "title": item.get("title", ""),
            "source_title": item.get("source_title", ""),
            "score": item.get("score", 0),
        }
        for item in sorted(project["stories"], key=lambda value: int(value["order"]))
    ]
    visual_concepts = [
        {
            "image_index": image.get("image_index"),
            "visual_concept": image.get("visual_concept", ""),
            "selection_reason": image.get("selection_reason", ""),
        }
        for image in planned.get("images", [])
    ]
    return f"""
Create a factual YouTube publishing-copy package for a complete technology-news video.
Story 01 is the highest-rated lead and must drive the title and thumbnail.

STRICT RULES
- Return 5 to 10 title candidates, each at most 100 characters.
- Do not make any claim stronger than the supplied original article.
- Preserve uncertainty words such as reportedly, may, or believes when needed.
- Put the important company/product/security subject early.
- No fabricated quotation, number, victim, country, or attribution.
- Thumbnail concepts must use only supported lead-story facts.
- Thumbnail text must be 2 to 4 short words, at most 24 characters.
- Thumbnail text must name the lead product, company, or concrete topic; reject
  generic clickbait such as NO CLICK NEEDED unless a named topic is also present.
- Recommended thumbnail text must complement, not fully repeat, the title.
- Scores use 10=best for clarity/curiosity/specificity and 10=highest risk for factual_risk.
- Lead description summarizes story 01. Episode description previews the other stories.
- Tags must be useful search phrases without #. Hashtags must start with #.
- Do not write chapters or source links; the local stage creates those exactly.

LEAD STORY
Title: {lead.get('title', '')}
Source title: {lead.get('source_title', '')}
Score: {lead.get('score', '')}
Ranking reason: {lead.get('ranking_reason', '')}
Approved narration:
{narration}

Original source text:
{original}

APPROVED LEAD VISUAL CONCEPTS
{json.dumps(visual_concepts, ensure_ascii=False, indent=2)}

FULL EPISODE STORY LIST
{json.dumps(episode_stories, ensure_ascii=False, indent=2)}
""".strip()


def generate_copy_with_openai(
    project: dict,
    lead: dict,
    planned: dict,
    narration: str,
    original: str,
    settings: PublishSettings,
) -> PublishingCopy:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for publishing-copy generation.")
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
                            "You are a meticulous YouTube technology-news editor. "
                            "Accuracy outranks sensationalism. Use only supplied facts."
                        ),
                    },
                    {
                        "role": "user",
                        "content": _publishing_prompt(
                            project, lead, planned, narration, original
                        ),
                    },
                ],
                text_format=PublishingCopy,
            )
            if response.output_parsed is None:
                raise RuntimeError("OpenAI returned no structured publishing copy.")
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
    raise RuntimeError(f"Publishing-copy generation failed: {last_error}") from last_error


def thumbnail_generation_prompt(
    *,
    lead: dict,
    planned: dict,
    narration: str,
    publishing_copy: PublishingCopy,
    headline: str,
) -> str:
    concept = next(
        item
        for item in publishing_copy.thumbnail_concepts
        if item.concept_id == publishing_copy.recommended_concept_id
    )
    return f"""
Create one final YouTube technology-news thumbnail with its headline rendered into
the image pixels. Output native 1280x720, 16:9.

EXACT HEADLINE TEXT (spell exactly; no other editorial headline):
{headline.upper()}

FIXED SERIES TEMPLATE: high-contrast black or near-black editorial background;
huge heavy condensed uppercase headline inside the LEFT 46%, starting near x=55 and
vertically centered; story depiction inside the RIGHT 54%; white headline letters
with exactly one supported danger/number phrase emphasized in vivid red or yellow;
crisp edges and a restrained black shadow. Keep this geometry identical across the
series and readable at phone size.

LEAD STORY: {lead.get('title', '')}
SOURCE TITLE: {lead.get('source_title', '')}
APPROVED VISUAL: {concept.visual}
APPROVED NARRATION EVIDENCE: {narration[:1800]}
STORYBOARD CONTEXT: {json.dumps(planned.get('images', []), ensure_ascii=False)[:1800]}

Depict one clear, immediately understandable focal subject grounded only in the
approved story. The scene may be dramatic and vivid but must remain bright enough to
read; avoid crushed blacks. No collage, split panels, duration badge, channel logo,
YouTube UI, watermark, TV frame, flags, weapons, fabricated numbers, unsupported
victims, or unrelated technology. Generic interface labels inside a depicted device
are acceptable, but add no second headline. Ensure the exact headline is perfectly
spelled and fully inside the crop-safe area.
""".strip()


def generate_thumbnail_with_openai(
    prompt: str, settings: PublishSettings
) -> tuple[bytes, dict]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for thumbnail generation.")
    from openai import OpenAI

    client = OpenAI(timeout=settings.timeout_seconds)
    raw = client.images.with_raw_response.generate(
        model=settings.thumbnail_image_model,
        prompt=prompt,
        n=1,
        size=settings.thumbnail_image_size,
        quality=settings.thumbnail_image_quality,
        output_format=settings.thumbnail_image_format,
        background="opaque",
    )
    response = raw.parse()
    if not response.data or not response.data[0].b64_json:
        raise RuntimeError("OpenAI returned no thumbnail image bytes.")
    usage = getattr(response, "usage", None)
    return base64.b64decode(response.data[0].b64_json), {
        "request_id": raw.headers.get("x-request-id", ""),
        "usage": (
            usage.model_dump(mode="json")
            if hasattr(usage, "model_dump")
            else usage
        ),
    }


def prepare_ai_rendered_thumbnail(
    *,
    output_dir: Path,
    lead: dict,
    planned: dict,
    narration: str,
    publishing_copy: PublishingCopy,
    headline: str,
    settings: PublishSettings,
    confirm_cost: bool,
    generator=None,
) -> Path:
    prompt = thumbnail_generation_prompt(
        lead=lead,
        planned=planned,
        narration=narration,
        publishing_copy=publishing_copy,
        headline=headline,
    )
    prompt_path = output_dir / "thumbnail_background_prompt.txt"
    atomic_write_text(prompt_path, prompt)
    extension = ".jpg" if settings.thumbnail_image_format == "jpeg" else f".{settings.thumbnail_image_format}"
    output = output_dir / f"thumbnail_ai_rendered_{_slug(headline, 48)}{extension}"
    manifest_path = output_dir / "thumbnail_generation.json"
    fingerprint = json_fingerprint(
        {
            "prompt": prompt,
            "model": settings.thumbnail_image_model,
            "quality": settings.thumbnail_image_quality,
            "size": settings.thumbnail_image_size,
            "format": settings.thumbnail_image_format,
        }
    )
    if manifest_path.is_file() and output.is_file():
        cached = load_json(manifest_path, "Thumbnail generation manifest")
        if (
            cached.get("input_fingerprint") == fingerprint
            and cached.get("file_sha256") == sha256_file(output)
        ):
            return output
    if not confirm_cost:
        raise RuntimeError(
            "One paid thumbnail image request is required. Re-run with "
            "--confirm-thumbnail-cost after reviewing the prompt."
        )
    generator = generator or generate_thumbnail_with_openai
    image_bytes, provider = generator(prompt, settings)

    def validate_temporary(path: Path) -> None:
        media = probe_image(path)
        if (int(media.get("width", 0)), int(media.get("height", 0))) != (1280, 720):
            raise RuntimeError("Generated thumbnail must be exactly 1280x720.")

    atomic_write_bytes(output, image_bytes, validate_temporary=validate_temporary)
    atomic_write_json(
        manifest_path,
        {
            "schema_version": 1,
            "created_at_utc": utc_now(),
            "input_fingerprint": fingerprint,
            "file": str(output),
            "file_sha256": sha256_file(output),
            "prompt_file": str(prompt_path),
            "model": settings.thumbnail_image_model,
            "quality": settings.thumbnail_image_quality,
            "size": settings.thumbnail_image_size,
            "format": settings.thumbnail_image_format,
            "request_id": provider.get("request_id") or None,
            "usage": provider.get("usage"),
        },
    )
    return output


def _chapter_timestamp(seconds: float) -> str:
    total = max(0, int(math.floor(seconds + 1e-6)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02d}:{secs:02d}"
        if hours
        else f"{minutes}:{secs:02d}"
    )


def _chapter_title(value: str) -> str:
    return textwrap.shorten(
        re.sub(r"\s+", " ", value).strip(),
        width=72,
        placeholder="…",
    )


def build_chapters(project: dict, timing: dict) -> list[dict]:
    timing_by_id = {
        item["story_id"]: item for item in timing.get("stories", [])
    }
    result = []
    for story in sorted(project["stories"], key=lambda item: int(item["order"])):
        timed = timing_by_id.get(story["story_id"])
        if timed is None:
            raise RuntimeError(f"Chapter timing is missing for {story['story_id']}.")
        start = float(timed["narration"]["start"])
        result.append(
            {
                "order": int(story["order"]),
                "story_id": story["story_id"],
                "start_seconds": start,
                "timestamp": _chapter_timestamp(start),
                "title": _chapter_title(story["title"]),
            }
        )
    if len(result) < YOUTUBE_MINIMUM_CHAPTERS:
        raise RuntimeError("YouTube requires at least three chapter timestamps.")
    if result[0]["timestamp"] != "0:00":
        raise RuntimeError("The first YouTube chapter must start at 0:00.")
    for first, second in zip(result, result[1:]):
        if second["start_seconds"] <= first["start_seconds"]:
            raise RuntimeError("YouTube chapters must be strictly increasing.")
        if second["start_seconds"] - first["start_seconds"] < YOUTUBE_MINIMUM_CHAPTER_SECONDS:
            raise RuntimeError("Every YouTube chapter must be at least 10 seconds.")
    return result


def chapter_block(chapters: list[dict]) -> str:
    return "\n".join(
        f"{item['timestamp']} {item['title']}" for item in chapters
    )


def build_description(copy: PublishingCopy, project: dict, chapters: list[dict]) -> str:
    sources = []
    for story in sorted(project["stories"], key=lambda item: int(item["order"])):
        sources.append(
            f"{int(story['order'])}. {story.get('source_title') or story['title']}\n"
            f"{story['source_url']}"
        )
    hashtags = " ".join(
        item if item.startswith("#") else f"#{item}" for item in copy.hashtags
    )
    return (
        f"{copy.lead_description.strip()}\n\n"
        f"{copy.episode_description.strip()}\n\n"
        f"CHAPTERS\n{chapter_block(chapters)}\n\n"
        f"SOURCES\n" + "\n\n".join(sources) + f"\n\n{hashtags}\n"
    )


def _title_score(candidate: TitleCandidate) -> float:
    return round(
        (
            candidate.clarity
            + candidate.curiosity
            + candidate.specificity
            + (11 - candidate.factual_risk)
        )
        / 4,
        2,
    )


def _filter_path(path: Path) -> str:
    value = path.resolve().as_posix().replace("'", r"\'")
    if len(value) >= 2 and value[1] == ":":
        value = value[0] + r"\:" + value[2:]
    return value


def _run(command: list[str], label: str) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2500:]
        raise RuntimeError(f"{label} failed: {detail}")


def _thumbnail_lines(value: str) -> list[str]:
    words = value.upper().split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > 13:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if not 1 <= len(lines) <= 3:
        raise RuntimeError("Thumbnail text must fit in one to three short lines.")
    return lines


def render_thumbnail_variants(
    output_dir: Path,
    background: Path,
    thumbnail_text: str,
    settings: PublishSettings,
    *,
    text_rendering: Literal["local_overlay", "ai_rendered"] = "local_overlay",
) -> dict:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required for thumbnail rendering.")
    if not background.is_file():
        raise FileNotFoundError(f"Thumbnail background is missing: {background}")
    lines = _thumbnail_lines(thumbnail_text)
    text_dir = output_dir / "thumbnail_text"
    text_dir.mkdir(parents=True, exist_ok=True)
    for index, line in enumerate(lines, start=1):
        atomic_write_text(text_dir / f"line_{index}.txt", line)
    font_size = 92 if len(lines) <= 2 else 76
    line_step = font_size + 18
    total_height = len(lines) * line_step
    first_y = max(190, round((settings.height - total_height) / 2))
    variants = []
    for variant, font_color, panel_opacity, color_filter in (
        ("v1", "white", 0.64, None),
        ("v2", "0xFFD54F", 0.56, "eq=contrast=1.04:saturation=1.08"),
    ):
        output = output_dir / f"thumbnail_{variant}.png"
        filters = [
            (
                f"scale={settings.width}:{settings.height}:"
                "force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={settings.width}:{settings.height}"
            )
        ]
        if text_rendering == "local_overlay":
            filters.extend(
                [
                    (
                        f"drawbox=x=32:y=135:w=590:h=450:"
                        f"color=black@{panel_opacity}:t=fill"
                    ),
                    "drawbox=x=48:y=165:w=14:h=390:color=0xE53935@1.0:t=fill",
                ]
            )
            for index in range(1, len(lines) + 1):
                filters.append(
                    "drawtext="
                    f"fontfile='{_filter_path(settings.font_file)}':"
                    f"textfile='{_filter_path(text_dir / f'line_{index}.txt')}':"
                    f"fontcolor={font_color}:fontsize={font_size}:"
                    "borderw=5:bordercolor=black@0.95:shadowx=4:shadowy=4:"
                    f"x=82:y={first_y + (index - 1) * line_step}"
                )
        elif text_rendering == "ai_rendered" and color_filter:
            filters.append(color_filter)
        _run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error",
                "-i", str(background), "-vf", ",".join(filters),
                "-frames:v", "1", "-y", str(output),
            ],
            f"Render thumbnail {variant}",
        )
        probe = probe_image(output)
        if int(probe.get("width", 0)) != settings.width or int(probe.get("height", 0)) != settings.height:
            raise RuntimeError(f"Thumbnail {variant} is not 1280x720.")
        variants.append(
            {
                "variant": variant,
                "file": str(output),
                "sha256": sha256_file(output),
                "width": settings.width,
                "height": settings.height,
                "font_color": font_color,
                "text": thumbnail_text,
                "text_lines": lines,
                "text_rendering": text_rendering,
            }
        )
    final = output_dir / "thumbnail_final.png"
    shutil.copy2(Path(variants[0]["file"]), final)
    phone = output_dir / "thumbnail_phone_preview_320x180.png"
    _run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error",
            "-i", str(final), "-vf",
            f"scale={settings.phone_width}:{settings.phone_height}:flags=lanczos",
            "-frames:v", "1", "-y", str(phone),
        ],
        "Render phone-size thumbnail preview",
    )
    contact = output_dir / "thumbnail_variants_contact_sheet.png"
    _run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error",
            "-i", variants[0]["file"], "-i", variants[1]["file"],
            "-filter_complex",
            "[0:v]scale=640:360[a];[1:v]scale=640:360[b];[a][b]hstack=2[out]",
            "-map", "[out]", "-frames:v", "1", "-y", str(contact),
        ],
        "Render thumbnail contact sheet",
    )
    return {
        "alignment_template": {
            "template_id": (
                "tech_reports_ai_headline_v1"
                if text_rendering == "ai_rendered"
                else "tech_news_left_headline_v1"
            ),
            "canvas": {"width": 1280, "height": 720},
            "headline_region": {
                "side": "left",
                "maximum_width_percent": 46,
                "text_origin": {"x": 55, "vertical_alignment": "center"},
                "maximum_lines": 3,
            },
            "subject_region": {"side": "right", "target_width_percent": 54},
            "palette": {
                "background": "black_or_near_black",
                "primary_text": "white",
                "single_emphasis": "red_or_yellow",
            },
            "text_rendering": text_rendering,
            "font_file": str(settings.font_file),
            "fixed_outline_pixels": 5,
            "fixed_shadow_offset": {"x": 4, "y": 4},
        },
        "variants": variants,
        "selected_variant": "v1",
        "final_file": str(final),
        "final_sha256": sha256_file(final),
        "phone_preview_file": str(phone),
        "phone_preview_sha256": sha256_file(phone),
        "contact_sheet_file": str(contact),
    }


def _slug(value: str, maximum: int = 72) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (normalized[:maximum].rstrip("-") or "tech-news-thumbnail")


def archive_thumbnail_asset(
    *,
    video_number: int,
    thumbnails: dict,
    publishing_copy: PublishingCopy,
    lead: dict,
    background: Path,
    background_prompt: str,
    thumbnail_text: str,
) -> dict:
    final = Path(thumbnails["final_file"])
    checksum = sha256_file(final)
    destination_dir = PROJECTS_ROOT / "Assets" / "Media" / "Thumbnails"
    destination_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"youtube_thumbnail_video-{video_number}_"
        f"{_slug(publishing_copy.recommended_title)}_{checksum[:8]}.png"
    )
    destination = destination_dir / filename
    if destination.is_file() and sha256_file(destination) != checksum:
        raise RuntimeError(f"Thumbnail archive collision: {destination}")
    if not destination.is_file():
        shutil.copy2(final, destination)
    metadata_path = destination.with_suffix(".metadata.json")
    rendering = thumbnails["alignment_template"]["text_rendering"]
    description = (
        f"Technology-news thumbnail for '{lead['title']}', using the fixed "
        "series layout with the headline on the left and the main subject on "
        f"the right. Text rendering: {rendering}."
    )
    metadata = {
        "schema_version": 1,
        "asset_type": "youtube_thumbnail",
        "video_number": video_number,
        "description": description,
        "youtube_title": publishing_copy.recommended_title,
        "thumbnail_text": thumbnail_text,
        "lead_story_id": lead["story_id"],
        "lead_story_title": lead["title"],
        "lead_source_title": lead.get("source_title", ""),
        "lead_source_url": lead["source_url"],
        "background_file": str(background),
        "background_sha256": sha256_file(background),
        "background_generation_prompt": background_prompt,
        "generation_provider": (
            "openai_api"
            if (background.parent / "thumbnail_generation.json").is_file()
            else "built_in_image_generation"
        ),
        "text_rendering": thumbnails["alignment_template"]["text_rendering"],
        "alignment_template": thumbnails["alignment_template"],
        "width": 1280,
        "height": 720,
        "sha256": checksum,
        "created_at_utc": utc_now(),
    }
    atomic_write_json(metadata_path, metadata)
    return {
        "file": str(destination),
        "metadata_file": str(metadata_path),
        "sha256": checksum,
        "description": metadata["description"],
    }


def _copy_grounding_quality(copy: PublishingCopy, lead: dict, narration: str) -> dict:
    evidence = f"{lead.get('source_title', '')} {narration}".casefold()
    title_tokens = {
        item
        for item in re.findall(r"[a-z0-9]+", copy.recommended_title.casefold())
        if len(item) >= 4
    }
    supported = sorted(token for token in title_tokens if token in evidence)
    return {
        "recommended_title_characters": len(copy.recommended_title),
        "supported_title_tokens": supported,
        "minimum_supported_terms": len(supported) >= 2,
    }


def build_publish_package(
    *,
    video_number: int,
    background: Path | None = None,
    settings: PublishSettings | None = None,
    confirm_ai_cost: bool = False,
    generate_thumbnail: bool = False,
    confirm_thumbnail_cost: bool = False,
    force_copy: bool = False,
    thumbnail_text_rendering: Literal["local_overlay", "ai_rendered"] = "local_overlay",
    rendered_headline: str | None = None,
) -> dict:
    settings = settings or PublishSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    state, project, timing, storyboard = _require_inputs(video_dir)
    lead, planned, narration, original = _lead_story(project, storyboard)
    output_dir = video_dir / "09_publish"
    output_dir.mkdir(parents=True, exist_ok=True)
    copy_path = output_dir / "editorial_copy.json"
    copy_input_fingerprint = json_fingerprint(
        {
            "lead_story": lead,
            "lead_storyboard": planned,
            "narration": narration,
            "original": original,
            "episode_stories": project["stories"],
            "model": settings.model,
            "reasoning_effort": settings.reasoning_effort,
        }
    )
    publishing_copy: PublishingCopy | None = None
    if copy_path.is_file() and not force_copy:
        cached = load_json(copy_path, "Editorial publishing copy")
        if cached.get("input_fingerprint") == copy_input_fingerprint:
            publishing_copy = PublishingCopy.model_validate(cached["copy"])
    if publishing_copy is None:
        if not confirm_ai_cost:
            raise RuntimeError(
                "Publishing copy needs one paid text request. Re-run with "
                "--confirm-ai-cost, or keep a current cached editorial_copy.json."
            )
        publishing_copy = generate_copy_with_openai(
            project, lead, planned, narration, original, settings
        )
        atomic_write_json(
            copy_path,
            {
                "schema_version": SCHEMA_VERSION,
                "created_at_utc": utc_now(),
                "input_fingerprint": copy_input_fingerprint,
                "provider": "openai",
                "model": settings.model,
                "reasoning_effort": settings.reasoning_effort,
                "copy": publishing_copy.model_dump(mode="json"),
            },
        )
    thumbnail_headline = (
        rendered_headline.strip()
        if rendered_headline and rendered_headline.strip()
        else publishing_copy.recommended_thumbnail_text
    )
    if len(thumbnail_headline) > 24:
        raise RuntimeError("Thumbnail headline must contain at most 24 characters.")
    if generate_thumbnail:
        thumbnail_text_rendering = "ai_rendered"
        background = prepare_ai_rendered_thumbnail(
            output_dir=output_dir,
            lead=lead,
            planned=planned,
            narration=narration,
            publishing_copy=publishing_copy,
            headline=thumbnail_headline,
            settings=settings,
            confirm_cost=confirm_thumbnail_cost,
        )
    if background is None:
        raise RuntimeError(
            "Provide --background or use --generate-thumbnail with explicit cost "
            "confirmation when no cached thumbnail exists."
        )
    if (
        thumbnail_text_rendering == "ai_rendered"
        and not rendered_headline
        and not generate_thumbnail
    ):
        raise RuntimeError("AI-rendered thumbnails require --rendered-headline.")

    chapters = build_chapters(project, timing)
    chapters_text = chapter_block(chapters)
    description = build_description(publishing_copy, project, chapters)
    if chapters_text not in description:
        raise RuntimeError("Description is missing its exact YouTube chapter block.")
    atomic_write_text(output_dir / "title.txt", publishing_copy.recommended_title)
    atomic_write_text(output_dir / "description.txt", description)
    atomic_write_text(output_dir / "chapters.txt", chapters_text + "\n")
    atomic_write_text(output_dir / "tags.txt", ", ".join(publishing_copy.tags) + "\n")
    atomic_write_text(
        output_dir / "hashtags.txt", " ".join(publishing_copy.hashtags) + "\n"
    )
    background = background.resolve()
    thumbnails = render_thumbnail_variants(
        output_dir,
        background,
        thumbnail_headline,
        settings,
        text_rendering=thumbnail_text_rendering,
    )
    background_prompt_path = output_dir / "thumbnail_background_prompt.txt"
    background_prompt = (
        background_prompt_path.read_text(encoding="utf-8").strip()
        if background_prompt_path.is_file()
        else ""
    )
    thumbnail_archive = archive_thumbnail_asset(
        video_number=video_number,
        thumbnails=thumbnails,
        publishing_copy=publishing_copy,
        lead=lead,
        background=background,
        background_prompt=background_prompt,
        thumbnail_text=thumbnail_headline,
    )
    grounding = _copy_grounding_quality(publishing_copy, lead, narration)
    candidates = [
        {**item.model_dump(mode="json"), "overall_score": _title_score(item)}
        for item in publishing_copy.title_candidates
    ]
    quality = {
        "status": "passed",
        "story_01_is_highest_rated": True,
        "title_candidate_count": len(candidates),
        "recommended_title_within_100_characters": len(publishing_copy.recommended_title) <= YOUTUBE_TITLE_LIMIT,
        "recommended_title_is_candidate": any(
            item["title"] == publishing_copy.recommended_title for item in candidates
        ),
        "recommended_title_grounded": grounding["minimum_supported_terms"],
        "chapter_count": len(chapters),
        "chapters_start_at_zero": chapters[0]["timestamp"] == "0:00",
        "chapters_strictly_increasing": all(
            second["start_seconds"] > first["start_seconds"]
            for first, second in zip(chapters, chapters[1:])
        ),
        "description_contains_exact_chapter_block": chapters_text in description,
        "source_count": len(project["stories"]),
        "all_sources_have_urls": all(item.get("source_url") for item in project["stories"]),
        "thumbnail_dimensions_1280x720": all(
            item["width"] == 1280 and item["height"] == 720
            for item in thumbnails["variants"]
        ),
        "phone_preview_created": Path(thumbnails["phone_preview_file"]).is_file(),
        "fixed_thumbnail_alignment_recorded": thumbnails["alignment_template"][
            "template_id"
        ] in {"tech_news_left_headline_v1", "tech_reports_ai_headline_v1"},
        "thumbnail_archived_with_metadata": (
            Path(thumbnail_archive["file"]).is_file()
            and Path(thumbnail_archive["metadata_file"]).is_file()
        ),
    }
    if not all(
        value
        for key, value in quality.items()
        if key not in {"title_candidate_count", "chapter_count", "source_count"}
    ):
        raise RuntimeError(f"Milestone 9 quality failed: {quality}")
    package = {
        "schema_version": SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "created_at_utc": utc_now(),
        "configuration": {
            **asdict(settings),
            "font_file": str(settings.font_file),
        },
        "lead_story": {
            "story_id": lead["story_id"],
            "order": int(lead["order"]),
            "title": lead["title"],
            "source_title": lead.get("source_title", ""),
            "source_url": lead["source_url"],
            "score": lead.get("score"),
            "ranking_reason": lead.get("ranking_reason", ""),
            "narration_sha256": sha256_file(Path(lead["text"])),
            "original_sha256": sha256_file(Path(lead["original"])),
        },
        "title_candidates": candidates,
        "recommended_title": publishing_copy.recommended_title,
        "recommended_title_reason": publishing_copy.recommended_title_reason,
        "thumbnail_concepts": [
            item.model_dump(mode="json") for item in publishing_copy.thumbnail_concepts
        ],
        "recommended_concept_id": publishing_copy.recommended_concept_id,
        "recommended_thumbnail_text": thumbnail_headline,
        "thumbnail_text_rendering": thumbnail_text_rendering,
        "thumbnail_background": {
            "file": str(background),
            "sha256": sha256_file(background),
            "source": (
                "built_in_image_generation_with_embedded_headline"
                if thumbnail_text_rendering == "ai_rendered"
                else "built_in_image_generation_using_approved_story_01_reference"
            ),
        },
        "thumbnails": thumbnails,
        "thumbnail_asset_archive": thumbnail_archive,
        "chapters": chapters,
        "chapter_block": chapters_text,
        "description_file": str(output_dir / "description.txt"),
        "description_sha256": sha256_file(output_dir / "description.txt"),
        "title_file": str(output_dir / "title.txt"),
        "tags": publishing_copy.tags,
        "hashtags": publishing_copy.hashtags,
        "sources": [
            {
                "order": int(item["order"]),
                "title": item.get("source_title") or item["title"],
                "url": item["source_url"],
            }
            for item in sorted(project["stories"], key=lambda value: int(value["order"]))
        ],
        "grounding": grounding,
        "quality": quality,
    }
    package["input_fingerprint"] = json_fingerprint(
        {
            "copy": sha256_file(copy_path),
            "project": sha256_file(video_dir / "project_info.json"),
            "timing": sha256_file(video_dir / "story_timing.json"),
            "render": sha256_file(video_dir / "08_render" / "render_report.json"),
            "background": sha256_file(background),
            "thumbnail_headline": thumbnail_headline,
            "thumbnail_text_rendering": thumbnail_text_rendering,
            "settings": package["configuration"],
        }
    )
    final_video = video_dir / "08_render" / f"{project_name(video_number)}_final.mp4"
    upload_manifest = {
        "schema_version": 1,
        "provider": "youtube_data_api_v3",
        "video_number": video_number,
        "video_file": str(final_video),
        "video_sha256": sha256_file(final_video),
        "thumbnail_file": thumbnails["final_file"],
        "thumbnail_sha256": thumbnails["final_sha256"],
        "metadata": {
            "snippet": {
                "title": publishing_copy.recommended_title,
                "description": description,
                "tags": publishing_copy.tags,
                "categoryId": "28",
                "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
                "embeddable": True,
                "license": "youtube",
            },
        },
        "upload": {
            "mode": "resumable",
            "chunk_size_bytes": 8388608,
            "notify_subscribers": False,
            "explicit_confirmation_required": True,
            "thumbnail_after_video_insert": True,
        },
        "chapters_embedded_in_description": chapter_block(chapters) in description,
        "created_at_utc": utc_now(),
    }
    upload_manifest_path = output_dir / "youtube_upload_manifest.json"
    atomic_write_json(upload_manifest_path, upload_manifest)
    package["youtube_upload_manifest_file"] = str(upload_manifest_path)
    package["youtube_upload_ready"] = True
    package_path = output_dir / "publish_package.json"
    atomic_write_json(package_path, package)
    state.setdefault("stages", {})[STAGE_NAME] = {
        "status": "PUBLISH_READY",
        "publish_package_file": str(package_path),
        "title": publishing_copy.recommended_title,
        "thumbnail_file": thumbnails["final_file"],
        "description_file": str(output_dir / "description.txt"),
        "chapter_count": len(chapters),
        "input_fingerprint": package["input_fingerprint"],
        "updated_at_utc": utc_now(),
    }
    state["updated_at_utc"] = state["stages"][STAGE_NAME]["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)
    return package


def publish_status(video_number: int) -> dict:
    video_dir = project_dir(video_number)
    state = load_json(video_dir / "state.json", "Stage state")
    stage = state.get("stages", {}).get(STAGE_NAME, {})
    package_path = video_dir / "09_publish" / "publish_package.json"
    ready = stage.get("status") == "PUBLISH_READY" and package_path.is_file()
    return {
        "status": "PUBLISH_READY" if ready else stage.get("status", "NOT_STARTED"),
        "publish_package_file": str(package_path),
        "title": stage.get("title"),
        "thumbnail_file": stage.get("thumbnail_file"),
        "description_file": stage.get("description_file"),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the Milestone 9 YouTube publishing package."
    )
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument(
        "--action", choices=("generate", "status"), default="generate"
    )
    parser.add_argument("--background", type=Path)
    parser.add_argument("--confirm-ai-cost", action="store_true")
    parser.add_argument("--generate-thumbnail", action="store_true")
    parser.add_argument("--confirm-thumbnail-cost", action="store_true")
    parser.add_argument("--force-copy", action="store_true")
    parser.add_argument(
        "--thumbnail-text-rendering",
        choices=("local_overlay", "ai_rendered"),
        default="local_overlay",
    )
    parser.add_argument("--rendered-headline")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.video < 1:
            raise ValueError("--video must be 1 or greater.")
        if args.action == "status":
            result = publish_status(args.video)
        else:
            result = build_publish_package(
                video_number=args.video,
                background=args.background,
                confirm_ai_cost=args.confirm_ai_cost,
                generate_thumbnail=args.generate_thumbnail,
                confirm_thumbnail_cost=args.confirm_thumbnail_cost,
                force_copy=args.force_copy,
                thumbnail_text_rendering=args.thumbnail_text_rendering,
                rendered_headline=args.rendered_headline,
            )
        if args.json or args.action == "status":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Title: {result['recommended_title']}")
            print(f"Thumbnail: {result['thumbnails']['final_file']}")
            print(f"Description: {result['description_file']}")
            print("Milestone 9: PUBLISH_READY")
        return 0
    except Exception as exc:  # noqa: BLE001
        report_cli_error("PUBLISH", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
