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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


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
from file_locations import ASSETS_ROOT, PROJECTS_ROOT, project_dir, project_name, resolve_portable_path  # noqa: E402
from storyboard_stage import probe_image  # noqa: E402
from tooling import find_executable  # noqa: E402
from youtube_upload import LEGACY_TITLE_SUFFIX, format_youtube_title, next_publish_slot  # noqa: E402
from youtube_schedule import SCHEDULE_CONFIG  # noqa: E402


SCHEMA_VERSION = 1
STAGE_NAME = "publish"
YOUTUBE_TITLE_LIMIT = 100
EDITORIAL_TITLE_LIMIT = 65
YOUTUBE_MINIMUM_CHAPTERS = 3
YOUTUBE_MINIMUM_CHAPTER_SECONDS = 10
THUMBNAIL_TEXT_LIMIT = 22
PUBLISHING_PROMPT_VERSION = 3


def validate_custom_youtube_title(value: object) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if compact.casefold().endswith(LEGACY_TITLE_SUFFIX.casefold()):
        compact = compact[: -len(LEGACY_TITLE_SUFFIX)].rstrip()
    if not compact:
        raise ValueError("YouTube title cannot be empty.")
    if len(compact) > YOUTUBE_TITLE_LIMIT:
        raise ValueError(
            f"YouTube title must be {YOUTUBE_TITLE_LIMIT} characters or fewer."
        )
    return format_youtube_title(compact)


def _normalize_editorial_title(value: object) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip(" -|:;,")
    if len(compact) <= EDITORIAL_TITLE_LIMIT:
        return compact
    shortened = compact[: EDITORIAL_TITLE_LIMIT + 1].rsplit(" ", 1)[0]
    return shortened.strip(" -|:;,") or compact[:EDITORIAL_TITLE_LIMIT].strip()


def _normalize_thumbnail_text(value: object) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip(" -|:;,.")
    if not compact:
        return "TECH NEWS"
    selected: list[str] = []
    for word in compact.split():
        candidate = " ".join([*selected, word])
        if len(selected) >= 4 or len(candidate) > THUMBNAIL_TEXT_LIMIT:
            break
        selected.append(word)
    result = " ".join(selected)
    if not result:
        result = compact[:THUMBNAIL_TEXT_LIMIT].strip(" -|:;,.")
    if len(result) < 4:
        result = f"{result} NEWS".strip()[:THUMBNAIL_TEXT_LIMIT]
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PublishSettings:
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "medium"
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
                "PUBLISH_REASONING_EFFORT", "medium"
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

    title: str = Field(min_length=15, max_length=EDITORIAL_TITLE_LIMIT)
    rationale: str = Field(min_length=20, max_length=400)
    clarity: int = Field(ge=1, le=10)
    curiosity: int = Field(ge=1, le=10)
    specificity: int = Field(ge=1, le=10)
    factual_risk: int = Field(ge=1, le=10)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> str:
        return _normalize_editorial_title(value)


class ThumbnailConcept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept_id: str = Field(pattern=r"^concept_[1-3]$")
    visual: str = Field(min_length=30, max_length=600)
    text_options: list[str] = Field(min_length=2, max_length=4)
    rationale: str = Field(min_length=20, max_length=400)

    @field_validator("text_options", mode="before")
    @classmethod
    def normalize_text_options(cls, value: object) -> list[str]:
        raw_options = value if isinstance(value, list) else [value]
        options: list[str] = []
        for raw in raw_options:
            normalized = _normalize_thumbnail_text(raw)
            if normalized.casefold() not in {
                option.casefold() for option in options
            }:
                options.append(normalized)
        for fallback in ("TECH NEWS", "TECH ALERT"):
            if len(options) >= 2:
                break
            if fallback.casefold() not in {option.casefold() for option in options}:
                options.append(fallback)
        return options[:4]


class PublishingCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_candidates: list[TitleCandidate] = Field(min_length=5, max_length=10)
    recommended_title: str = Field(min_length=15, max_length=EDITORIAL_TITLE_LIMIT)
    recommended_title_reason: str = Field(min_length=20, max_length=500)
    thumbnail_concepts: list[ThumbnailConcept] = Field(min_length=2, max_length=3)
    recommended_concept_id: str
    recommended_thumbnail_text: str = Field(
        min_length=4,
        max_length=THUMBNAIL_TEXT_LIMIT,
    )
    lead_description: str = Field(min_length=80, max_length=650)
    episode_description: str = Field(min_length=80, max_length=650)
    tags: list[str] = Field(min_length=8, max_length=25)
    hashtags: list[str] = Field(min_length=3, max_length=5)

    @field_validator("recommended_thumbnail_text", mode="before")
    @classmethod
    def normalize_recommended_thumbnail_text(cls, value: object) -> str:
        return _normalize_thumbnail_text(value)

    @field_validator("recommended_title", mode="before")
    @classmethod
    def normalize_recommended_title(cls, value: object) -> str:
        return _normalize_editorial_title(value)

    @model_validator(mode="after")
    def validate_recommendations(self) -> "PublishingCopy":
        titles = {item.title: item for item in self.title_candidates}
        if self.recommended_title not in titles:
            normalized_recommendation = self.recommended_title.strip().casefold()
            normalized_match = next(
                (
                    title
                    for title in titles
                    if title.strip().casefold() == normalized_recommendation
                ),
                None,
            )
            self.recommended_title = normalized_match or max(
                self.title_candidates,
                key=lambda item: (
                    item.clarity + item.curiosity + item.specificity
                    - item.factual_risk
                ),
            ).title
        concepts = {item.concept_id: item for item in self.thumbnail_concepts}
        if self.recommended_concept_id not in concepts:
            self.recommended_concept_id = self.thumbnail_concepts[0].concept_id
        options = concepts[self.recommended_concept_id].text_options
        if self.recommended_thumbnail_text not in options:
            normalized_text = self.recommended_thumbnail_text.strip().casefold()
            normalized_match = next(
                (
                    option
                    for option in options
                    if option.strip().casefold() == normalized_text
                ),
                None,
            )
            self.recommended_thumbnail_text = normalized_match or options[0]
        unique_tags: list[str] = []
        seen_tags: set[str] = set()
        for tag in self.tags:
            normalized = tag.strip().casefold()
            if normalized and normalized not in seen_tags:
                seen_tags.add(normalized)
                unique_tags.append(tag.strip())
        for fallback_tag in (
            "technology news",
            "tech reports",
            "technology update",
            "tech industry",
            "digital technology",
            "latest tech news",
            "AI news",
            "cybersecurity news",
        ):
            if len(unique_tags) >= 8:
                break
            normalized = fallback_tag.casefold()
            if normalized not in seen_tags:
                seen_tags.add(normalized)
                unique_tags.append(fallback_tag)
        self.tags = unique_tags
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
    selection = project.get("lead_story_selection", {})
    if project.get("lead_story_selection_required") and (
        not isinstance(selection, dict)
        or selection.get("status") != "selected"
        or selection.get("selected_story_id") != lead.get("story_id")
    ):
        raise RuntimeError("Story 01 does not match the saved editorial lead choice.")
    planned = next(
        (
            item
            for item in storyboard.get("stories", [])
            if item.get("story_id") == lead.get("story_id")
        ),
        None,
    )
    if planned is None or not (
        planned.get("editorial_lead") is True
        or planned.get("highest_ranked") is True
    ):
        raise RuntimeError("Lead storyboard does not mark story 01 as the editorial lead.")
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
Story 01 is the editorially selected lead and must drive the title and thumbnail.

STRICT RULES
- Return 5 to 10 genuinely different title candidates. Target 5 to 14 words and
  35 to 63 characters; 65 characters is the hard maximum.
- Use sentence case, capitalizing only the first word and proper names. Write a
  conversational mini-story, not a compressed article headline: establish the concrete
  subject, state the surprising change, then expose or tease the consequence, conflict,
  reversal, scale, or personal stake. A specific company/product may lead, but a clear
  frame such as "This new startup" or "The safest way to..." may create the curiosity
  gap when it stays accurate and the thumbnail supplies the missing brand identity.
- Use one deliberate ellipsis as the signature ending in most candidates. Put it at the
  end by default. In at most 1 or 2 candidates, move it into the middle only at a true
  narrative pivot before a contrasting payoff, as in "[Company] is back... and
  [Stake]". Never split an ordinary subject from its verb, such as "Police arrest...
  after". A question may end with "...?". Never use multiple ellipses or let punctuation
  hide a vague, generic title.
- Reference-derived structural patterns include "[Company] is back... and [Stake]",
  "Did [Company] just [Consequence]...?", "The [Trusted Thing] was just [Reversal]...",
  "[Company] gets [Consequence], again...", "This [Specific Subject] finally [Concrete
  Action]...", and "The most [Supported Superlative] [Thing] in history...". Use the
  rhythm, pacing, and curiosity mechanics, not these placeholder words.
- Words such as "just", "finally", "again", "killed", "terrified", "safest", or
  "most controversial" require direct support from the supplied facts. Never fabricate
  first-person reporting or personal experience. Use a central supported number when it
  creates scale or surprise. Quotation marks may isolate one short, supported, contested,
  or ironic phrase; never invent a quotation.
- STYLE REFERENCE TITLES—study their mechanics, but never copy an entity, number, event,
  or claim unless it is independently supported by the supplied lead story:
  "DeepSeek is back... and Silicon Valley is terrified"
  "The summer Math fell to the machines..."
  "This new startup can query anywhere you've been..."
  "Meta's new model wants 'deep access' to your personal life..."
  "I spent 3 days at MIT... the robot hype is worse than you think"
  "The safest way to store Bitcoin was just hacked..."
  "Did Anthropic just kill the indie hacker...?"
  "The most interesting 'hack' in history..."
  "Open-weight AI just hit 2.8 trillion parameters..."
  "This $12 billion startup finally shipped something..."
  "OpenAI gets sued for stealing, again..."
  "The most controversial rewrite in history just shipped..."
  "Claude is definitely not conscious..."
