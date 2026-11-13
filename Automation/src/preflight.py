from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_locations import (  # noqa: E402 - .env must be loaded first
    AUTOMATION_ROOT,
    CONFIG_ROOT,
    DATA_ROOT,
    DOCS_ROOT,
    NEWS_ARCHIVE_ROOT,
    PROJECTS_ROOT,
    REFERENCES_ROOT,
    SCRIPTS_ROOT,
    SOURCE_ROOT as CENTRAL_SOURCE_ROOT,
    WORKSPACE_ROOT,
    existing_project_numbers,
    next_project_number,
    project_name,
)
from tooling import find_executable  # noqa: E402 - .env must be loaded first
from storyboard_stage import StoryboardSettings  # noqa: E402
from media_index import MediaIndexSettings  # noqa: E402
from media_selection_stage import MediaSelectionSettings  # noqa: E402
from unsplash_media_stage import UnsplashMediaSettings  # noqa: E402
from caption_stage import CaptionSettings  # noqa: E402
from effects_stage import EffectsSettings, SUPPORTED_AUDIO  # noqa: E402
from render_stage import RenderSettings  # noqa: E402
from publish_stage import PublishSettings  # noqa: E402
from youtube_upload import YouTubeUploadSettings  # noqa: E402
from news_archive import ArchiveSettings  # noqa: E402
from news_pipeline import validate_configuration as validate_news_configuration  # noqa: E402
from env_doctor import inspect_env  # noqa: E402


PLACEHOLDER_MARKERS = (
    "BURAYA_",
    "YOUR_",
    "CHANGE_ME",
    "REPLACE_ME",
)


@dataclass
class Check:
    name: str
    status: str
    detail: str


def configured_secret(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value) and not any(marker in value.upper() for marker in PLACEHOLDER_MARKERS)


def numbered_elevenlabs_keys() -> list[str]:
    slots: dict[int, str] = {}
    env_path = SCRIPT_DIR / ".env"
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            match = re.match(
                r"^\s*ELEVENLABS_API_KEY_([1-9][0-9]*)\s*=\s*(.*?)\s*$",
                raw_line,
            )
            if not match:
                continue
            slot = int(match.group(1))
            value = match.group(2).strip().strip("\"'")
            if (
                slot not in slots
                and value
                and not any(
                    marker in value.upper() for marker in PLACEHOLDER_MARKERS
                )
            ):
                slots[slot] = value

    for name, value in os.environ.items():
        match = re.fullmatch(r"ELEVENLABS_API_KEY_([1-9][0-9]*)", name)
        if not match:
            continue
        slot = int(match.group(1))
        if (
            slot not in slots
            and value.strip()
            and not any(marker in value.upper() for marker in PLACEHOLDER_MARKERS)
        ):
            slots[slot] = value.strip()
    return [f"ELEVENLABS_API_KEY_{slot}" for slot in sorted(slots)]


def _git_repo_root() -> Path | None:
    if (WORKSPACE_ROOT / ".git").exists():
        return WORKSPACE_ROOT
    if (AUTOMATION_ROOT / ".git").exists():
        return AUTOMATION_ROOT
    return None


def git_ignores_env() -> tuple[bool, str]:
    repo_root = _git_repo_root()
    if repo_root is None:
        return False, "Git repository is not initialized."

    env_target = "Automation/.env" if repo_root == WORKSPACE_ROOT else ".env"
    command = [
        "git",
        "-c",
        f"safe.directory={repo_root.as_posix()}",
        "check-ignore",
        "-q",
        env_target,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run git check-ignore: {exc}"
    return result.returncode == 0, (
        ".env is excluded from Git."
        if result.returncode == 0
        else ".env is not excluded from Git."
    )


def git_ignores_runtime_state() -> tuple[bool, str]:
    repo_root = _git_repo_root()
    if repo_root is None:
        return False, "Git repository is not initialized."
    prefix = "Automation/" if repo_root == WORKSPACE_ROOT else ""
    targets = (f"{prefix}data/processed_urls.json", f"{prefix}data/news_archive/index.json")
    ignored: list[str] = []
    for target in targets:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo_root.as_posix()}",
                "check-ignore",
                "-q",
                target,
            ],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            ignored.append(target)
    passed = len(ignored) == len(targets)
    return passed, (
        "Runtime URL history and pulled-news archive are excluded from Git."
        if passed
        else f"Runtime paths not excluded from Git: {sorted(set(targets) - set(ignored))}"
    )


