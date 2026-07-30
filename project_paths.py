from __future__ import annotations

import os
import re
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_FOLDER_PATTERN = re.compile(r"^Video ([1-9][0-9]*)$")


def _configured_path(variable: str, default: Path) -> Path:
    raw_value = os.getenv(variable, "").strip()
    path = Path(raw_value).expanduser() if raw_value else default
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path.resolve()


AUTOMATION_ROOT = _configured_path("AUTOMATION_ROOT", SCRIPT_DIR)
PROJECTS_ROOT = _configured_path("PROJECTS_ROOT", AUTOMATION_ROOT.parent)


def project_name(video_number: int) -> str:
    if video_number < 1:
        raise ValueError("Video number must be 1 or greater.")
    return f"Video {video_number}"


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