- Avoid article-summary phrasing and tired wrappers such as "What We Know",
  "Explained", "Inside", "Everything You Need to Know", "Tech News", or "Update".
- Do not make any claim stronger than the supplied original article.
- Preserve uncertainty words such as reportedly, may, or believes when needed.
- Put a concrete company, product, person, object, or user stake early; do not bury the
  subject merely to manufacture mystery.
- No fabricated quotation, number, victim, country, or attribution.
- Thumbnail concepts must use only supported lead-story facts and follow this fixed
  editorial system: a flat soft warm charcoal-brown background matching #231F20
  (RGB 35, 31, 32), oversized uppercase words on the left,
  a recognizable key person on the right, the relevant company/product logo near the
  text, and one dominant word or compact payoff phrase designed for vivid red.
- Every concept.visual must identify the key person and logo to depict. Prefer the most
  important named founder, executive, researcher, creator, or public figure who is
  materially connected to the lead event. If nobody is named, a widely recognized
  founder, CEO, or public face of the primary mentioned company may serve as a neutral
  brand-associated visual anchor. State that role in concept.visual and never pose them
  as an offender, victim, witness, or decision-maker unless the evidence supports it.
  If no defensible person is available, use the unmistakable product, device, robot, or
  physical subject instead; never invent or guess an identity. Two people are allowed
  only for a supported direct clash.
- Treat the person's pose and expression as story-specific art direction, not a fixed
  series pose. Follow the references' broad pose vocabulary: analytical side glances,
  folded arms, neutral portraits, raised palms, skeptical reactions, candid physical
  actions, or supported person-to-person interaction. Deliberately vary the pose family
  from one thumbnail to the next. Never default every person to the same raised open
  hand or mid-explanation gesture, and never copy one reference pose mechanically.
- Use the logo as a small or medium identity anchor, never as the whole composition.
  If the thumbnail headline already spells the company or product name, request its
  symbol/icon only—no wordmark and no duplicate small brand text. A wordmark is allowed
  only when the headline itself omits that brand name.
  Avoid bland stock-photo staging, generic laptop screens, tiny UI, detailed scenery,
  empty backgrounds, and collections of unrelated objects.
- Thumbnail text must be a punchy 1 to 4 word phrase, ideally 8 to 21 characters
  and never more than 22 characters. It is a visual hook, not a sentence or title.
- Use plain words only: no slash, vertical bar, separator punctuation, or duplicated
  company/product name inside one text option.
- Write text options with a built-in color hierarchy: a single power word or compact
  payoff phrase should carry the tension in red while the setup can remain warm white.
  Style-shape examples only include "HUGGING FACE HACKED", "OPUS LEVELS UP",
  "APPLE V. OPENAI", and "YOUR KEYS THEIR COINS". Never reuse an entity or claim from
  an example unless the lead-story evidence supports it.
- recommended_thumbnail_text must exactly equal one text_options entry from the
  recommended_concept_id. Do not paraphrase it into a separate value.
- Thumbnail text must identify the company, product, object, consequence, or personal
  stake from the lead story. Prefer compact hooks like "YOUR FACE DATA",
  "WINDOWS WARNING", or "WAYMO HALTED". Avoid compressed article headlines such as
  "HEATHROW WINDOWS WARNING" or "WAYMO CHARGING HALTED". Reject vague filler such as
  "TECH NEWS", "BIG UPDATE", or "THIS CHANGES EVERYTHING".
- Recommended thumbnail text must complement the title and must not repeat the full
  title. Distill its strongest concrete tension into fewer words.
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


