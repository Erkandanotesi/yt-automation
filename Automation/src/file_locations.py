from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv


SOURCE_ROOT = Path(__file__).resolve().parent
DEFAULT_AUTOMATION_ROOT = SOURCE_ROOT.parent
ENV_FILE = DEFAULT_AUTOMATION_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)


def _configured_path(variable: str, default: Path, *, base: Path) -> Path:
    raw_value = os.getenv(variable, "").strip()
    path = Path(raw_value).expanduser() if raw_value else default
    if not path.is_absolute():
        path = base / path
    return path.resolve()


AUTOMATION_ROOT = _configured_path(
    "AUTOMATION_ROOT", DEFAULT_AUTOMATION_ROOT, base=DEFAULT_AUTOMATION_ROOT
)
WORKSPACE_ROOT = _configured_path(
    "WORKSPACE_ROOT", AUTOMATION_ROOT.parent, base=AUTOMATION_ROOT
)
PROJECTS_ROOT = _configured_path(
    "PROJECTS_ROOT", WORKSPACE_ROOT / "Projects", base=AUTOMATION_ROOT
)

CONFIG_ROOT = AUTOMATION_ROOT / "config"
DATA_ROOT = AUTOMATION_ROOT / "data"
DOCS_ROOT = AUTOMATION_ROOT / "docs"
REFERENCES_ROOT = AUTOMATION_ROOT / "references"
SCRIPTS_ROOT = AUTOMATION_ROOT / "scripts"
TESTS_ROOT = AUTOMATION_ROOT / "tests"
TOOLS_ROOT = AUTOMATION_ROOT / "tools"

TECH_FEEDS_FILE = CONFIG_ROOT / "feeds.opml"
GAMING_FEEDS_FILE = CONFIG_ROOT / "gaming_feeds.opml"
TECH_PROFILE_FILE = CONFIG_ROOT / "channel_profile.txt"
GAMING_PROFILE_FILE = CONFIG_ROOT / "gaming_channel_profile.txt"
PROCESSED_URLS_FILE = DATA_ROOT / "processed_urls.json"
NEWS_HISTORY_RESET_FILE = DATA_ROOT / "news_history_reset.json"
NEWS_ARCHIVE_ROOT = _configured_path(
    "NEWS_ARCHIVE_PATH",
    DATA_ROOT / "news_archive",
    base=AUTOMATION_ROOT,
)
NEWS_ARCHIVE_ARTICLES_ROOT = NEWS_ARCHIVE_ROOT / "articles"
NEWS_ARCHIVE_SUMMARIES_ROOT = NEWS_ARCHIVE_ROOT / "summaries"
NEWS_ARCHIVE_INDEX_FILE = NEWS_ARCHIVE_ROOT / "index.json"
NEWS_SIMILARITY_CACHE_FILE = NEWS_ARCHIVE_ROOT / "similarity_cache.json"
YOUTUBE_SCHEDULE_STATE_FILE = DATA_ROOT / "youtube_schedule_state.json"
MEDIA_INDEX_ROOT = DATA_ROOT / "indexes"
MEDIA_INDEX_FILE = MEDIA_INDEX_ROOT / "media_index.json"
MEDIA_INDEX_REPORT_FILE = MEDIA_INDEX_ROOT / "media_index_report.json"
MEDIA_BENCHMARK_FILE = DATA_ROOT / "benchmarks" / "media_retrieval_benchmark.json"
EDITING_REFERENCE_FILE = REFERENCES_ROOT / "capcut_screenshot.png"
FFMPEG_ROOT = TOOLS_ROOT / "ffmpeg"
YOUTUBE_CREDENTIALS_ROOT = AUTOMATION_ROOT / ".youtube_credentials"
ASSETS_ROOT = _configured_path(
    "ASSETS_ROOT", WORKSPACE_ROOT / "Assets", base=AUTOMATION_ROOT
)
MODELS_ROOT = _configured_path(
    "MODELS_ROOT", WORKSPACE_ROOT / "models", base=AUTOMATION_ROOT
)
MOTION_ENGINE_ROOT = _configured_path(
    "MOTION_ENGINE_ROOT", WORKSPACE_ROOT / "motion_engine", base=AUTOMATION_ROOT
)
AUDIO_ASSETS_ROOT = ASSETS_ROOT / "Audio"
BACKGROUND_MUSIC_ROOT = AUDIO_ASSETS_ROOT / "background_music"
AUDIO_SFX_ROOT = AUDIO_ASSETS_ROOT / "sfx"
SUBSCRIBE_SFX_ROOT = AUDIO_ASSETS_ROOT / "subscribe_sfx"
OVERLAYS_ROOT = ASSETS_ROOT / "Overlays"
SEPARATOR_EXAMPLE_ROOT = ASSETS_ROOT / "example_seperator"
TV_FRAME_FILE = OVERLAYS_ROOT / "tv_frame.png"
IMAGE_TV_EFFECT_FILE = OVERLAYS_ROOT / "green-screen-tv-effect.mov"
PAPER_SFX_FILE = AUDIO_SFX_ROOT / "paper_sound.MP3"
SHUTTER_SFX_FILE = AUDIO_SFX_ROOT / "shutter_sound.MP3"

