"""Choose stock-video or motion-graphic scenes from timestamped news windows."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
AUTOMATION_ROOT = PROJECT_ROOT / "Automation"
AUTOMATION_SOURCE = AUTOMATION_ROOT / "src"
AUTOMATION_SITE_PACKAGES = AUTOMATION_ROOT / ".venv" / "Lib" / "site-packages"

# Use the project's existing Automation environment when this module is called
# with the system Python. The application itself already runs through this venv.
if AUTOMATION_SITE_PACKAGES.is_dir() and str(AUTOMATION_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_SITE_PACKAGES))

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


load_dotenv(AUTOMATION_ROOT / ".env", override=False)

try:
    from .asset_resolver import get_brand_logo
except ImportError:  # Running as `python motion_engine/visual_director.py`.
    from asset_resolver import get_brand_logo


TemplateType = Literal[
    "bento_stat",
    "versus_card",
    "headline_highlight",
    "terminal_code",
    "bullet_summary",
]
SceneType = Literal["STOCK_VIDEO", "MOTION_GRAPHIC"]
MotionTrigger = Literal[
    "number_or_rate",
    "technology_comparison",
    "code_or_command",
    "headline_emphasis",
    "three_item_list",
]


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icon: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=220)


class BentoStatData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=180)
    kicker: str | None
    footnote: str | None
    logo_icon: str | None


class VersusCardData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=100)
    left_title: str = Field(min_length=1, max_length=80)
    left_val: str = Field(min_length=1, max_length=80)
    right_title: str = Field(min_length=1, max_length=80)
    right_val: str = Field(min_length=1, max_length=80)
    left_note: str | None
    right_note: str | None


class HeadlineHighlightData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    headline: str = Field(min_length=1, max_length=240)
    highlight_words: list[str] = Field(min_length=1, max_length=8)
    category: str | None
    footer: str | None


class TerminalCodeData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    lines: list[str] = Field(min_length=1, max_length=8)


class BulletSummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=140)
    items: list[SummaryItem] = Field(min_length=3, max_length=3)


TemplateData = (
    BentoStatData
    | VersusCardData
    | HeadlineHighlightData
    | TerminalCodeData
    | BulletSummaryData
    | None
)


class WindowDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_id: str = Field(min_length=1, max_length=120)
    scene_type: SceneType
    motion_trigger_type: MotionTrigger | None
    motion_trigger_evidence: str | None
    template_type: TemplateType | None
    template_data: TemplateData
    brand_slug: str | None
    search_query: str | None
    rationale: str = Field(min_length=8, max_length=400)


class VisualDirectorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenes: list[WindowDecision]


class VisualDirectorError(RuntimeError):
    """Raised when the model response cannot be aligned to timed input windows."""


_DATA_MODELS: dict[str, type[BaseModel]] = {
    "bento_stat": BentoStatData,
    "versus_card": VersusCardData,
    "headline_highlight": HeadlineHighlightData,
    "terminal_code": TerminalCodeData,
    "bullet_summary": BulletSummaryData,
}
_TRIGGER_TEMPLATES: dict[str, set[str]] = {
    "number_or_rate": {"bento_stat", "headline_highlight"},
    "technology_comparison": {"versus_card", "headline_highlight"},
    "code_or_command": {"terminal_code"},
    "headline_emphasis": {"headline_highlight"},
    "three_item_list": {"bullet_summary"},
}
_SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_NUMBER_PATTERN = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
_NUMBER_WORD_VALUES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_SCALES = {
    "thousand": Decimal(1_000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
    "trillion": Decimal(1_000_000_000_000),
    "k": Decimal(1_000),
    "m": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
    "t": Decimal(1_000_000_000_000),
}
_NUMBER_WORD_PATTERN = re.compile(
    r"(?<![a-z])(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion|trillion|point|and|minus|negative|plus)"
    r"(?:[\s-]+(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion|trillion|point|and|minus|negative|plus))*"
    r"(?![a-z])",
    re.IGNORECASE,
)
_SPOKEN_YEAR_PATTERN = re.compile(
    r"\b(?P<century>eighteen|nineteen|twenty)[\s-]+"
    r"(?P<decade>twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[\s-]+(?P<unit>one|two|three|four|five|six|seven|eight|nine))?\b",
    re.IGNORECASE,
)


def _number_word_value(phrase: str) -> Decimal | None:
    total = Decimal(0)
    group = 0
    decimal_digits = ""
    in_decimal = False
    sign = 1
    found_number = False
    for token in phrase.casefold().replace("-", " ").split():
        if token in {"and", "plus"}:
            continue
        if token in {"minus", "negative"} and not found_number:
            sign = -1
            continue
        if token == "point":
            in_decimal = True
            continue
        if in_decimal and token in _NUMBER_WORD_VALUES:
            decimal_digits += str(_NUMBER_WORD_VALUES[token])
            found_number = True
        elif token in _NUMBER_WORD_VALUES:
            group += _NUMBER_WORD_VALUES[token]
            found_number = True
        elif token == "hundred":
            group = max(group, 1) * 100
            found_number = True
        elif token in _NUMBER_SCALES:
            group_value = Decimal(group)
            if decimal_digits:
                group_value += Decimal(f"0.{decimal_digits}")
            total += (group_value if group_value else Decimal(1)) * _NUMBER_SCALES[token]
            group = 0
            decimal_digits = ""
            in_decimal = False
            found_number = True
    if not found_number:
        return None
    value = total + Decimal(group)
    if decimal_digits:
        value += Decimal(f"0.{decimal_digits}")
    return value * sign


def _number_mentions(text: str) -> set[Decimal]:
    """Find numeric mentions expressed as digits or standard English number words."""
    values: set[Decimal] = set()
    for match in _NUMBER_PATTERN.finditer(text):
        try:
            value = Decimal(match.group().replace(",", ""))
        except InvalidOperation:
            continue
        scale_match = re.match(
            r"\s*(thousand|million|billion|trillion|[kmbt])\b",
            text[match.end():],
            re.IGNORECASE,
        )
        if scale_match:
            value *= _NUMBER_SCALES[scale_match.group(1).casefold()]
        values.add(value.normalize())

    year_text = _SPOKEN_YEAR_PATTERN.sub(
        lambda match: str(
            {"eighteen": 1800, "nineteen": 1900, "twenty": 2000}[
                match.group("century").casefold()
            ]
            + _NUMBER_WORD_VALUES[match.group("decade").casefold()]
            + _NUMBER_WORD_VALUES.get((match.group("unit") or "").casefold(), 0)
        ),
        text,
    )
    for match in _NUMBER_WORD_PATTERN.finditer(year_text):
        value = _number_word_value(match.group())
        if value is None:
            continue
        scale_match = re.match(
            r"\s+(thousand|million|billion|trillion)\b",
            year_text[match.end():],
            re.IGNORECASE,
        )
        if scale_match:
            value *= _NUMBER_SCALES[scale_match.group(1).casefold()]
        values.add(value.normalize())
    return values


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VisualDirectorError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise VisualDirectorError(f"{label} must contain a JSON object: {path}")
    return value


def _resolve_input_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _story_for_id(stories: Sequence[dict[str, Any]], story_id: str | None) -> dict[str, Any]:
    if story_id:
        for story in stories:
            if str(story.get("story_id", "")) == story_id:
                return story
        raise VisualDirectorError(f"Story {story_id!r} was not found in the input manifest.")
    if len(stories) == 1:
        return stories[0]
    raise VisualDirectorError("story_id is required when a manifest contains multiple stories.")


def _stock_windows_from_shared_builder(
    video_dir: Path,
    story: dict[str, Any],
) -> list[dict[str, Any]]:
    """Use the exact window builder used by media_selection_stage."""
    source_path = str(AUTOMATION_SOURCE)
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    try:
        from media_selection_stage import (  # noqa: PLC0415
            MediaSelectionSettings,
            build_archive_windows,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Timed stock windows require the existing Automation/.venv environment."
        ) from exc

    storyboard_path = video_dir / "04_storyboard" / "storyboard.json"
    storyboard_story: dict[str, Any] = {"images": []}
    if storyboard_path.is_file():
        storyboard = _read_json(storyboard_path, "Storyboard")
        for candidate in storyboard.get("stories", []):
            if candidate.get("story_id") == story.get("story_id"):
                storyboard_story = candidate
                break
    settings = MediaSelectionSettings.from_environment()
    return build_archive_windows(story, storyboard_story, settings)


def load_story_inputs(
    video_dir: str | Path,
    story_id: str | None = None,
    *,
    media_plan_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load a news summary and the same timed windows used by stock selection.

    Existing ``05_visuals/media_plan.json`` windows are preferred. If media
    selection has not run, the helper calls its shared ``build_archive_windows``
    function so the visual director keeps the same boundaries and timestamps.
    """
    root = Path(video_dir).expanduser().resolve()
    timing = _read_json(root / "story_timing.json", "Story timing manifest")
    story = _story_for_id(timing.get("stories", []), story_id)
    resolved_story_id = str(story.get("story_id", ""))

    text_path_value = story.get("text_file")
    if not text_path_value:
        raise VisualDirectorError(f"Story {resolved_story_id!r} has no text_file in story_timing.json.")
    text_path = _resolve_input_path(text_path_value, root)
    try:
        news_text = text_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FileNotFoundError(f"News text file not found: {text_path}") from exc
    if not news_text:
        raise VisualDirectorError(f"News text is empty: {text_path}")

    plan_path = (
        Path(media_plan_path).expanduser().resolve()
        if media_plan_path is not None
        else root / "05_visuals" / "media_plan.json"
    )
    windows: list[dict[str, Any]] = []
    window_source = ""
    if plan_path.is_file():
        media_plan = _read_json(plan_path, "Media plan")
        for planned_story in media_plan.get("stories", []):
            if planned_story.get("story_id") == resolved_story_id:
                planned_windows = planned_story.get("windows", [])
                if isinstance(planned_windows, list) and planned_windows:
                    windows = planned_windows
                    window_source = str(plan_path)
                break

    if not windows:
        windows = _stock_windows_from_shared_builder(root, story)
        window_source = "media_selection_stage.build_archive_windows"

    if not windows:
        windows = story.get("visual_beats") or story.get("sentences") or []
        window_source = "story_timing.json visual_beats/sentences fallback"
    if not windows:
        raise VisualDirectorError(f"No timestamped windows were found for {resolved_story_id!r}.")

    return {
        "story_id": resolved_story_id,
        "title": str(story.get("title", "")),
        "news_text": news_text,
        "segments": windows,
        "window_source": window_source,
    }


