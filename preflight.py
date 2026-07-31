from __future__ import annotations

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

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

from project_paths import (  # noqa: E402 - .env must be loaded first
    AUTOMATION_ROOT,
    PROJECTS_ROOT,
    existing_project_numbers,
    next_project_number,
    project_name,
)
from tooling import find_executable  # noqa: E402 - .env must be loaded first
from storyboard_stage import StoryboardSettings  # noqa: E402
from media_index import MediaIndexSettings  # noqa: E402
from media_selection_stage import MediaSelectionSettings  # noqa: E402
from caption_stage import CaptionSettings  # noqa: E402
from effects_stage import EffectsSettings, SUPPORTED_AUDIO  # noqa: E402


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


def git_ignores_env() -> tuple[bool, str]:
    if not (AUTOMATION_ROOT / ".git").exists():
        return False, "Git repository is not initialized."

    command = [
        "git",
        "-c",
        f"safe.directory={AUTOMATION_ROOT.as_posix()}",
        "check-ignore",
        "-q",
        ".env",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=AUTOMATION_ROOT,
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
        PROJECTS_ROOT == AUTOMATION_ROOT.parent,
        str(PROJECTS_ROOT),
    )
    add(
        "Separated roots",
        AUTOMATION_ROOT != PROJECTS_ROOT,
        "Automation code and numbered video projects use different directories.",
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
    ignored, ignore_detail = git_ignores_env()
    add("Secret exclusion", ignored, ignore_detail)

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
                "max paid requests/run="
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

    try:
        caption_settings = CaptionSettings.from_environment()
        caption_settings.validate()
    except (RuntimeError, ValueError) as exc:
        add("Basic SRT configuration", False, str(exc))
    else:
        add(
            "Basic SRT configuration",
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
        add(
            "Milestone 6 configuration",
            (
                (not effects_settings.tv_frame_enabled or effects_settings.tv_frame_file.is_file())
                and (not effects_settings.music_enabled or any(path and path.is_file() for path in music_files))
                and effects_settings.editing_reference_file.is_file()
            ),
            (
                f"motion={effects_settings.motion_scale:.3f}; "
                f"TV frame={effects_settings.tv_frame_file}; "
                f"music tracks={len(music_files)}; "
                f"reference={effects_settings.editing_reference_file.name}"
            ),
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
        path = AUTOMATION_ROOT / filename
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