PROJECT_FOLDER_PATTERN = re.compile(r"^Video ([1-9][0-9]*)$")
_VIDEO_SEGMENT_PATTERN = re.compile(r"(?:^|/)(Video [1-9][0-9]*)(?:/(.*))?$", re.IGNORECASE)
_WORKSPACE_ANCHORS: tuple[tuple[str, Path], ...] = (
    ("Assets", ASSETS_ROOT),
    ("Automation", AUTOMATION_ROOT),
    ("models", MODELS_ROOT),
    ("motion_engine", MOTION_ENGINE_ROOT),
    ("Projects", PROJECTS_ROOT),
)


def resolve_portable_path(raw_path: str | Path | None, video_dir: Path | None = None) -> Path:
    """Resolve stored manifest paths across workspace moves or project folder relocation."""
    text = str(raw_path or "").strip()
    if not text:
        return Path("")
    candidate = Path(text)
    if candidate.exists():
        return candidate

    normalized = text.replace("\\", "/")
    if video_dir is not None:
        marker = f"/{video_dir.name}/"
        idx = normalized.casefold().rfind(marker.casefold())
        if idx >= 0:
            suffix = normalized[idx + len(marker) :].lstrip("/")
            relocated = video_dir / Path(suffix) if suffix else video_dir
            if relocated.exists():
                return relocated

    video_match = _VIDEO_SEGMENT_PATTERN.search(normalized)
    if video_match:
        video_name = video_match.group(1)
        suffix = (video_match.group(2) or "").lstrip("/")
        base_video_dir = video_dir if (video_dir is not None and video_dir.name.casefold() == video_name.casefold()) else (PROJECTS_ROOT / video_name)
        relocated = base_video_dir / Path(suffix) if suffix else base_video_dir
        if relocated.exists():
            return relocated

    best_candidate: Path | None = None
    for anchor_name, anchor_root in _WORKSPACE_ANCHORS:
        anchor_match = re.search(
            rf"(?:^|/){re.escape(anchor_name)}(?:/(.*))?$",
            normalized,
            re.IGNORECASE,
        )
        if anchor_match:
            suffix = (anchor_match.group(1) or "").lstrip("/")
            relocated = anchor_root / Path(suffix) if suffix else anchor_root
            if relocated.exists():
                return relocated
            if best_candidate is None:
                best_candidate = relocated

    if video_match:
        video_name = video_match.group(1)
        suffix = (video_match.group(2) or "").lstrip("/")
        base_video_dir = video_dir if (video_dir is not None and video_dir.name.casefold() == video_name.casefold()) else (PROJECTS_ROOT / video_name)
        return base_video_dir / Path(suffix) if suffix else base_video_dir

    return best_candidate if best_candidate is not None else candidate


def automation_path(*parts: str) -> Path:
    return AUTOMATION_ROOT.joinpath(*parts).resolve()


def source_file(filename: str) -> Path:
    return (SOURCE_ROOT / filename).resolve()


def project_name(video_number: int) -> str:
    if video_number < 1:
        raise ValueError("Video number must be 1 or greater.")
    return f"Video {video_number}"


def parse_video_number(value: str | int) -> int:
    if isinstance(value, int):
        number = value
    else:
        cleaned = value.strip()
        match = PROJECT_FOLDER_PATTERN.fullmatch(cleaned)
        if match:
            number = int(match.group(1))
        elif cleaned.isdigit():
            number = int(cleaned)
        else:
            raise ValueError(f"Invalid video reference: {value}")
    if number < 1:
        raise ValueError("Video number must be 1 or greater.")
    return number


def project_dir(video_number: int) -> Path:
    return PROJECTS_ROOT / project_name(video_number)


def existing_project_numbers() -> list[int]:
    if not PROJECTS_ROOT.exists():
        return []
    numbers: list[int] = []
    for child in PROJECTS_ROOT.iterdir():
        if not child.is_dir():
            continue
        match = PROJECT_FOLDER_PATTERN.fullmatch(child.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def latest_project_number() -> int | None:
    numbers = existing_project_numbers()
    return numbers[-1] if numbers else None


def next_project_number() -> int:
    latest = latest_project_number()
    return 1 if latest is None else latest + 1


def validate_project_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.parent != PROJECTS_ROOT.resolve():
        raise ValueError(f"Project must be directly under {PROJECTS_ROOT}: {resolved}")
    if PROJECT_FOLDER_PATTERN.fullmatch(resolved.name) is None:
        raise ValueError(f"Invalid project folder name: {resolved.name}")
    return resolved


def ensure_runtime_directories() -> None:
    for path in (
        DATA_ROOT,
        NEWS_ARCHIVE_ROOT,
        NEWS_ARCHIVE_ARTICLES_ROOT,
        NEWS_ARCHIVE_SUMMARIES_ROOT,
        MEDIA_INDEX_ROOT,
    ):
        path.mkdir(parents=True, exist_ok=True)