def _existing_stock_query(segment: Mapping[str, Any]) -> str:
    direct = segment.get("search_query") or segment.get("primary_query")
    if direct:
        return str(direct).strip()
    stock_query = segment.get("stock_video_query")
    if isinstance(stock_query, Mapping):
        primary = stock_query.get("primary_query")
        if primary:
            return str(primary).strip()
    selected = segment.get("selected_asset")
    if isinstance(selected, Mapping):
        metadata = selected.get("provider_metadata")
        if isinstance(metadata, Mapping) and metadata.get("query"):
            return str(metadata["query"]).strip()
    return ""


def _normalize_segments(segments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(segments, start=1):
        if not isinstance(raw, Mapping):
            raise TypeError(f"Timed segment {index} must be a mapping.")
        start_value = raw.get("start", raw.get("timeline_start"))
        end_value = raw.get("end", raw.get("timeline_end"))
        try:
            start = float(start_value)
            end = float(end_value)
            timeline_start = float(raw.get("timeline_start", start))
            timeline_end = float(raw.get("timeline_end", end))
        except (TypeError, ValueError) as exc:
            raise VisualDirectorError(f"Timed segment {index} has invalid start/end values.") from exc
        if start < 0 or end <= start or timeline_start < 0 or timeline_end <= timeline_start:
            raise VisualDirectorError(f"Timed segment {index} must have positive, ordered timestamps.")

        window_id = str(
            raw.get("window_id")
            or raw.get("segment_id")
            or raw.get("id")
            or raw.get("index")
            or f"window_{index:03d}"
        )
        if window_id in identifiers:
            raise VisualDirectorError(f"Duplicate timed segment id: {window_id}")
        identifiers.add(window_id)
        text = str(raw.get("text") or raw.get("important_claim") or raw.get("timestamp_excerpt") or "").strip()
        if not text:
            raise VisualDirectorError(f"Timed segment {window_id!r} has no narration text.")

        entities = raw.get("entities", [])
        if not entities:
            named_entities = raw.get("named_entities")
            if isinstance(named_entities, Mapping):
                entities = [
                    str(entity)
                    for values in named_entities.values()
                    if isinstance(values, list)
                    for entity in values
                ]
        if not isinstance(entities, list):
            entities = []
        visual_queries = raw.get("visual_queries", raw.get("suggested_visual_queries", []))
        if not isinstance(visual_queries, list):
            visual_queries = []

        normalized.append(
            {
                "window_id": window_id,
                "start": start,
                "end": end,
                "duration": end - start,
                "timeline_start": timeline_start,
                "timeline_end": timeline_end,
                "text": text,
                "timestamp_excerpt": str(raw.get("timestamp_excerpt", "")),
                "entities": [str(item) for item in entities],
                "scene_intent": str(raw.get("scene_intent", "")),
                "visual_queries": [str(item) for item in visual_queries],
                "existing_stock_query": _existing_stock_query(raw),
                "brand_slug": str(raw.get("brand_slug", "")).strip() or None,
            }
        )
    return normalized


def _configured_model(model: str | None) -> str:
    return (
        (model or "").strip()
        or os.getenv("MOTION_VISUAL_DIRECTOR_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or "gpt-6-luna"
    )


def _configured_reasoning_effort(reasoning_effort: str | None) -> str:
    effort = (
        (reasoning_effort or "").strip().lower()
        or os.getenv("MOTION_VISUAL_DIRECTOR_REASONING_EFFORT", "").strip().lower()
        or os.getenv("OPENAI_REASONING_EFFORT", "").strip().lower()
        or "medium"
    )
    if effort not in {"low", "medium", "high", "xhigh"}:
        raise ValueError("reasoning_effort must be low, medium, high, or xhigh.")
    return effort


def motion_graphic_target_percent() -> int:
    """Return the configured motion-graphic share of timed visual windows."""
    raw = os.getenv("MOTION_GRAPHIC_TARGET_PERCENT", "35").strip()
    try:
        target = int(raw)
    except ValueError as exc:
        raise ValueError("MOTION_GRAPHIC_TARGET_PERCENT must be a whole number from 0 to 100.") from exc
    if not 0 <= target <= 100:
        raise ValueError("MOTION_GRAPHIC_TARGET_PERCENT must be from 0 to 100.")
    return target


def _motion_target_range(target_percent: int) -> tuple[int, int]:
    """Allow five percentage points for indivisible timed windows."""
    return max(0, target_percent - 5), min(100, target_percent + 5)


def _build_prompt(
    *,
    news_text: str,
    title: str,
    segments: list[dict[str, Any]],
    story_id: str,
    default_brand_slug: str | None,
    target_motion_percent: int,
) -> str:
    total_seconds = sum(segment["duration"] for segment in segments)
    minimum_motion_percent, maximum_motion_percent = _motion_target_range(
        target_motion_percent
    )
    minimum_stock_percent = 100 - maximum_motion_percent
    maximum_stock_percent = 100 - minimum_motion_percent
    target_stock_percent = 100 - target_motion_percent
    segment_payload = [
        {
            "window_id": segment["window_id"],
            "start": segment["start"],
            "end": segment["end"],
            "duration": segment["duration"],
            "timeline_start": segment["timeline_start"],
            "timeline_end": segment["timeline_end"],
            "text": segment["text"],
            "timestamp_excerpt": segment["timestamp_excerpt"],
            "entities": segment["entities"],
            "scene_intent": segment["scene_intent"],
            "visual_queries": segment["visual_queries"],
            "existing_stock_query": segment["existing_stock_query"],
            "brand_slug": segment["brand_slug"] or default_brand_slug,
        }
        for segment in segments
    ]
    return f"""
Plan exactly one visual decision for each supplied timed window. Preserve each
window_id and do not split, merge, move, or extend its timestamps. This keeps animation
decisions aligned with the same windows used by the existing stock-video selector.

Treat allocation as a real target: calculate shares by timed-window duration and aim
for {target_stock_percent}% STOCK_VIDEO / {target_motion_percent}% MOTION_GRAPHIC,
allowing a five-percentage-point band ({minimum_stock_percent}–{maximum_stock_percent}%
stock / {minimum_motion_percent}–{maximum_motion_percent}% motion) for indivisible
windows. First scan every window for valid cues, then select the strongest eligible
windows to approach the configured target. Do not default eligible windows to stock
merely to be conservative. A window is eligible only when its text contains a concrete
number or rate, a direct technology comparison, literal code/command text, a central
headline-worthy claim, or exactly three explicit list items. Prefer distinct facts
over repeated overlapping windows. If supported cues cannot reach the target, use all
valid cues and leave the rest as stock; never invent a cue to satisfy the ratio.
A scene's motion_trigger_evidence must be an exact excerpt from that window's text.

For STOCK_VIDEO, return a concise English search_query for the existing Pixabay/Pexels
searcher. Reuse existing_stock_query unchanged when it is relevant. Keep the query
visual and literal; do not imply footage shows the exact company, person, or event.
For MOTION_GRAPHIC, select exactly one of bento_stat, versus_card, headline_highlight,
terminal_code, or bullet_summary and fill only that template's fields from the supplied
news. For bento_stat.value, use an amount explicitly stated in that exact timed window;
you may convert spoken number words to digits, but do not estimate, round, or abbreviate.
Use bento_stat for a supported number/rate, versus_card for two explicit values,
headline_highlight for an important supported claim, terminal_code only for literal
commands/code, and bullet_summary only for exactly three explicit items. Do not
fabricate numbers, comparison values, source names, code, list entries, or claims.
Set brand_slug only when a relevant technology brand is unambiguous. Use a lowercase
Simple Icons slug; otherwise return null. Do not generate SVG markup.

STORY
ID: {story_id}
TITLE: {title or "(not provided)"}
DEFAULT BRAND SLUG: {default_brand_slug or "(none)"}
TOTAL TIMED-WINDOW DURATION: {total_seconds:.3f} seconds

NEWS TEXT
{news_text}

TIMED WINDOWS
{json.dumps(segment_payload, ensure_ascii=False, indent=2)}
""".strip()


def _model_instance(data: TemplateData) -> BaseModel | None:
    return data if isinstance(data, BaseModel) else None


def _normalised_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _canonical_brand_slug(value: str | None) -> str | None:
    if not value:
        return None
    slug = re.sub(r"[^a-z0-9-]+", "-", value.strip().casefold()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug if _SLUG_PATTERN.fullmatch(slug) else None


_BRAND_TEXT_PATTERNS = {
    "anthropic": re.compile(r"\b(?:anthropic|claude)\b", re.IGNORECASE),
    "openai": re.compile(r"\b(?:openai|gpt(?:[- ]?4|[- ]?5))\b", re.IGNORECASE),
    "apple": re.compile(r"\b(?:apple|iphone|ipad|macbook)\b", re.IGNORECASE),
    "microsoft": re.compile(r"\b(?:microsoft|windows|azure|foundry)\b", re.IGNORECASE),
    "google": re.compile(r"\b(?:google|gemini|android)\b", re.IGNORECASE),
    "nvidia": re.compile(r"\b(?:nvidia|geforce|cuda)\b", re.IGNORECASE),
    "samsung": re.compile(r"\b(?:samsung|galaxy)\b", re.IGNORECASE),
    "meta": re.compile(r"\b(?:meta|facebook|instagram)\b", re.IGNORECASE),
    "amazon": re.compile(r"\b(?:amazon|aws)\b", re.IGNORECASE),
    "intel": re.compile(r"\bintel\b", re.IGNORECASE),
    "amd": re.compile(r"\b(?:amd|ryzen|radeon)\b", re.IGNORECASE),
    "qualcomm": re.compile(r"\b(?:qualcomm|snapdragon)\b", re.IGNORECASE),
}


def _infer_brand_slug(text: str) -> str | None:
    matches = [
        slug for slug, pattern in _BRAND_TEXT_PATTERNS.items()
        if pattern.search(text)
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_logo_svg(slug: str, cache: dict[str, str | None]) -> str | None:
    if slug in cache:
        return cache[slug]
    try:
        cached_path = get_brand_logo(slug)
        svg = Path(cached_path).read_text(encoding="utf-8") if cached_path else None
    except (OSError, ValueError):
        svg = None
    except Exception as exc:  # Network failures keep the Lucide fallback usable.
        try:
            import requests  # noqa: PLC0415
        except ImportError:
            raise exc
        if not isinstance(exc, requests.exceptions.RequestException):
            raise
        svg = None
    cache[slug] = svg
    return svg


def _validated_template_data(decision: WindowDecision, window_text: str) -> dict[str, Any]:
    template_type = decision.template_type
    data = _model_instance(decision.template_data)
    expected_model = _DATA_MODELS.get(template_type or "")
    if template_type is None or data is None or expected_model is None:
        raise VisualDirectorError("MOTION_GRAPHIC needs one supported template and its data.")
    if not isinstance(data, expected_model):
        raise VisualDirectorError(
            f"template_data does not match template_type {template_type!r}."
        )

    data_dict = data.model_dump(mode="json", exclude_none=True)
    if template_type == "bento_stat":
        value = str(data_dict.get("value", ""))
        value_numbers = _number_mentions(value)
        window_numbers = _number_mentions(window_text)
        if not (value_numbers & window_numbers):
            raise VisualDirectorError(
                "bento_stat.value must use a number supported by the timed news window "
                f"(value={value!r}, parsed={sorted(value_numbers)}, "
                f"window_numbers={sorted(window_numbers)})."
            )
    elif template_type == "headline_highlight":
        headline = str(data_dict.get("headline", ""))
        words = data_dict.get("highlight_words", [])

        def render_token(value: str) -> str:
            return re.sub(r"^[^\w]+|[^\w]+$", "", value, flags=re.UNICODE).casefold()

        headline_tokens = {render_token(token) for token in headline.split()}
        normalized_highlights: list[str] = []
        for word in words:
            parts = [render_token(part) for part in str(word).split()]
            parts = [part for part in parts if part]
            if not parts or any(part not in headline_tokens for part in parts):
                raise VisualDirectorError("Every highlight_words entry must appear in the headline.")
            normalized_highlights.extend(parts)
        data_dict["highlight_words"] = list(dict.fromkeys(normalized_highlights))
    elif template_type == "terminal_code":
        lines = data_dict.get("lines", [])
        if not lines or not any(_normalised_text(line) in _normalised_text(window_text) for line in lines):
            raise VisualDirectorError("terminal_code must contain literal command/code text from the window.")
    elif template_type == "bullet_summary" and len(data_dict.get("items", [])) != 3:
        raise VisualDirectorError("bullet_summary requires exactly three items.")
    return data_dict


def _fallback_stock_query(segment: Mapping[str, Any]) -> str:
    existing_query = str(segment.get("existing_stock_query", "")).strip()
    if existing_query:
        return existing_query
    visual_queries = segment.get("visual_queries", [])
    if visual_queries:
        return " ".join(str(item).strip() for item in visual_queries[:2] if str(item).strip())
    intent = str(segment.get("scene_intent", "")).replace("_", " ").strip()
    entities = [str(item).strip() for item in segment.get("entities", []) if str(item).strip()]
    query = " ".join([intent, *entities]).strip()
    if query:
        return query
    stopwords = {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "has", "have", "in", "into", "is", "it", "of", "on", "or", "that",
        "the", "their", "they", "this", "to", "was", "were", "with",
    }
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", str(segment.get("text", "")))
        if word.casefold() not in stopwords
    ]
    return " ".join(words[:8])


def _stock_fallback(segment: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "scene_type": "STOCK_VIDEO",
        "motion_trigger_type": None,
        "motion_trigger_evidence": None,
        "template_type": None,
        "template_data": None,
        "brand_slug": None,
        "search_query": _fallback_stock_query(segment),
        "rationale": f"Used stock footage because motion data was not validated: {reason}"[:400],
    }


def _validate_decision(decision: WindowDecision, segment: dict[str, Any]) -> dict[str, Any]:
    scene_type = decision.scene_type
    if scene_type == "STOCK_VIDEO":
        if decision.template_type is not None or decision.template_data is not None:
            return _stock_fallback(segment, "stock scene included motion-template data")
        query = (segment["existing_stock_query"] or decision.search_query or "").strip()
        if not query:
            query = _fallback_stock_query(segment)
        if not query:
            raise VisualDirectorError(f"STOCK_VIDEO window {decision.window_id!r} has no usable search_query.")
        search_query = query
        return {
            "scene_type": scene_type,
            "motion_trigger_type": None,
            "motion_trigger_evidence": None,
            "template_type": None,
            "template_data": None,
            "brand_slug": None,
            "search_query": search_query,
            "rationale": decision.rationale,
        }

    try:
        if decision.search_query is not None:
            raise VisualDirectorError("motion scene also included a stock search query")
        if decision.motion_trigger_type is None or not decision.motion_trigger_evidence:
            raise VisualDirectorError("motion scene has no trigger and evidence")
        if _normalised_text(decision.motion_trigger_evidence) not in _normalised_text(segment["text"]):
            raise VisualDirectorError("motion trigger evidence is absent from the timed window")
        allowed_templates = _TRIGGER_TEMPLATES[decision.motion_trigger_type]
        if decision.template_type not in allowed_templates:
            raise VisualDirectorError(
                f"trigger {decision.motion_trigger_type!r} does not support template {decision.template_type!r}"
            )
        template_data = _validated_template_data(decision, segment["text"])
    except VisualDirectorError as exc:
        return _stock_fallback(segment, str(exc))
    return {
        "scene_type": scene_type,
        "motion_trigger_type": decision.motion_trigger_type,
        "motion_trigger_evidence": decision.motion_trigger_evidence,
        "template_type": decision.template_type,
        "template_data": template_data,
        "brand_slug": _canonical_brand_slug(decision.brand_slug),
        "search_query": None,
        "rationale": decision.rationale,
    }


def _calibrate_motion_share(
    *,
    client: Any,
    model: str,
    reasoning_effort: str,
    news_text: str,
    title: str,
    story_id: str,
    segments: list[dict[str, Any]],
    decisions: dict[str, WindowDecision],
    validated: dict[str, dict[str, Any]],
    target_motion_percent: int,
) -> tuple[dict[str, WindowDecision], dict[str, Any]]:
    total_seconds = sum(segment["duration"] for segment in segments)
    motion_seconds = sum(
        segment["duration"]
        for segment in segments
        if validated[segment["window_id"]]["scene_type"] == "MOTION_GRAPHIC"
    )
    initial_percent = 100 * motion_seconds / total_seconds if total_seconds else 0.0
    minimum_motion_percent, maximum_motion_percent = _motion_target_range(
        target_motion_percent
    )
    metadata: dict[str, Any] = {
        "attempted": False,
        "initial_motion_graphic_percent": round(initial_percent, 2),
        "target_motion_graphic_percent": target_motion_percent,
        "target_motion_graphic_percent_range": [
            minimum_motion_percent,
            maximum_motion_percent,
        ],
        "added_window_ids": [],
        "removed_window_ids": [],
        "status": "not_needed",
    }
    if not total_seconds:
        metadata["status"] = "no_timed_windows"
        return decisions, metadata

    # When the initial plan is above the configured band, preserve the strongest
    # factual animation cues and downgrade weaker headline cards to stock footage.
    if initial_percent > maximum_motion_percent:
        trigger_priority = {
            "headline_emphasis": 0,
            "three_item_list": 1,
            "code_or_command": 2,
            "technology_comparison": 3,
            "number_or_rate": 4,
        }
        motion_segments = sorted(
            (
                segment
                for segment in segments
                if validated[segment["window_id"]]["scene_type"] == "MOTION_GRAPHIC"
            ),
            key=lambda segment: (
                trigger_priority.get(
                    decisions[segment["window_id"]].motion_trigger_type or "",
                    0,
                ),
                -float(segment["duration"]),
                segment["window_id"],
            ),
        )
        for segment in motion_segments:
            current_percent = 100 * motion_seconds / total_seconds
            if current_percent <= target_motion_percent:
                break
            window_id = segment["window_id"]
            stock_decision = WindowDecision(
                window_id=window_id,
                scene_type="STOCK_VIDEO",
                motion_trigger_type=None,
                motion_trigger_evidence=None,
                template_type=None,
                template_data=None,
                brand_slug=None,
                search_query=_fallback_stock_query(segment),
                rationale=(
                    "Returned to stock footage to meet the configured visual mix."
                ),
            )
            validated_stock = _validate_decision(stock_decision, segment)
            if validated_stock["scene_type"] != "STOCK_VIDEO":
                continue
            decisions[window_id] = stock_decision
            validated[window_id] = validated_stock
            motion_seconds -= float(segment["duration"])
            metadata["removed_window_ids"].append(window_id)

        final_percent = 100 * motion_seconds / total_seconds
        metadata["final_motion_graphic_percent"] = round(final_percent, 2)
        metadata["status"] = (
            "adjusted_down"
            if metadata["removed_window_ids"]
            else "no_downgrade_candidates"
        )
        return decisions, metadata

    if initial_percent >= minimum_motion_percent:
        return decisions, metadata

    candidates = []
    for segment in segments:
        if validated[segment["window_id"]]["scene_type"] != "STOCK_VIDEO":
            continue
        text = segment["text"]
        has_comparison = bool(
            re.search(
                r"\b(?:versus|vs\.?|compared with|compared to|whereas|rather than|"
                r"more than|less than|higher than|lower than)\b",
                text,
                re.IGNORECASE,
            )
        )
        has_code = bool(re.search(r"^\s*(?:\$|>)\s*\S|command|code snippet", text, re.IGNORECASE | re.MULTILINE))
        has_three_item_list = bool(
            re.search(r"\bfirst\b.+\bsecond\b.+\bthird\b|\bthree (?:ways|reasons|steps|items)\b", text, re.IGNORECASE)
        )
        if _number_mentions(text) or has_comparison or has_code or has_three_item_list:
            candidates.append(segment)
    if not candidates:
        metadata["status"] = "no_stock_candidates"
        return decisions, metadata

    metadata["attempted"] = True
    metadata["status"] = "attempted"
    candidate_payload = [
        {
            "window_id": segment["window_id"],
            "start": segment["start"],
            "end": segment["end"],
            "duration": segment["duration"],
            "text": segment["text"],
            "scene_intent": segment["scene_intent"],
            "entities": segment["entities"],
            "visual_queries": segment["visual_queries"],
            "numeric_values_in_window": [
                str(value) for value in sorted(_number_mentions(segment["text"]))
            ],
        }
        for segment in candidates
    ]
    existing_motion = [
        {
            "window_id": segment["window_id"],
            "start": segment["start"],
            "end": segment["end"],
            "text": segment["text"],
            "template_type": decisions[segment["window_id"]].template_type,
        }
        for segment in segments
        if validated[segment["window_id"]]["scene_type"] == "MOTION_GRAPHIC"
    ]
    prompt = f"""
The first pass selected only {motion_seconds:.3f} of {total_seconds:.3f} seconds
({initial_percent:.2f}%) as MOTION_GRAPHIC. The configured target is
{target_motion_percent}% (acceptable band {minimum_motion_percent}–{maximum_motion_percent}%).
Review the stock candidates below. Promote the strongest eligible candidates to
MOTION_GRAPHIC until the target is reached without exceeding the upper band. Use a
valid number/rate, explicit comparison, literal code/command, central supported
headline claim, or exactly three explicit list items from that candidate's text.
Every material amount or rate in a candidate is a priority cue; use bento_stat when
the number is relevant to the story, and do not skip a valid amount merely because
another window mentions the same general topic.
Keep candidates as STOCK_VIDEO when they have no concrete cue. Do not repeat a fact
already shown in an existing motion window. Preserve each window_id and its timing.
For all candidates, return exactly one decision using the same five template rules.
Never invent or calculate facts. For bento_stat.value, copy an amount stated in the
candidate text, converting words to digits only. For headline_highlight, every
headline claim must be directly supported by the candidate text.

STORY: {story_id}
TITLE: {title}
NEWS:
{news_text}

ALREADY SELECTED MOTION WINDOWS:
{json.dumps(existing_motion, ensure_ascii=False, indent=2)}

STOCK CANDIDATES TO REVIEW:
{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}
""".strip()
    try:
        response = client.responses.parse(
            model=model,
            reasoning={"effort": reasoning_effort},
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a careful visual director. Use only the supplied story "
                        "and exact timed-window text; follow the duration target when "
                        "valid concrete cues allow it."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            text_format=VisualDirectorResponse,
        )
        calibration = response.output_parsed
    except Exception as exc:
        metadata["status"] = "api_error"
        metadata["error_type"] = type(exc).__name__
        return decisions, metadata
    if calibration is None:
        metadata["status"] = "empty_response"
        return decisions, metadata

    candidate_ids = {segment["window_id"] for segment in candidates}
    returned: dict[str, WindowDecision] = {}
    for decision in calibration.scenes:
        if decision.window_id in candidate_ids and decision.window_id not in returned:
            returned[decision.window_id] = decision

    valid_motion: list[tuple[dict[str, Any], WindowDecision, dict[str, Any]]] = []
    for segment in candidates:
        proposed = returned.get(segment["window_id"])
        if proposed is None or proposed.scene_type != "MOTION_GRAPHIC":
            continue
        calibrated = _validate_decision(proposed, segment)
        if calibrated["scene_type"] == "MOTION_GRAPHIC":
            valid_motion.append((segment, proposed, calibrated))

    target_seconds = total_seconds * target_motion_percent / 100
    maximum_seconds = total_seconds * maximum_motion_percent / 100
    for segment, proposed, calibrated in valid_motion:
        if motion_seconds >= target_seconds:
            break
        if motion_seconds + segment["duration"] > maximum_seconds:
            continue
        decisions[segment["window_id"]] = proposed
        validated[segment["window_id"]] = calibrated
        motion_seconds += segment["duration"]
        metadata["added_window_ids"].append(segment["window_id"])

    final_percent = 100 * motion_seconds / total_seconds if total_seconds else 0.0
    metadata["final_motion_graphic_percent"] = round(final_percent, 2)
    metadata["status"] = "adjusted" if metadata["added_window_ids"] else "no_additional_valid_cues"
    return decisions, metadata


def _allocation(scenes: list[dict[str, Any]]) -> dict[str, Any]:
    total_seconds = sum(float(scene["end"]) - float(scene["start"]) for scene in scenes)
    motion_seconds = sum(
        float(scene["end"]) - float(scene["start"])
        for scene in scenes
        if scene["scene_type"] == "MOTION_GRAPHIC"
    )
    stock_seconds = max(0.0, total_seconds - motion_seconds)
    motion_percent = round(100 * motion_seconds / total_seconds, 2) if total_seconds else 0.0
    stock_percent = round(100 * stock_seconds / total_seconds, 2) if total_seconds else 0.0
    target_motion_percent = motion_graphic_target_percent()
    minimum_motion_percent, maximum_motion_percent = _motion_target_range(
        target_motion_percent
    )
    motion_count = sum(scene["scene_type"] == "MOTION_GRAPHIC" for scene in scenes)
    stock_count = len(scenes) - motion_count
    return {
        "basis": "timed_window_duration",
        "window_count": len(scenes),
        "total_window_seconds": round(total_seconds, 3),
        "stock_video_count": stock_count,
        "stock_video_seconds": round(stock_seconds, 3),
        "stock_video_percent": stock_percent,
        "motion_graphic_count": motion_count,
        "motion_graphic_seconds": round(motion_seconds, 3),
        "motion_graphic_percent": motion_percent,
        "target_stock_video_percent": [
            100 - maximum_motion_percent,
            100 - minimum_motion_percent,
        ],
        "target_motion_graphic_percent": [
            minimum_motion_percent,
            maximum_motion_percent,
        ],
        "target_motion_graphic_percent_target": target_motion_percent,
        "target_met": minimum_motion_percent <= motion_percent <= maximum_motion_percent,
    }


def plan_visual_scenes(
    news_text: str,
    segments: Sequence[Mapping[str, Any]],
    *,
    story_id: str = "",
    title: str = "",
    window_source: str = "provided_timestamped_segments",
    brand_slug: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Plan one stock or motion decision per timestamped media-selection window.

    ``client`` can be supplied by callers that already own an OpenAI client. When
    omitted, the function uses the project's OpenAI SDK and Automation/.env settings.
    The API is called only when this function is invoked.
    """
    story_text = str(news_text or "").strip()
    if not story_text:
        raise ValueError("news_text cannot be empty.")
    if not segments:
        raise ValueError("segments must contain at least one timestamped window.")

    normalized_segments = _normalize_segments(segments)
    target_motion_percent = motion_graphic_target_percent()
    default_brand = _canonical_brand_slug(brand_slug) or _infer_brand_slug(title)
    selected_model = _configured_model(model)
    selected_effort = _configured_reasoning_effort(reasoning_effort)
    prompt = _build_prompt(
        news_text=story_text,
        title=title,
        segments=normalized_segments,
        story_id=story_id,
        default_brand_slug=default_brand,
        target_motion_percent=target_motion_percent,
    )

    if client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for visual scene planning.")
        from openai import OpenAI  # noqa: PLC0415

        timeout_seconds = float(os.getenv("MOTION_VISUAL_DIRECTOR_TIMEOUT", "120"))
        client = OpenAI(timeout=timeout_seconds)

    retries = max(1, int(os.getenv("MOTION_VISUAL_DIRECTOR_API_RETRIES", "2")))
    response_data: VisualDirectorResponse | None = None
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = client.responses.parse(
                model=selected_model,
                reasoning={"effort": selected_effort},
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a careful visual director for a technology-news video. "
                            "Use only the supplied news and timed-window text. Never invent facts. "
                            "Return one structured decision per window."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                text_format=VisualDirectorResponse,
            )
            response_data = response.output_parsed
            if response_data is None:
                raise VisualDirectorError("OpenAI returned no structured visual plan.")
            break
        except Exception as exc:  # Retry transient API/schema failures, then report once.
            last_error = exc
            if attempt < retries:
                time.sleep(min(float(attempt), 2.0))
    if response_data is None:
        raise VisualDirectorError(f"Visual scene planning failed: {last_error}") from last_error

    expected_ids = [segment["window_id"] for segment in normalized_segments]
    returned_ids = [decision.window_id for decision in response_data.scenes]
    if len(returned_ids) != len(set(returned_ids)):
        raise VisualDirectorError("OpenAI returned duplicate window_id values.")
    if set(returned_ids) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(returned_ids))
        unexpected = sorted(set(returned_ids) - set(expected_ids))
        raise VisualDirectorError(
            f"Visual plan windows do not match input; missing={missing}, unexpected={unexpected}."
        )

    decisions = {decision.window_id: decision for decision in response_data.scenes}
    validated_decisions = {
        segment["window_id"]: _validate_decision(
            decisions[segment["window_id"]],
            segment,
        )
        for segment in normalized_segments
    }
    decisions, quota_adjustment = _calibrate_motion_share(
        client=client,
        model=selected_model,
        reasoning_effort=selected_effort,
        news_text=story_text,
        title=title,
        story_id=story_id,
        segments=normalized_segments,
        decisions=decisions,
        validated=validated_decisions,
        target_motion_percent=target_motion_percent,
    )

    logo_cache: dict[str, str | None] = {}
    planned_scenes: list[dict[str, Any]] = []
    for segment in normalized_segments:
        decision = decisions[segment["window_id"]]
        validated = _validate_decision(decision, segment)
        resolved_slug = None
        if validated["scene_type"] == "MOTION_GRAPHIC":
            resolved_slug = (
                _canonical_brand_slug(segment.get("brand_slug"))
                or validated["brand_slug"]
                or _infer_brand_slug(f"{title} {segment['text']}")
                or default_brand
            )
        logo_svg = _resolve_logo_svg(resolved_slug, logo_cache) if resolved_slug else None
        template_data = validated["template_data"]
        if template_data is not None and logo_svg:
            template_data = {**template_data, "logo_svg": logo_svg}

        planned_scenes.append(
            {
                "window_id": segment["window_id"],
                "story_id": story_id,
                "start": segment["start"],
                "end": segment["end"],
                "duration": segment["duration"],
                "timeline_start": segment["timeline_start"],
                "timeline_end": segment["timeline_end"],
                "text": segment["text"],
                "scene_type": validated["scene_type"],
                "motion_trigger_type": validated["motion_trigger_type"],
                "motion_trigger_evidence": validated["motion_trigger_evidence"],
                "template_type": validated["template_type"],
                "template_data": template_data,
                "brand_slug": resolved_slug,
                "brand_logo_svg": logo_svg,
                "search_query": validated["search_query"],
                "rationale": validated["rationale"],
            }
        )

    return {
        "schema_version": 1,
        "story_id": story_id,
        "title": title,
        "window_source": window_source,
        "model": selected_model,
        "reasoning_effort": selected_effort,
        "allocation": _allocation(planned_scenes),
        "quota_adjustment": quota_adjustment,
        "scenes": planned_scenes,
    }