def _trim_words(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip(" -|:;,.")
    if len(compact) <= limit:
        return compact
    shortened = compact[: limit + 1].rsplit(" ", 1)[0].strip(" -|:;,.")
    return shortened or compact[:limit].strip()


def _local_publishing_fallback(
    project: dict,
    lead: dict,
    narration: str,
) -> PublishingCopy:
    """Build conservative, grounded copy when structured provider output is invalid."""

    base = _trim_words(
        str(lead.get("title") or lead.get("source_title") or "Technology News Update"),
        EDITORIAL_TITLE_LIMIT,
    )
    subject = _trim_words(base, 34)
    title_texts = [
        base,
        _trim_words(f"Why {subject} Matters Now", EDITORIAL_TITLE_LIMIT),
        _trim_words(f"{subject}: The Real Stakes", EDITORIAL_TITLE_LIMIT),
        _trim_words(f"What {subject} Means for You", EDITORIAL_TITLE_LIMIT),
        _trim_words(f"{subject}: Why It Matters", EDITORIAL_TITLE_LIMIT),
    ]
    unique_titles: list[str] = []
    for title in title_texts:
        if len(title) < 15:
            title = _trim_words(f"{title} | Tech", EDITORIAL_TITLE_LIMIT)
        if title not in unique_titles:
            unique_titles.append(title)
    while len(unique_titles) < 5:
        suffix = len(unique_titles) + 1
        unique_titles.append(
            _trim_words(f"{subject}: Key Detail {suffix}", EDITORIAL_TITLE_LIMIT)
        )

    meaningful = [
        token.upper()
        for token in re.findall(r"[A-Za-z0-9]+", base)
        if token.casefold()
        not in {"a", "an", "and", "for", "from", "in", "of", "on", "the", "to"}
    ]
    thumbnail_words: list[str] = []
    for token in meaningful:
        candidate = " ".join([*thumbnail_words, token])
        if len(candidate) > THUMBNAIL_TEXT_LIMIT or len(thumbnail_words) >= 4:
            break
        thumbnail_words.append(token)
    thumbnail_text = " ".join(thumbnail_words) or "TECH NEWS"
    alternative_text = (
        _trim_words(f"{thumbnail_words[0]} ALERT", THUMBNAIL_TEXT_LIMIT)
        if thumbnail_words
        else "TECH ALERT"
    )

    narration_summary = _trim_words(narration, 560)
    lead_description = (
        narration_summary
        if len(narration_summary) >= 80
        else _trim_words(
            f"This report examines {base} and summarizes the verified details "
            "available in the original reporting.",
            650,
        )
    )
    other_titles = [
        str(item.get("title") or item.get("source_title") or "").strip()
        for item in sorted(project.get("stories", []), key=lambda item: int(item["order"]))
        if int(item.get("order", 0)) != int(lead.get("order", 1))
    ]
    episode_description = _trim_words(
        "Also in this technology-news episode: " + "; ".join(other_titles),
        650,
    )
    if len(episode_description) < 80:
        episode_description += (
            " The episode covers the most important context, consequences, and "
            "developments from each report."
        )

    tag_seeds = [
        "technology news",
        "tech reports",
        "latest technology",
        "technology update",
        "AI news",
        "cybersecurity news",
        "digital technology",
        "tech industry",
        *[word.casefold() for word in meaningful[:6]],
    ]
    tags: list[str] = []
    for tag in tag_seeds:
        if tag and tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
        if len(tags) == 12:
            break

    candidate_models = [
        TitleCandidate(
            title=title,
            rationale=(
                "This conservative fallback title stays anchored to the supplied "
                "lead-story headline after structured metadata repair failed."
            ),
            clarity=8,
            curiosity=7,
            specificity=8,
            factual_risk=1,
        )
        for title in unique_titles[:5]
    ]
    return PublishingCopy(
        title_candidates=candidate_models,
        recommended_title=candidate_models[0].title,
        recommended_title_reason=(
            "The original lead-story wording is the most factual and specific safe "
            "choice available without another provider request."
        ),
        thumbnail_concepts=[
            ThumbnailConcept(
                concept_id="concept_1",
                visual=(
                    "A high-contrast editorial layout on a flat soft #231F20 warm "
                    "charcoal-brown field, with the lead story's "
                    "recognizable key person or physical subject on the right, its "
                    "relevant company logo near the left headline, and one red hook word."
                ),
                text_options=[thumbnail_text, alternative_text],
                rationale=(
                    "The words come directly from the lead headline and remain "
                    "readable in the fixed series layout."
                ),
            ),
            ThumbnailConcept(
                concept_id="concept_2",
                visual=(
                    "A clean editorial close-up on a uniform soft #231F20 warm "
                    "charcoal-brown field, featuring the supported key person "
                    "or product, with a small accurate brand mark, oversized left-side "
                    "type, and the factual consequence isolated as the red payoff."
                ),
                text_options=[alternative_text, thumbnail_text],
                rationale=(
                    "This alternate arrangement preserves the same grounded subject "
                    "while offering a stronger alert treatment."
                ),
            ),
        ],
        recommended_concept_id="concept_1",
        recommended_thumbnail_text=thumbnail_text,
        lead_description=lead_description,
        episode_description=episode_description[:650],
        tags=tags,
        hashtags=["#TechNews", "#Technology", "#TechReports"],
    )


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
            if isinstance(exc, ValidationError):
                print(
                    "[PUBLISH WARNING] Structured publishing copy was inconsistent; "
                    "using a grounded local fallback without another text request.",
                    file=sys.stderr,
                )
                return _local_publishing_fallback(project, lead, narration)
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

REFERENCE-DERIVED SERIES TEMPLATE: use a uniform soft warm charcoal-brown background
matching #231F20 (RGB 35, 31, 32). This is a visibly lifted dark brown-gray field, not
black. Keep the color flat and matte across the entire canvas: no black corners, no
gradient, no vignette, no spotlight halo, and no smoky or cinematic falloff. The
background is a solid graphic fill, not a photographed room or lighted space;
lighting on the person must not brighten or darken the background. Place an
oversized uppercase headline on the left and a photorealistic editorial
cutout of the story's recognizable key person on the right. Show the person chest-up
or waist-up with an expressive, context-appropriate gesture or reaction, clean edge
separation, realistic identity, and restrained studio lighting. If no person is
named, a widely recognized founder, CEO, or public face of the primary mentioned
company may appear only as a neutral brand-associated visual anchor. Do not imply that
this person committed, suffered, witnessed, approved, or caused an action unless the
approved evidence says so. If no defensible person is available, replace the person
with the single most recognizable product, device, robot, or physical subject; never
invent a person.
At most two people may appear, only when a documented rivalry, lawsuit, or direct clash
requires both.

POSE DIVERSITY: the left-text/right-subject layout is consistent, but the person's pose
is not. Follow the reference set's full range of human presentation: analytical side
glance, folded arms, neutral portrait, raised palm, skeptical reaction, candid physical
action, or supported interaction between people. Select the pose family that best fits
this story and person, then make it visibly different from the generic raised-open-hand
or mid-explanation pose when that pose is not specifically justified. Hands may remain
visible and expressive; do not hide them by default. Do not mechanically copy a single
reference pose or give every founder the same gesture. Preserve an editorially neutral
role, and never invent a prop or physical action merely to make the pose different.

Include the main mentioned company's or product's official logo or wordmark as a small
or medium identity anchor near the headline. Render it recognizably and accurately,
but never let it replace the human/product focal subject. If the exact headline already
spells that company or product name, show its standalone symbol/icon only: omit the
wordmark and never repeat the brand in smaller text. Use a wordmark only when the exact
headline omits the brand name. Do not add unrelated logos.

TYPOGRAPHY SYSTEM: use Panton Rust Black Base for every word in the main thumbnail
headline, matching the reference thumbnails' heavy condensed uppercase treatment on
1 to 3 balanced lines, with flat vivid signal red close to #F21F3D, bright alert yellow
close to #FFD000, and warm ivory close to #F4EFD8. Make one dominant word or compact
payoff phrase red or yellow; render the remaining setup words in the warm ivory. Choose
the emphasis color from the approved story context: use red for an immediate, supported
danger, attack, failure, loss, ban, or other negative consequence; use yellow for a
warning, uncertainty, question, reveal, update, or other non-alarmist hook. Do not
default to red. Use a thin-to-medium near-black
outline and only a tight, subtle lower-right drop shadow for separation. Do not use a
deep shadow, long extrusion, bevel, metallic effect, gradient fill, glow, or 3D text.
Use that same Panton Rust Black Base upper-line display face for every main headline line;
do not switch a cream line to a pixel, bitmap, retro-game, or block font.
The emphasized element must
be the largest or strongest semantic hook, such as HACKED, RETURNS, PROBLEM, FICTION,
or BEEF when that exact idea is supported. A genuinely secondary supporting caption
may use a simpler editorial face, but every word in the main thumbnail hook must keep
the same heavy condensed display family and
the composition must still have one obvious context-appropriate red or yellow hook. Do not impose a fixed percentage
split or fixed text-box width. Give the exact phrase the space it naturally needs and
move the visual boundary when necessary. Keep generous outer margins, normal readable
letter spacing, and comfortable line spacing. Never tighten tracking, narrow glyphs,
squeeze, stretch, crowd, or run text against an edge. Preserve the consistent
left-text/right-subject rhythm and phone-size readability.

LEAD STORY: {lead.get('title', '')}
SOURCE TITLE: {lead.get('source_title', '')}
APPROVED VISUAL: {concept.visual}
APPROVED NARRATION EVIDENCE: {narration[:1800]}
TEXT-ONLY STORY CONTEXT: {json.dumps(planned.get('images', []), ensure_ascii=False)[:1800]}

Create a completely new composition specifically for this thumbnail. No previous
story image is supplied or should be recreated, traced, composited, or imitated. The
TEXT-ONLY STORY CONTEXT supplies facts only: ignore any storyboard visual_style or art
direction inside it and follow this reference-derived series template instead.
Depict one clear, immediately understandable focal subject grounded only in the
approved story. Keep the background simple and the silhouette bold rather than
building a detailed cinematic scene. Avoid crushed blacks, passive stock-photo poses,
generic screen-only scenes, logo-only art, tiny interface details, and empty filler.
No pure-black background, gradient, vignette, collage, split panels, duration badge,
YouTube UI, watermark, TV frame, flags,
weapons, fabricated numbers, unsupported victims, or unrelated technology. Never add
"THE CODE REPORT", its square corner badge, any channel logo, or any substitute corner
branding. An official story-relevant company/product logo is allowed and requested;
channel branding is not. Add no extra editorial headline. Ensure the exact headline is
perfectly spelled and fully inside the crop-safe area.
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
    hashtags = " ".join(
        item if item.startswith("#") else f"#{item}" for item in copy.hashtags
    )
    return (
        f"{copy.lead_description.strip()}\n\n"
        f"{copy.episode_description.strip()}\n\n"
        f"CHAPTERS\n{chapter_block(chapters)}\n\n"
        f"{hashtags}\n"
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


def prepare_stock_thumbnail_background(
    *,
    video_dir: Path,
    output_dir: Path,
    lead: dict,
) -> Path:
    """Create a 1280x720 thumbnail background from selected non-AI media."""
    media_plan = load_json(
        video_dir / "05_visuals" / "media_plan.json",
        "Media plan",
    )
    if media_plan.get("quality", {}).get("status") != "passed":
        raise RuntimeError("Stock thumbnail fallback requires a passing media plan.")
    lead_plan = next(
        (
            story
            for story in media_plan.get("stories", [])
            if story.get("story_id") == lead.get("story_id")
        ),
        None,
    )
    if lead_plan is None:
        raise RuntimeError("Lead story is missing from the media plan.")
    candidates = [
        (window, window.get("selected_asset", {}))
        for window in lead_plan.get("windows", [])
        if isinstance(window.get("selected_asset"), dict)
    ]
    candidates.sort(
        key=lambda item: (
            item[1].get("kind") == "video",
            float(item[0].get("start", 0)),
        )
    )
    selected_window: dict | None = None
    selected_asset: dict | None = None
    source: Path | None = None
    for window, asset in candidates:
        candidate = resolve_portable_path(
            str(asset.get("project_copy") or asset.get("absolute_path") or ""),
            video_dir=output_dir.parent,
        )
        if candidate.is_file():
            selected_window = window
            selected_asset = asset
            source = candidate
            break
    if source is None or selected_asset is None or selected_window is None:
        raise RuntimeError("Lead story has no usable selected stock image or video.")
    expected_sha = str(selected_asset.get("sha256", ""))
    source_sha = sha256_file(source)
    if expected_sha and source_sha != expected_sha:
        raise RuntimeError(f"Selected stock thumbnail source changed: {source}")

    output = output_dir / "thumbnail_stock_background.png"
    manifest_path = output_dir / "thumbnail_stock_background.json"
    source_kind = str(selected_asset.get("kind", "image"))
    seek_seconds = 0.0
    if source_kind == "video":
        seek_seconds = max(
            0.0,
            float(selected_asset.get("source_start_seconds", 0.0))
            + min(1.0, float(selected_window.get("duration", 0.0)) / 2),
        )
    fingerprint = json_fingerprint(
        {
            "source": str(source.resolve()),
            "source_sha256": source_sha,
            "source_kind": source_kind,
            "seek_seconds": seek_seconds,
            "window_id": selected_window.get("window_id"),
            "size": "1280x720",
        }
    )
    if manifest_path.is_file() and output.is_file():
        cached = load_json(manifest_path, "Stock thumbnail manifest")
        if (
            cached.get("input_fingerprint") == fingerprint
            and cached.get("file_sha256") == sha256_file(output)
        ):
            return output

    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required for the stock thumbnail fallback.")
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
    if source_kind == "video":
        command.extend(["-ss", f"{seek_seconds:.6f}"])
    command.extend(
        [
            "-i",
            str(source),
            "-vf",
            (
                "scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,"
                "crop=1280:720"
            ),
            "-frames:v",
            "1",
            str(output),
        ]
    )
    _run(command, "Prepare stock thumbnail background")
    media = probe_image(output)
    if (int(media.get("width", 0)), int(media.get("height", 0))) != (1280, 720):
        raise RuntimeError("Stock thumbnail background must be exactly 1280x720.")
    atomic_write_json(
        manifest_path,
        {
            "schema_version": 1,
            "created_at_utc": utc_now(),
            "input_fingerprint": fingerprint,
            "file": str(output),
            "file_sha256": sha256_file(output),
            "source_file": str(source.resolve()),
            "source_sha256": source_sha,
            "source_kind": source_kind,
            "source_window_id": selected_window.get("window_id"),
            "seek_seconds": seek_seconds,
            "generation_provider": "stock_or_archive_media",
        },
    )
    return output


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
    if text_rendering == "ai_rendered":
        headline_region = {
            "side": "left",
            "width_policy": "flexible_to_phrase",
            "anchor": "consistent_left",
            "maximum_lines": 3,
            "tracking_policy": "never_compress_to_fit",
        }
        subject_region = {
            "side": "right",
            "boundary_policy": "may_shift_for_natural_headline_spacing",
        }
    else:
        headline_region = {
            "side": "left",
            "maximum_width_percent": 46,
            "text_origin": {"x": 55, "vertical_alignment": "center"},
            "maximum_lines": 3,
        }
        subject_region = {"side": "right", "target_width_percent": 54}
    return {
        "alignment_template": {
            "template_id": (
                "tech_reports_ai_headline_v2"
                if text_rendering == "ai_rendered"
                else "tech_news_left_headline_v1"
            ),
            "canvas": {"width": 1280, "height": 720},
            "headline_region": headline_region,
            "subject_region": subject_region,
            "palette": {
                "background": "flat_soft_warm_charcoal_brown_#231F20",
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
    destination_dir = ASSETS_ROOT / "Media" / "Thumbnails"
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
            else "stock_or_archive_media"
            if background.name == "thumbnail_stock_background.png"
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


def _ensure_grounded_publishing_copy(
    copy: PublishingCopy,
    project: dict,
    lead: dict,
    narration: str,
) -> PublishingCopy:
    if _copy_grounding_quality(copy, lead, narration)["minimum_supported_terms"]:
        return copy
    grounded_candidates = [
        item
        for item in copy.title_candidates
        if _copy_grounding_quality(
            copy.model_copy(update={"recommended_title": item.title}),
            lead,
            narration,
        )["minimum_supported_terms"]
    ]
    if grounded_candidates:
        copy.recommended_title = max(
            grounded_candidates,
            key=lambda item: (
                item.clarity + item.curiosity + item.specificity - item.factual_risk
            ),
        ).title
        return copy
    print(
        "[PUBLISH WARNING] No suggested title passed local grounding; using a "
        "conservative lead-story fallback before thumbnail generation.",
        file=sys.stderr,
    )
    return _local_publishing_fallback(project, lead, narration)


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
            "publishing_prompt_version": PUBLISHING_PROMPT_VERSION,
        }
    )
    publishing_copy: PublishingCopy | None = None
    reused_cached_copy = False
    existing_package_path = output_dir / "publish_package.json"
    existing_title_path = output_dir / "title.txt"
    prior_custom_title = (
        existing_title_path.read_text(encoding="utf-8").strip()
        if existing_title_path.is_file()
        else ""
    )
    if copy_path.is_file() and not force_copy:
        cached = load_json(copy_path, "Editorial publishing copy")
        fingerprint_matches = cached.get("input_fingerprint") == copy_input_fingerprint
        lead_text_unchanged = False
        if not fingerprint_matches and existing_package_path.is_file():
            try:
                existing_pkg = load_json(existing_package_path, "Publish package")
                prev_lead = existing_pkg.get("lead_story", {})
                lead_text_unchanged = (
                    prev_lead.get("story_id") == lead.get("story_id")
                    and prev_lead.get("narration_sha256")
                    == sha256_file(Path(lead["text"]))
                    and prev_lead.get("original_sha256")
                    == sha256_file(Path(lead["original"]))
                    and cached.get("model") == settings.model
                )
            except Exception:  # noqa: BLE001
                lead_text_unchanged = False
        if fingerprint_matches or lead_text_unchanged:
            publishing_copy = PublishingCopy.model_validate(cached["copy"])
            reused_cached_copy = True
            if lead_text_unchanged and not fingerprint_matches:
                cached["input_fingerprint"] = copy_input_fingerprint
                atomic_write_json(copy_path, cached)
    if publishing_copy is None:
        if not confirm_ai_cost:
            raise RuntimeError(
                "Publishing copy needs one paid text request. Re-run with "
                "--confirm-ai-cost, or keep a current cached editorial_copy.json."
            )
        publishing_copy = generate_copy_with_openai(
            project, lead, planned, narration, original, settings
        )
        publishing_copy = _ensure_grounded_publishing_copy(
            publishing_copy,
            project,
            lead,
            narration,
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
    else:
        publishing_copy = _ensure_grounded_publishing_copy(
            publishing_copy,
            project,
            lead,
            narration,
        )
    requested_thumbnail_headline = (
        rendered_headline.strip()
        if rendered_headline and rendered_headline.strip()
        else publishing_copy.recommended_thumbnail_text
    )
    thumbnail_headline = _normalize_thumbnail_text(requested_thumbnail_headline)
    if thumbnail_headline != requested_thumbnail_headline:
        print(
            "[PUBLISH WARNING] Thumbnail headline exceeded the layout boundary; "
            f"using '{thumbnail_headline}' locally.",
            file=sys.stderr,
        )
    configured_images = storyboard.get("configuration", {}).get(
        "images_per_story",
        storyboard.get("quality", {}).get("images_per_story"),
    )
    try:
        ai_image_generation_disabled = int(configured_images) == 0
    except (TypeError, ValueError):
        ai_image_generation_disabled = bool(
            storyboard.get("quality", {}).get("ai_image_generation_disabled")
        )
    if ai_image_generation_disabled and background is None and not generate_thumbnail:
        generate_thumbnail = False
        thumbnail_text_rendering = "local_overlay"
        background = prepare_stock_thumbnail_background(
            video_dir=video_dir,
            output_dir=output_dir,
            lead=lead,
        )
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
    youtube_title = format_youtube_title(publishing_copy.recommended_title)
    if reused_cached_copy and prior_custom_title:
        try:
            youtube_title = validate_custom_youtube_title(prior_custom_title)
        except ValueError:
            pass
    if chapters_text not in description:
        raise RuntimeError("Description is missing its exact YouTube chapter block.")
    atomic_write_text(output_dir / "title.txt", youtube_title)
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
        ] in {
            "tech_news_left_headline_v1",
            "tech_reports_ai_headline_v1",
            "tech_reports_ai_headline_v2",
        },
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
                else "stock_or_archive_media_with_local_overlay"
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
    upload_manifest_path = output_dir / "youtube_upload_manifest.json"
    scheduled_publish = next_publish_slot(exclude_manifest=upload_manifest_path)
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
                "title": youtube_title,
                "description": description,
                "tags": publishing_copy.tags,
                "categoryId": "28",
                "defaultLanguage": "en",
                "defaultAudioLanguage": "en",
            },
            "status": {
                "privacyStatus": "private",
                "publishAt": scheduled_publish.isoformat(),
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
        "scheduled_publish_at": scheduled_publish.isoformat(),
        "scheduled_publish_at_utc": scheduled_publish.astimezone(timezone.utc).isoformat(),
        "schedule_timezone": "GMT+03:00 Turkey",
        "schedule_configuration": SCHEDULE_CONFIG.as_dict(),
        "automatic_captions": "youtube_automatic_when_eligible",
        "created_at_utc": utc_now(),
    }
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


def update_publish_title(video_number: int, new_title: str) -> dict:
    """Update the YouTube title across all Milestone 9 publish artifacts."""
    formatted_title = validate_custom_youtube_title(new_title)
    video_dir = project_dir(video_number)
    output_dir = video_dir / "09_publish"
    title_path = output_dir / "title.txt"
    package_path = output_dir / "publish_package.json"
    upload_manifest_path = output_dir / "youtube_upload_manifest.json"
    state_path = video_dir / "state.json"

    if (
        not title_path.is_file()
        and not package_path.is_file()
        and not upload_manifest_path.is_file()
    ):
        raise FileNotFoundError(
            f"Publish package not found for {project_name(video_number)}: {output_dir}"
        )

    previous_title = ""
    if title_path.is_file():
        previous_title = title_path.read_text(encoding="utf-8").strip()

    package: dict | None = None
    if package_path.is_file():
        package = load_json(package_path, "Publish package")
        if not previous_title:
            previous_title = str(package.get("recommended_title", "")).strip()

    upload_manifest: dict | None = None
    if upload_manifest_path.is_file():
        upload_manifest = load_json(upload_manifest_path, "YouTube upload manifest")
        if not previous_title:
            previous_title = str(
                upload_manifest.get("metadata", {}).get("snippet", {}).get("title", "")
            ).strip()

    atomic_write_text(title_path, formatted_title)

    if upload_manifest is not None:
        upload_manifest.setdefault("metadata", {}).setdefault("snippet", {})[
            "title"
        ] = formatted_title
        atomic_write_json(upload_manifest_path, upload_manifest)

    if package is not None:
        package["recommended_title"] = formatted_title
        candidates = package.get("title_candidates", [])
        if isinstance(package.get("quality"), dict):
            package["quality"]["recommended_title_within_100_characters"] = (
                len(formatted_title) <= YOUTUBE_TITLE_LIMIT
            )
            package["quality"]["recommended_title_is_candidate"] = any(
                isinstance(item, dict)
                and str(item.get("title", "")).strip() == formatted_title
                for item in candidates
            )
        if isinstance(package.get("grounding"), dict):
            package["grounding"]["recommended_title_characters"] = len(formatted_title)
        atomic_write_json(package_path, package)

        archive_meta = package.get("thumbnail_asset_archive", {})
        if isinstance(archive_meta, dict) and archive_meta.get("metadata_file"):
            archive_meta_path = Path(str(archive_meta["metadata_file"]))
            if archive_meta_path.is_file():
                try:
                    metadata = load_json(archive_meta_path, "Thumbnail archive metadata")
                    metadata["youtube_title"] = formatted_title
                    atomic_write_json(archive_meta_path, metadata)
                except RuntimeError:
                    pass

    if state_path.is_file():
        state = load_json(state_path, "Stage state")
        publish_stage_state = state.setdefault("stages", {}).setdefault(STAGE_NAME, {})
        publish_stage_state["title"] = formatted_title
        publish_stage_state["updated_at_utc"] = utc_now()
        state["updated_at_utc"] = publish_stage_state["updated_at_utc"]
        atomic_write_json(state_path, state)

    return {
        "video_number": video_number,
        "previous_title": previous_title,
        "title": formatted_title,
        "changed": formatted_title != previous_title,
        "title_file": str(title_path),
        "publish_package_file": str(package_path),
        "youtube_upload_manifest_file": str(upload_manifest_path),
    }


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
        "--action",
        choices=("generate", "status", "set-title"),
        default="generate",
    )
    parser.add_argument("--title")
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
        elif args.action == "set-title":
            if not args.title:
                raise ValueError("--title is required when --action is set-title.")
            result = update_publish_title(args.video, args.title)
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
        if args.json or args.action in {"status", "set-title"}:
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