def writable_directory(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".preflight-",
            suffix=".tmp",
            dir=path,
            delete=True,
        ):
            pass
    except OSError as exc:
        return False, f"Not writable: {exc}"
    return True, f"Writable: {path}"


def collect_checks() -> list[Check]:
    checks: list[Check] = []

    def add(name: str, passed: bool, detail: str, *, warning: bool = False) -> None:
        status = "PASS" if passed else ("WARN" if warning else "FAIL")
        checks.append(Check(name=name, status=status, detail=detail))

    add(
        "Python",
        sys.version_info >= (3, 11),
        f"{sys.version.split()[0]} (requires 3.11 or newer)",
    )
    add(
        "Automation root",
        AUTOMATION_ROOT == SCRIPT_DIR,
        str(AUTOMATION_ROOT),
    )
    add(
        "Projects root",
        PROJECTS_ROOT != AUTOMATION_ROOT
        and PROJECTS_ROOT != WORKSPACE_ROOT,
        str(PROJECTS_ROOT),
    )
    add(
        "Separated roots",
        AUTOMATION_ROOT != PROJECTS_ROOT,
        "Automation code and numbered video projects use different directories.",
    )
    modular_roots = (
        CENTRAL_SOURCE_ROOT,
        CONFIG_ROOT,
        DATA_ROOT,
        DOCS_ROOT,
        REFERENCES_ROOT,
        SCRIPTS_ROOT,
    )
    add(
        "Modular Automation layout",
        all(path.is_dir() and path.parent == AUTOMATION_ROOT for path in modular_roots),
        ", ".join(path.name for path in modular_roots),
    )
    add(
        "Central file locations",
        (CENTRAL_SOURCE_ROOT / "file_locations.py").is_file(),
        str(CENTRAL_SOURCE_ROOT / "file_locations.py"),
    )
    add(
        "Milestone 10 orchestrator",
        (CENTRAL_SOURCE_ROOT / "news_studio.py").is_file()
        and (CENTRAL_SOURCE_ROOT / "ai_video_checkpoint.py").is_file()
        and (AUTOMATION_ROOT / "news-studio.bat").is_file(),
        "CLI, one-button launcher, optional AI-video checkpoint, lock, dry-run, "
        "and recovery entry points.",
    )

    automation_writable, automation_detail = writable_directory(AUTOMATION_ROOT)
    add("Automation writable", automation_writable, automation_detail)
    projects_writable, projects_detail = writable_directory(PROJECTS_ROOT)
    add("Projects writable", projects_writable, projects_detail)

    numbers = existing_project_numbers()
    next_number = next_project_number()
    add(
        "Project numbering",
        True,
        f"Found {numbers or 'none'}; next project is {project_name(next_number)}.",
    )
    add(
        "Next project available",
        not (PROJECTS_ROOT / project_name(next_number)).exists(),
        str(PROJECTS_ROOT / project_name(next_number)),
    )

    add(
        "Environment file",
        (AUTOMATION_ROOT / ".env").is_file(),
        str(AUTOMATION_ROOT / ".env"),
    )
    try:
        env_report = inspect_env(AUTOMATION_ROOT / ".env")
    except (OSError, RuntimeError) as exc:
        add("Environment structure", False, str(exc))
    else:
        env_clean = (
            env_report["encoding"] == "utf-8"
            and not env_report["has_utf8_bom"]
            and not env_report["duplicate_variables"]
            and not env_report["mojibake_comment_lines"]
        )
        add(
            "Environment structure",
            env_clean,
            (
                f"encoding={env_report['encoding']}; BOM={env_report['has_utf8_bom']}; "
                f"variables={env_report['variable_count']}; "
                f"duplicates={sorted(env_report['duplicate_variables'])}; "
                f"mojibake comments={env_report['mojibake_comment_lines']}"
            ),
        )
    ignored, ignore_detail = git_ignores_env()
    add("Secret exclusion", ignored, ignore_detail)
    ignored_state, state_detail = git_ignores_runtime_state()
    add("Runtime-state exclusion", ignored_state, state_detail)

    try:
        news_archive_settings = ArchiveSettings.from_environment()
        news_archive_settings.validate()
    except ValueError as exc:
        add("Pulled-news archive configuration", False, str(exc))
    else:
        add(
            "Pulled-news archive configuration",
            NEWS_ARCHIVE_ROOT.is_dir(),
            (
                f"archive={NEWS_ARCHIVE_ROOT}; exact SHA-256 first; "
                f"AI candidates={news_archive_settings.candidate_limit}; "
                f"local threshold={news_archive_settings.local_similarity_threshold:g}; "
                "AI duplicate confidence="
                f"{news_archive_settings.ai_duplicate_min_confidence:g}"
            ),
        )

    try:
        validate_news_configuration()
    except Exception as exc:  # noqa: BLE001 - preflight must report any startup defect.
        add("News pipeline startup", False, f"{type(exc).__name__}: {exc}")
    else:
        add(
            "News pipeline startup",
            True,
            "Configuration validation passed without RSS or provider requests.",
        )

    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    provider_key = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
    add(
        "AI provider",
        provider in {"openai", "gemini"},
        f"{provider}; {provider_key} is configured: {configured_secret(provider_key)}",
    )
    add(
        "AI API key",
        configured_secret(provider_key),
        f"{provider_key} is configured without exposing its value.",
    )
    add(
        "OpenAI storyboard/image key",
        configured_secret("OPENAI_API_KEY"),
        "OPENAI_API_KEY is configured without exposing its value.",
    )
    try:
        storyboard_settings = StoryboardSettings.from_environment()
        storyboard_settings.validate()
    except (RuntimeError, ValueError) as exc:
        add("Storyboard/image configuration", False, str(exc))
    else:
        add(
            "Storyboard/image configuration",
            True,
            (
                f"planner={storyboard_settings.planner_model}; "
                f"image={storyboard_settings.image_model}; "
                f"quality={storyboard_settings.image_quality}; "
                f"size={storyboard_settings.image_size}; "
                f"images/story={storyboard_settings.images_per_story}; "
                "max paid requests/run="
                f"{storyboard_settings.maximum_image_requests_per_run}"
            ),
        )
        try:
            planned_story_count = int(os.getenv("TOP_N", "5"))
        except ValueError:
            planned_story_count = 5
        required_image_requests = (
            max(1, planned_story_count) * storyboard_settings.images_per_story
        )
        add(
            "Full-run image request ceiling",
            storyboard_settings.maximum_image_requests_per_run
            >= required_image_requests,
            (
                f"requires up to {required_image_requests} for "
                f"{planned_story_count} stories; configured="
                f"{storyboard_settings.maximum_image_requests_per_run}"
            ),
        )

    try:
        media_index_settings = MediaIndexSettings.from_environment()
        media_index_settings.validate()
    except (RuntimeError, ValueError) as exc:
        add("Media index configuration", False, str(exc))
    else:
        add(
            "Media index configuration",
            True,
            (
                f"index={media_index_settings.index_path}; "
                f"roots={len(media_index_settings.roots)}"
            ),
        )
        for media_root in media_index_settings.roots:
            add(
                "User-confirmed no-copyright repository",
                media_root.path.is_dir(),
                str(media_root.path),
            )

    try:
        media_selection_settings = MediaSelectionSettings.from_environment()
        media_selection_settings.validate()
    except (RuntimeError, ValueError) as exc:
        add("Archive selection configuration", False, str(exc))
    else:
        add(
            "Archive selection configuration",
            True,
            (
                f"model={media_selection_settings.model}; "
                f"shortlist={media_selection_settings.shortlist_size}; "
                f"windows={media_selection_settings.minimum_window_seconds:g}-"
                f"{media_selection_settings.maximum_window_seconds:g}s"
            ),
        )
        if media_selection_settings.unsplash_fallback_enabled:
            try:
                unsplash_settings = UnsplashMediaSettings.from_environment()
                unsplash_settings.validate()
            except (RuntimeError, ValueError) as exc:
                add("Unsplash fallback configuration", False, str(exc))
            else:
                add(
                    "Unsplash fallback configuration",
                    True,
                    (
                        f"search cap="
                        f"{media_selection_settings.unsplash_max_searches_per_video}; "
                        f"download cap="
                        f"{media_selection_settings.unsplash_max_downloads_per_video}; "
                        f"query planner="
                        f"{media_selection_settings.unsplash_query_model}/"
                        f"{media_selection_settings.unsplash_query_reasoning_effort}; "
                        f"destination={unsplash_settings.destination}"
                    ),
                )
        else:
            add("Unsplash fallback configuration", True, "Disabled.")
    try:
        caption_settings = CaptionSettings.from_environment()
        caption_settings.validate()
    except (RuntimeError, ValueError) as exc:
        add("Caption configuration", False, str(exc))
    else:
        add(
            "Caption configuration",
            True,
            (
                f"lines={caption_settings.max_lines}; "
                f"characters/line={caption_settings.max_characters_per_line}; "
                f"words/cue={caption_settings.max_words_per_caption}; "
                f"minimum={caption_settings.minimum_duration_seconds:g}s"
            ),
        )

    try:
        effects_settings = EffectsSettings.from_environment()
        effects_settings.validate()
    except (RuntimeError, ValueError) as exc:
        add("Milestone 6 configuration", False, str(exc))
    else:
        music_files = (
            [effects_settings.music_file]
            if effects_settings.music_file is not None
            else [
                path
                for path in effects_settings.music_directory.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO
            ]
            if effects_settings.music_directory.is_dir()
            else []
        )
        first_music_valid = (
            effects_settings.first_music_file is None
            or (
                effects_settings.first_music_file.is_file()
                and effects_settings.first_music_file.suffix.lower()
                in SUPPORTED_AUDIO
            )
        )
        add(
            "Milestone 6 configuration",
            (
                (not effects_settings.tv_frame_enabled or effects_settings.tv_frame_file.is_file())
                and (
                    not effects_settings.image_tv_effect_enabled
                    or effects_settings.image_tv_effect_file.is_file()
                )
                and (not effects_settings.music_enabled or any(path and path.is_file() for path in music_files))
                and (not effects_settings.music_enabled or first_music_valid)
                and (
                    not effects_settings.paper_sfx_enabled
                    or effects_settings.paper_sfx_file.is_file()
                )
                and (
                    not effects_settings.shutter_sfx_enabled
                    or effects_settings.shutter_sfx_file.is_file()
                )
                and effects_settings.editing_reference_file.is_file()
            ),
            (
                f"motion={effects_settings.motion_scale:.3f}; "
                f"max hold={effects_settings.maximum_visual_hold_seconds:g}s; "
                f"TV frame={effects_settings.tv_frame_file}; "
                f"animated TV effect={effects_settings.image_tv_effect_file} "
                f"at {effects_settings.image_tv_effect_opacity:.0%}; "
                f"procedural glitch={effects_settings.glitch_enabled}; "
                f"paper SFX={effects_settings.paper_sfx_file.name}; "
                f"shutter SFX={effects_settings.shutter_sfx_file.name}; "
                f"music tracks={len(music_files)}; "
                "first music="
                f"{effects_settings.first_music_file.name if effects_settings.first_music_file else 'playlist default'}; "
                "music section transition="
                f"{effects_settings.music_section_fade_seconds:g}s fade + "
                f"{effects_settings.music_section_silence_seconds:g}s silence; "
                f"reference={effects_settings.editing_reference_file.name}"
            ),
        )

    try:
        render_settings = RenderSettings.from_environment()
        render_settings.validate()
    except (RuntimeError, ValueError) as exc:
        add("Milestone 8 configuration", False, str(exc))
    else:
        add(
            "Milestone 8 configuration",
            True,
            (
                f"final={render_settings.width}x{render_settings.height}; "
                f"preview={render_settings.preview_width}x{render_settings.preview_height}; "
                f"fps={render_settings.fps}; preset={render_settings.preset}; "
                f"page flip={render_settings.page_flip_enabled} "
                f"({render_settings.page_flip_duration_seconds:g}s, skipped separators only); "
                "attribution=bottom-right safe edge"
            ),
        )

    try:
        publish_settings = PublishSettings.from_environment()
        publish_settings.validate()
    except (OSError, RuntimeError, ValueError) as exc:
        add("Milestone 9 configuration", False, str(exc))
    else:
        add(
            "Milestone 9 configuration",
            True,
            (
                f"copy model={publish_settings.model}; "
                "thumbnail=1280x720; fixed series alignment; "
                "AI-rendered or deterministic local headline"
            ),
        )

    try:
        upload_settings = YouTubeUploadSettings.from_environment()
        upload_settings.validate(require_credentials=False)
    except (OSError, RuntimeError, ValueError) as exc:
        add("YouTube upload configuration", False, str(exc), warning=True)
    else:
        add(
            "YouTube upload configuration",
            True,
            (
                f"privacy={upload_settings.privacy_status}; "
                f"category={upload_settings.category_id}; "
                f"chunk={upload_settings.chunk_size_bytes} bytes; resumable"
            ),
        )
        google_upload_modules = all(
            importlib.util.find_spec(name) is not None
            for name in ("googleapiclient", "google_auth_oauthlib")
        )
        add(
            "YouTube upload libraries",
            google_upload_modules,
            (
                "Google YouTube upload libraries are installed."
                if google_upload_modules
                else "Install requirements.txt before the first real upload."
            ),
            warning=True,
        )
        add(
            "YouTube OAuth client",
            upload_settings.client_secrets_file.is_file(),
            (
                str(upload_settings.client_secrets_file)
                if upload_settings.client_secrets_file.is_file()
                else (
                    "One-time setup remains: place the OAuth Desktop client JSON at "
                    f"{upload_settings.client_secrets_file}"
                )
            ),
            warning=True,
        )

    elevenlabs_keys = numbered_elevenlabs_keys()
    add(
        "ElevenLabs API key",
        bool(elevenlabs_keys),
        f"Configured slots: {', '.join(elevenlabs_keys) if elevenlabs_keys else 'none'}",
    )
    add(
        "ElevenLabs voice",
        configured_secret("ELEVENLABS_VOICE_ID"),
        "ELEVENLABS_VOICE_ID is configured."
        if configured_secret("ELEVENLABS_VOICE_ID")
        else "ELEVENLABS_VOICE_ID is missing or still a placeholder.",
    )

    for filename in (
        "feeds.opml",
        "channel_profile.txt",
        "gaming_feeds.opml",
        "gaming_channel_profile.txt",
    ):
        path = CONFIG_ROOT / filename
        add(
            filename,
            path.is_file() and path.stat().st_size > 0,
            str(path),
            warning=filename.startswith("gaming_"),
        )

    for executable in ("ffmpeg", "ffprobe"):
        resolved = find_executable(executable)
        add(
            executable,
            resolved is not None,
            str(resolved) if resolved else f"{executable} was not found.",
        )

    free_gib = shutil.disk_usage(PROJECTS_ROOT).free / (1024**3)
    add(
        "Free disk space",
        free_gib >= 10,
        f"{free_gib:.1f} GiB available; 10 GiB minimum recommended.",
        warning=True,
    )

    return checks


def main() -> int:
    json_output = "--json" in sys.argv[1:]
    checks = collect_checks()

    if json_output:
        print(
            json.dumps(
                {
                    "automation_root": str(AUTOMATION_ROOT),
                    "projects_root": str(PROJECTS_ROOT),
                    "next_project": project_name(next_project_number()),
                    "checks": [asdict(check) for check in checks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("NEWS STUDIO PREFLIGHT")
        print("=" * 68)
        print(f"Automation : {AUTOMATION_ROOT}")
        print(f"Projects   : {PROJECTS_ROOT}")
        print(f"Next       : {project_name(next_project_number())}")
        print("-" * 68)
        for check in checks:
            print(f"[{check.status:4}] {check.name}: {check.detail}")

    failures = [check for check in checks if check.status == "FAIL"]
    if not json_output:
        print("-" * 68)
        print(
            "READY" if not failures else f"NOT READY: {len(failures)} required check(s) failed."
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
