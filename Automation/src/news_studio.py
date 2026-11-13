from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from file_locations import (
    AUTOMATION_ROOT,
    PROJECTS_ROOT,
    existing_project_numbers,
    latest_project_number,
    next_project_number,
    parse_video_number,
    project_dir,
    project_name,
    resolve_portable_path,
    source_file,
)
from file_utils import atomic_write_json, sha256_file, sha256_text
from lead_story_selection import (
    finalize_lead_story,
    selection_is_consistent,
    selection_is_pending,
)
from news_archive import NewsArchive


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCHEMA_VERSION = 1
FLOW_STAGES = (
    "news",
    "tts",
    "alignment",
    "manual_separators",
    "storyboard",
    "ai_story_videos",
    "archive_visuals",
    "captions",
    "effects",
    "preview_approval",
    "render",
    "publish",
)
COST_STAGES = {
    "news",
    "tts",
    "alignment",
    "storyboard",
    "archive_visuals",
    "publish",
}
TERMINAL_COMPLETE = "COMPLETE"
TERMINAL_WAITING = "WAITING"
TERMINAL_FAILED = "FAILED"
PROGRESS_WIDTH = 36
ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_CYAN = "\x1b[96m"
ANSI_GREEN = "\x1b[92m"
ANSI_YELLOW = "\x1b[93m"
ANSI_RED = "\x1b[91m"
STAGE_LABELS = {
    "news": "Collecting and writing news",
    "tts": "Creating ElevenLabs voiceovers",
    "alignment": "Aligning narration timestamps",
    "manual_separators": "Resolving news separators",
    "storyboard": "Planning and generating AI images",
    "ai_story_videos": "Resolving optional AI-generated story videos",
    "archive_visuals": "Selecting contextual archive images",
    "captions": "Creating timed captions",
    "effects": "Planning frames, effects, and music",
    "preview_approval": "Preparing and approving the preview",
    "render": "Encoding the final video",
    "publish": "Creating thumbnail and YouTube metadata",
}
# Keep the established keys for every existing stage. The optional video
# checkpoint gets its own mnemonic key so saved instructions remain valid.
RESUME_STAGE_KEYS = ("1", "2", "3", "4", "5", "v", "6", "7", "8", "9", "a", "b")
NETWORK_FAILURE_PATTERNS = (
    r"name resolution error|nameresolutionerror|getaddrinfo failed",
    r"temporary failure in name resolution|nodename nor servname provided",
    r"failed to establish a new connection|network is unreachable",
    r"connection (?:timed out|reset|aborted|refused)",
    r"connecttimeout|readtimeout|proxyerror|remotedisconnected",
    r"max retries exceeded with url",
    r"winerror\s+(?:10051|10054|10060|10061|11001)",
)
FAST_DEFAULTS_ENV = "AUTOMATION_FAST_DEFAULTS"
GUI_BRIDGE_ENV = "NEWS_STUDIO_GUI_BRIDGE"


class NetworkStageError(RuntimeError):
    def __init__(self, stage: str, log_path: Path) -> None:
        super().__init__(f"Stage {stage} lost network connectivity. Log: {log_path}")
        self.stage = stage
        self.log_path = log_path


def _is_network_failure(output: str) -> bool:
    normalized = output.casefold()
    return any(re.search(pattern, normalized) for pattern in NETWORK_FAILURE_PATTERNS)


def _internet_available(timeout_seconds: float = 4.0) -> bool:
    configured = os.getenv(
        "NETWORK_PROBE_TARGETS",
        "api.openai.com:443,api.elevenlabs.io:443,www.google.com:443,1.1.1.1:443",
    )
    for value in configured.split(","):
        host, _, raw_port = value.strip().partition(":")
        if not host:
            continue
        try:
            with socket.create_connection(
                (host, int(raw_port or "443")),
                timeout=timeout_seconds,
            ):
                return True
        except (OSError, ValueError):
            continue
    return False


def _enable_windows_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def _console_color_enabled() -> bool:
    return bool(sys.stdout.isatty()) and _enable_windows_ansi()


def _windows_shift_pressed() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except (AttributeError, OSError):
        return False


def _paint(text: str, color: str, *, enabled: bool) -> str:
    return f"{color}{text}{ANSI_RESET}" if enabled else text


def clear_console() -> None:
    """Clear only an interactive terminal; redirected logs must remain intact."""
    if not sys.stdout.isatty():
        return
    if _enable_windows_ansi():
        print("\x1b[2J\x1b[H", end="", flush=True)
    elif os.name == "nt":
        os.system("cls")


def print_header(title: str, subtitle: str = "") -> None:
    color = _console_color_enabled()
    print(_paint("=" * 72, ANSI_CYAN, enabled=color))
    print(_paint(title, ANSI_BOLD + ANSI_CYAN, enabled=color))
    if subtitle:
        print(subtitle)
    print(_paint("=" * 72, ANSI_CYAN, enabled=color))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _quality_passed(path: Path) -> bool:
    return _read_json(path).get("quality", {}).get("status") == "passed"


def _file_matches(
    path_value: Any,
    expected_sha256: Any = None,
    *,
    video_dir: Path | None = None,
) -> bool:
    path = resolve_portable_path(path_value, video_dir=video_dir)
    if not path.is_file():
        return False
    return not expected_sha256 or sha256_file(path) == str(expected_sha256)


def _stage_state(video_dir: Path) -> dict:
    return _read_json(video_dir / "state.json").get("stages", {})


def _tts_complete(video_dir: Path) -> bool:
    project = _read_json(video_dir / "project_info.json")
    manifest = _read_json(video_dir / "Sesler" / "tts_info.json")
    stories = project.get("stories", [])
    files = manifest.get("files", [])
    if not stories or len(files) != len(stories):
        return False
    files_by_text = {
        Path(str(item.get("text_file", ""))).name: item for item in files
    }
    for story in stories:
        text_path = resolve_portable_path(story.get("text"), video_dir=video_dir)
        item = files_by_text.get(text_path.name)
        if (
            item is None
            or not text_path.is_file()
            or item.get("text_sha256")
            != sha256_text(text_path.read_text(encoding="utf-8").strip())
        ):
            return False
    for item in files:
        path = resolve_portable_path(item.get("audio_file"), video_dir=video_dir)
        if item.get("status") not in {"generated", "regenerated", "cached"} or not path.is_file():
            return False
        expected = item.get("audio_sha256")
        if expected and sha256_file(path) != expected:
            return False
    return True


def _news_complete(video_dir: Path) -> bool:
    project = _read_json(video_dir / "project_info.json")
    stories = project.get("stories", [])
    if project.get("status") != "complete" or not stories:
        return False
    if not selection_is_consistent(project):
        return False
    return all(
        _file_matches(item.get("text"), item.get("narration_sha256"), video_dir=video_dir)
        and _file_matches(item.get("original"), item.get("source_sha256"), video_dir=video_dir)
        for item in stories
    )


def _alignment_complete(video_dir: Path) -> bool:
    project = _read_json(video_dir / "project_info.json")
    timing = _read_json(video_dir / "story_timing.json")
    stories = timing.get("stories", [])
    project_story_ids = [item.get("story_id") for item in project.get("stories", [])]
    timing_story_ids = [item.get("story_id") for item in stories]
    if not project_story_ids or timing_story_ids != project_story_ids:
        return False
    tts = _read_json(video_dir / "Sesler" / "tts_info.json")
    return all(
        _file_matches(item.get("alignment_file"), item.get("alignment_sha256"), video_dir=video_dir)
        for item in tts.get("files", [])
    )


def _storyboard_complete(video_dir: Path) -> bool:
    project = _read_json(video_dir / "project_info.json")
    manifest = _read_json(video_dir / "04_storyboard" / "storyboard.json")
    timing = _read_json(video_dir / "story_timing.json")
    quality = manifest.get("quality", {})
    if quality.get("status") != "passed" or not quality.get(
        "all_images_generated_and_validated"
    ):
        return False
    if [item.get("story_id") for item in manifest.get("stories", [])] != [
        item.get("story_id") for item in project.get("stories", [])
    ]:
        return False
    images = [
        image
        for story in manifest.get("stories", [])
        for image in story.get("images", [])
    ]
    configured_images = manifest.get("configuration", {}).get(
        "images_per_story",
        quality.get("images_per_story"),
    )
    try:
        images_per_story = int(configured_images)
    except (TypeError, ValueError):
        images_per_story = None
    try:
        desired_images_per_story = int(
            os.getenv("AI_GENERATED_IMAGES_PER_STORY", "2")
        )
    except ValueError:
        return False
    if (
        images_per_story is not None
        and images_per_story != desired_images_per_story
    ):
        return False
    if images_per_story == 0:
        return not images and all(
            not story.get("images") for story in manifest.get("stories", [])
        )
    separators_skipped = bool(
        timing.get("quality", {}).get("manual_separators_skipped", False)
    )
    summary_roles_current = all(
        story.get("images")
        and story["images"][0].get("role")
        == "skipped_separator_story_summary"
        for story in manifest.get("stories", [])
    )
    if separators_skipped != summary_roles_current:
        return False
    return bool(images) and all(
        image.get("validation", {}).get("status") == "passed"
        and _file_matches(
            image.get("output_file"),
            image.get("generation", {}).get("file_sha256"),
            video_dir=video_dir,
        )
        for image in images
    )


def _ai_story_videos_complete(video_dir: Path) -> bool:
    try:
        from ai_video_checkpoint import status

        current = status(parse_video_number(video_dir.name))
    except (ImportError, OSError, RuntimeError, ValueError):
        return False
    return current.get("status") in {"READY", "SKIPPED"}


def _archive_visuals_complete(video_dir: Path) -> bool:
    project = _read_json(video_dir / "project_info.json")
    manifest = _read_json(video_dir / "05_visuals" / "media_plan.json")
    if manifest.get("quality", {}).get("status") != "passed":
        return False
    checkpoint = video_dir / "04_ai_story_videos" / "ai_video_manifest.json"
    if not checkpoint.is_file() or manifest.get(
        "ai_video_checkpoint_sha256"
    ) != sha256_file(checkpoint):
        return False
    if [item.get("story_id") for item in manifest.get("stories", [])] != [
        item.get("story_id") for item in project.get("stories", [])
    ]:
        return False
    assets = [
        window.get("selected_asset", {})
        for story in manifest.get("stories", [])
        for window in story.get("windows", [])
    ]
    return bool(assets) and all(
        _file_matches(asset.get("absolute_path"), asset.get("sha256"), video_dir=video_dir)
        for asset in assets
    )


def _captions_complete(video_dir: Path, state: dict) -> bool:
    try:
        from caption_stage import caption_status

        current_status = caption_status(
            video_number=parse_video_number(video_dir.name)
        )
    except (ImportError, RuntimeError, ValueError):
        return False
    if not current_status.get("current", False):
        return False
    project = _read_json(video_dir / "project_info.json")
    manifest = _read_json(video_dir / "06_captions" / "captions.json")
    if (
        state.get("captions", {}).get("status") != "CAPTIONS_READY"
        or manifest.get("quality", {}).get("status") != "passed"
    ):
        return False
    if [item.get("story_id") for item in manifest.get("stories", [])] != [
        item.get("story_id") for item in project.get("stories", [])
    ]:
        return False
    if not _file_matches(
        manifest.get("combined_srt_file"),
        manifest.get("combined_srt_sha256"),
        video_dir=video_dir,
    ):
        return False
    if not _file_matches(
        manifest.get("combined_ass_file"),
        manifest.get("combined_ass_sha256"),
        video_dir=video_dir,
    ):
        return False
    return all(
        _file_matches(item.get("srt_file"), item.get("srt_sha256"), video_dir=video_dir)
        and _file_matches(item.get("ass_file"), item.get("ass_sha256"), video_dir=video_dir)
        and _file_matches(item.get("text_file"), item.get("text_sha256"), video_dir=video_dir)
        and _file_matches(item.get("alignment_file"), item.get("alignment_sha256"), video_dir=video_dir)
        for item in manifest.get("stories", [])
    )


def _effects_complete(video_dir: Path, state: dict) -> bool:
    manifest = _read_json(video_dir / "07_effects" / "effects_plan.json")
    if (
        state.get("effects", {}).get("status") != "EFFECTS_READY"
        or manifest.get("quality", {}).get("status") != "passed"
    ):
        return False
    try:
        from effects_stage import effects_status

        current = effects_status(parse_video_number(video_dir.name))
    except (ImportError, RuntimeError, ValueError):
        return False
    if current.get("status") != "EFFECTS_READY":
        return False
    assets = [item.get("asset", {}) for item in manifest.get("visual_events", [])]
    return bool(assets) and all(
        _file_matches(asset.get("file"), asset.get("sha256"), video_dir=video_dir)
        for asset in assets
    )


def _render_complete(video_dir: Path, state: dict) -> bool:
    report = _read_json(video_dir / "08_render" / "render_report.json")
    if (
        state.get("render", {}).get("status") != "RENDER_READY"
        or report.get("quality", {}).get("status") != "passed"
    ):
        return False
    return all(
        _file_matches(item.get("file"), item.get("qa", {}).get("sha256"), video_dir=video_dir)
        and item.get("qa", {}).get("status") == "passed"
        for item in (report.get("final", {}), report.get("preview", {}))
    )


def _render_recovery_action(video_dir: Path) -> str:
    """Reuse passing outputs when failure happened after media QA."""
    report = _read_json(video_dir / "08_render" / "render_report.json")
    final = report.get("final", {})
    final_qa = final.get("qa", {})
    if (
        final_qa.get("status") == "passed"
        and _file_matches(final.get("file"), final_qa.get("sha256"), video_dir=video_dir)
    ):
        preview = report.get("preview", {})
        preview_qa = preview.get("qa", {})
        if (
            report.get("quality", {}).get("status") == "passed"
            and preview_qa.get("status") == "passed"
            and _file_matches(preview.get("file"), preview_qa.get("sha256"), video_dir=video_dir)
        ):
            return "finalize"
        return "preview"
    return "render"


def _publish_complete(video_dir: Path, state: dict) -> bool:
    if state.get("publish", {}).get("status") != "PUBLISH_READY":
        return False
    package = video_dir / "09_publish" / "publish_package.json"
    manifest = _read_json(video_dir / "09_publish" / "youtube_upload_manifest.json")
    return (
        package.is_file()
        and manifest.get("chapters_embedded_in_description") is True
        and _file_matches(manifest.get("video_file"), manifest.get("video_sha256"), video_dir=video_dir)
        and _file_matches(
            manifest.get("thumbnail_file"), manifest.get("thumbnail_sha256"), video_dir=video_dir
        )
    )


def _preview_status(video_dir: Path) -> tuple[str, str]:
    proof = video_dir / "07_effects" / "milestone6_reference_60s.mp4"
    approval_path = video_dir / "orchestration" / "preview_approval.json"
    project = _read_json(video_dir / "project_info.json")
    timing = _read_json(video_dir / "story_timing.json")
    if [item.get("story_id") for item in project.get("stories", [])] != [
        item.get("story_id") for item in timing.get("stories", [])
    ]:
        return "NOT_STARTED", "The review preview is stale after news changed."
    try:
        from effects_stage import effects_status, reference_proof_renderer_fingerprint

        current_effects = effects_status(parse_video_number(video_dir.name))
    except (ImportError, OSError, RuntimeError, ValueError):
        current_effects = {"status": "NOT_STARTED"}
    if current_effects.get("status") != "EFFECTS_READY":
        return "NOT_STARTED", "The review preview is stale after timeline inputs changed."
    if not proof.is_file():
        return "NOT_STARTED", "The 60-second review preview has not been rendered."
    proof_report = _read_json(proof.with_suffix(".json"))
    if proof_report.get("renderer_sha256") != reference_proof_renderer_fingerprint():
        return "NOT_STARTED", "The review preview is stale after renderer code changed."
    effects_plan = video_dir / "07_effects" / "effects_plan.json"
    if (
        not effects_plan.is_file()
        or proof_report.get("effects_plan_sha256") != sha256_file(effects_plan)
    ):
        return "NOT_STARTED", "The review preview is stale after effects changed."
    approval = _read_json(approval_path)
    if (
        approval.get("status") == "APPROVED"
        and approval.get("preview_sha256") == sha256_file(proof)
    ):
        return TERMINAL_COMPLETE, "The current review preview is approved."
    stages = _stage_state(video_dir)
    if (
        stages.get("render", {}).get("status") == "RENDER_READY"
        and stages.get("publish", {}).get("status") == "PUBLISH_READY"
    ):
        return (
            TERMINAL_COMPLETE,
            "Legacy project already has a reviewed final render and publish package.",
        )
    return TERMINAL_WAITING, f"Review and approve: {proof}"


def collect_project_status(video_number: int) -> dict:
    video_dir = project_dir(video_number)
    state = _stage_state(video_dir)
    project = _read_json(video_dir / "project_info.json")
    preview_status, preview_detail = _preview_status(video_dir)
    checks = {
        "news": {
            "status": TERMINAL_COMPLETE
            if _news_complete(video_dir)
            else "NOT_STARTED",
            "detail": str(video_dir / "project_info.json"),
        },
        "tts": {
            "status": TERMINAL_COMPLETE if _tts_complete(video_dir) else "NOT_STARTED",
            "detail": str(video_dir / "Sesler" / "tts_info.json"),
        },
        "alignment": {
            "status": TERMINAL_COMPLETE
            if _alignment_complete(video_dir)
            else "NOT_STARTED",
            "detail": str(video_dir / "story_timing.json"),
        },
        "manual_separators": {
            "status": TERMINAL_COMPLETE
            if state.get("manual_separators", {}).get("status") in {"READY", "SKIPPED"}
            else TERMINAL_WAITING
            if (video_dir / "03_manual_separators").is_dir()
            else "NOT_STARTED",
            "detail": str(video_dir / "03_manual_separators"),
        },
        "storyboard": {
            "status": TERMINAL_COMPLETE
            if _storyboard_complete(video_dir)
            else "NOT_STARTED",
            "detail": str(video_dir / "04_storyboard" / "storyboard.json"),
        },
        "ai_story_videos": {
            "status": TERMINAL_COMPLETE
            if _ai_story_videos_complete(video_dir)
            else TERMINAL_WAITING
            if (video_dir / "04_ai_story_videos").is_dir()
            else "NOT_STARTED",
            "detail": str(video_dir / "04_ai_story_videos"),
        },
        "archive_visuals": {
            "status": TERMINAL_COMPLETE
            if _archive_visuals_complete(video_dir)
            else "NOT_STARTED",
            "detail": str(video_dir / "05_visuals" / "media_plan.json"),
        },
        "captions": {
            "status": TERMINAL_COMPLETE
            if _captions_complete(video_dir, state)
            else "NOT_STARTED",
            "detail": str(video_dir / "06_captions" / "captions.srt"),
        },
        "effects": {
            "status": TERMINAL_COMPLETE
            if _effects_complete(video_dir, state)
            else "NOT_STARTED",
            "detail": str(video_dir / "07_effects" / "effects_plan.json"),
        },
        "preview_approval": {
            "status": preview_status,
            "detail": preview_detail,
        },
        "render": {
            "status": TERMINAL_COMPLETE
            if _render_complete(video_dir, state)
            else "NOT_STARTED",
            "detail": str(video_dir / "08_render"),
        },
        "publish": {
            "status": TERMINAL_COMPLETE
            if _publish_complete(video_dir, state)
            else "NOT_STARTED",
            "detail": str(video_dir / "09_publish"),
        },
    }
    rebuild_warnings: list[str] = []
    effects_configuration_stale = False
    if checks["effects"]["status"] != TERMINAL_COMPLETE:
        try:
            from effects_stage import effects_status

            effects_configuration_stale = (
                effects_status(video_number).get("status") == "STALE"
            )
        except (ImportError, RuntimeError, ValueError):
            effects_configuration_stale = False
    if (
        checks["render"]["status"] == TERMINAL_COMPLETE
        and checks["publish"]["status"] == TERMINAL_COMPLETE
    ):
        for stage in FLOW_STAGES[:-3]:
            if checks[stage]["status"] == TERMINAL_COMPLETE:
                continue
            if stage == "effects" and effects_configuration_stale:
                rebuild_warnings.append(
                    "effects: configuration changed; effects and downstream outputs "
                    "must be rebuilt"
                )
                continue
            rebuild_warnings.append(
                f"{stage}: one or more original inputs moved or changed"
            )
            checks[stage] = {
                "status": TERMINAL_COMPLETE,
                "detail": (
                    "Satisfied by the checksum-verified final render and publish "
                    "package; source repair is needed only for a rebuild."
                ),
            }
    rebuild_plan = _rebuild_plan(video_dir)
    prepared_rebuild_stages = set(rebuild_plan.get("prepared_stages", []))
    for stage in rebuild_plan.get("pending_stages", []):
        checks[stage] = {
            "status": (
                TERMINAL_WAITING
                if stage in prepared_rebuild_stages
                else "NOT_STARTED"
            ),
            "detail": (
                "Explicit rebuild is waiting at this stage."
                if stage in prepared_rebuild_stages
                else "Queued by explicit rebuild-from-stage selection."
            ),
        }
    next_stage = next(
        (name for name in FLOW_STAGES if checks[name]["status"] != TERMINAL_COMPLETE),
        None,
    )
    lock = _read_json(video_dir / ".news_studio.lock")
    try:
        archive_status = {"status": "READY", **NewsArchive().status()}
    except RuntimeError as exc:
        archive_status = {"status": "ERROR", "detail": str(exc)}
    return {
        "schema_version": SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "project_directory": str(video_dir),
        "exists": video_dir.is_dir(),
        "stages": checks,
        "next_stage": next_stage,
        "complete": next_stage is None,
        "rebuild_warnings": rebuild_warnings,
        "rebuild_plan": rebuild_plan or None,
        "locked": bool(lock),
        "lock": lock or None,
        "news_archive": archive_status,
    }


STAGE_COST_PLAN_KEYS = {
    "news": ("news_text_requests", "news_duplicate_comparison_requests"),
    "tts": ("elevenlabs_tts_stories",),
    "alignment": ("forced_alignment_requests",),
    "storyboard": ("storyboard_planning_requests", "story_image_requests_remaining"),
    "archive_visuals": ("archive_selection_requests", "unsplash_query_planning_requests"),
    "publish": ("publishing_copy_requests", "thumbnail_image_requests"),
}

API_COST_LABELS = {
    "news_text_requests": "News summaries",
    "news_duplicate_comparison_requests": "Duplicate checks",
    "elevenlabs_tts_stories": "ElevenLabs voiceovers",
    "forced_alignment_requests": "Alignment",
    "storyboard_planning_requests": "Storyboard plan",
    "story_image_requests_remaining": "AI images remaining",
    "archive_selection_requests": "Archive selection",
    "unsplash_query_planning_requests": "Unsplash query plan",
    "publishing_copy_requests": "YouTube copy",
    "thumbnail_image_requests": "Thumbnail",
}


def api_cost_plan(
    video_number: int | None = None,
    stages: list[str] | tuple[str, ...] | None = None,
) -> dict:
    top_n = int(os.getenv("TOP_N", "5"))
    images_per_story = int(os.getenv("AI_GENERATED_IMAGES_PER_STORY", "2"))
    thumbnail_image_enabled = _bool_env("AI_GENERATED_THUMBNAIL_ENABLED", True)
    missing_images = top_n * images_per_story
    if video_number is not None:
        storyboard = _read_json(
            project_dir(video_number) / "04_storyboard" / "storyboard.json"
        )
        if storyboard:
            missing_images = max(
                0,
                int(
                    storyboard.get(
                        "planned_image_count",
                        top_n * images_per_story,
                    )
                )
                - int(storyboard.get("generated_image_count", 0)),
            )
    plan = {
        "news_text_requests": f"up to {top_n + 1}",
        "news_duplicate_comparison_requests": (
            "0 for exact or unrelated articles; one cached request per plausible "
            "cross-feed match"
        ),
        "elevenlabs_tts_stories": f"up to {top_n}",
        "forced_alignment_requests": "0 normally; only missing timestamp alignments",
        "storyboard_planning_requests": 1,
        "story_image_requests_remaining": missing_images,
        "archive_selection_requests": "bounded and cached; depends on uncovered windows",
        "unsplash_query_planning_requests": (
            "up to 5 Luna-low requests; unresolved stories only, cached"
        ),
        "publishing_copy_requests": 1,
        "thumbnail_image_requests": 1 if thumbnail_image_enabled else 0,
        "live_test_requests": 0,
    }
    if stages is None:
        return plan
    relevant_keys = {
        key
        for stage in stages
        for key in STAGE_COST_PLAN_KEYS.get(stage, ())
    }
    return {key: value for key, value in plan.items() if key in relevant_keys}


def api_cost_plan_items(plan: dict) -> list[str]:
    return [
        f"{API_COST_LABELS[key]}: {value}"
        for key, value in plan.items()
        if key in API_COST_LABELS
    ]


@dataclass
class StageResult:
    status: Literal["COMPLETE", "WAITING"]
    stage: str
    detail: str


class ProgressDisplay:
    """Keep an overall production bar below live subprocess output."""

    def __init__(
        self,
        video_number: int,
        *,
        stream=None,
        live: bool | None = None,
        color: bool | None = None,
        width: int = PROGRESS_WIDTH,
    ) -> None:
        self.video_number = video_number
        self.stream = stream or sys.stdout
        self.live = bool(self.stream.isatty()) if live is None else live
        self.color = self.live if color is None else color
        self.width = max(10, width)
        self.total = len(FLOW_STAGES)
        self._active = False
        self._completed = 0
        self._state = "QUEUED"
        self._stage_label = "Production queued"
        if self.color and os.name == "nt":
            self.color = _enable_windows_ansi()

    def _bar_line(self, completed: int, state: str) -> str:
        completed = min(self.total, max(0, completed))
        filled_count = round(self.width * completed / self.total)
        failed_count = 0
        if state == "FAILED" and completed < self.total:
            failed_stage_end = round(self.width * (completed + 1) / self.total)
            failed_count = min(
                self.width - filled_count,
                max(1, failed_stage_end - filled_count),
            )
        filled = "█" * filled_count
        failed = "█" * failed_count if self.color else "▓" * failed_count
        remaining = "░" * (self.width - filled_count - failed_count)
        if self.color and filled:
            filled = f"\x1b[32m{filled}\x1b[0m"
        if self.color and failed:
            failed = f"\x1b[31m{failed}\x1b[0m"
        percentage = round(completed * 100 / self.total)
        state_text = state
        if self.color:
            state_color = {
                "COMPLETE": ANSI_GREEN,
                "RUNNING": ANSI_CYAN,
                "WAITING": ANSI_YELLOW,
                "FAILED": ANSI_RED,
            }.get(state, ANSI_RESET)
            state_text = _paint(state, ANSI_BOLD + state_color, enabled=True)
        return (
            f"[{filled}{failed}{remaining}] {percentage:3d}%  "
            f"{completed:02d}/{self.total:02d}  {state_text} - {self._stage_label}"
        )

    def _clear_live_line(self) -> None:
        if self.live and self._active:
            self.stream.write("\r\x1b[2K")

    def _draw(self, completed: int, state: str) -> None:
        self._completed = completed
        self._state = state
        line = self._bar_line(completed, state)
        if self.live:
            self.stream.write(f"\r\x1b[2K{line}")
            self.stream.flush()
            self._active = True
        else:
            self.stream.write(line + "\n")
            self.stream.flush()

    def start_stage(self, stage: str, completed: int) -> None:
        if self.live and self.color:
            self.stream.write("\x1b[2J\x1b[H")
        elif self.live and self.stream is sys.stdout and os.name == "nt":
            os.system("cls")
        else:
            self._clear_live_line()
            if self._active:
                self.stream.write("\n")
        label = STAGE_LABELS.get(stage, stage.replace("_", " ").title())
        self._stage_label = label
        stage_number = FLOW_STAGES.index(stage) + 1
        heading = f"NEWS STUDIO  |  {project_name(self.video_number)}"
        if self.color:
            heading = f"{ANSI_BOLD}{ANSI_CYAN}{heading}{ANSI_RESET}"
        self.stream.write(f"{heading}\n")
        self.stream.write("-" * 72 + "\n")
        self.stream.write(f"Stage {stage_number:02d}/{self.total:02d}  {label}\n")
        self._active = False
        self._draw(completed, "RUNNING")

    def write_subprocess_line(self, line: str) -> None:
        if not self.live:
            self.stream.write(line)
            self.stream.flush()
            return
        normalized = line.strip()
        lowered = normalized.casefold()
        important = bool(
            re.search(r"\|\s*(?:warning|error|critical)\s*\|", lowered)
            or re.match(r"^\[(?:[^]]*\s)?(?:warning|error|failed)\]", lowered)
            or "saved:" in lowered
            or "complete:" in lowered
            or "completed." in lowered
        )
        if not important:
            return
        self._clear_live_line()
        if "error" in lowered or "failed" in lowered:
            message_color = ANSI_RED
        elif "warning" in lowered:
            message_color = ANSI_YELLOW
        else:
            message_color = ANSI_GREEN
        message = _paint(normalized, message_color, enabled=self.color)
        self.stream.write(message + "\n")
        self._active = False
        self._draw(self._completed, self._state)

    def complete_stage(self, completed: int) -> None:
        self._draw(completed, "COMPLETE")
        if self.live:
            self.stream.write("\n")
            self.stream.flush()
            self._active = False

    def waiting(self, completed: int) -> None:
        self._draw(completed, "WAITING")
        if self.live:
            self.stream.write("\n")
            self.stream.flush()
            self._active = False

    def network_waiting(self, attempt: int, retry_seconds: float) -> None:
        self._clear_live_line()
        message = (
            "[NETWORK WAITING] Internet connection was interrupted. "
            f"Keeping this stage open; reconnect check #{attempt} in "
            f"{retry_seconds:g}s. Press Ctrl+C only to cancel."
        )
        self.stream.write(
            _paint(message, ANSI_BOLD + ANSI_YELLOW, enabled=self.color) + "\n"
        )
        self._active = False
        self._draw(self._completed, "WAITING")

    def network_resumed(self) -> None:
        self._clear_live_line()
        self.stream.write(
            _paint(
                "[NETWORK RESTORED] Retrying the same stage from its cached state.",
                ANSI_GREEN,
                enabled=self.color,
            )
            + "\n"
        )
        self._active = False
        self._draw(self._completed, "RUNNING")

    def failed(self, completed: int) -> None:
        self._draw(completed, "FAILED")
        if self.live:
            self.stream.write("\n")
            self.stream.flush()
            self._active = False


class ProjectLock:
    def __init__(self, video_dir: Path, *, recover_stale: bool = False) -> None:
        self.video_dir = video_dir
        self.path = video_dir / ".news_studio.lock"
        self.token = uuid.uuid4().hex
        self.recover_stale = recover_stale

    @staticmethod
    def _pid_running(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def __enter__(self) -> "ProjectLock":
        self.video_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.recover_stale:
            existing = _read_json(self.path)
            if not self._pid_running(int(existing.get("pid", 0))):
                self.path.unlink(missing_ok=True)
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "token": self.token,
            "created_at_utc": utc_now(),
            "command": sys.argv,
        }
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            owner = _read_json(self.path)
            raise RuntimeError(
                f"Project is locked by PID {owner.get('pid', 'unknown')}. "
                "Use --recover-stale-lock only after confirming that process stopped."
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        current = _read_json(self.path)
        if current.get("token") == self.token:
            self.path.unlink(missing_ok=True)


def _event(video_dir: Path, event: dict[str, Any]) -> None:
    directory = video_dir / "orchestration"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"at_utc": utc_now(), **event}
    with (directory / "events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _skeptical_mode_enabled() -> bool:
    """Read the summary mode from the saved environment without exposing its contents."""
    env_file = AUTOMATION_ROOT / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "NEWS_STUDIO_BE_SKEPTICAL":
                return value.strip().strip('"').strip("'").casefold() not in {"", "0", "false", "no", "off"}
    except OSError:
        pass
    return os.getenv("NEWS_STUDIO_BE_SKEPTICAL", "false").strip().casefold() not in {"", "0", "false", "no", "off"}


def _rebuild_plan(video_dir: Path) -> dict:
    plan = _read_json(video_dir / "orchestration" / "rebuild_plan.json")
    if plan.get("status") != "ACTIVE":
        return {}
    pending = [stage for stage in plan.get("pending_stages", []) if stage in FLOW_STAGES]
    if not pending:
        return {}
    plan["pending_stages"] = pending
    plan["prepared_stages"] = [
        stage for stage in plan.get("prepared_stages", []) if stage in pending
    ]
    return plan


def _begin_rebuild(video_dir: Path, start_stage: str) -> dict:
    start_index = FLOW_STAGES.index(start_stage)
    plan = {
        "schema_version": 1,
        "status": "ACTIVE",
        "requested_start_stage": start_stage,
        "pending_stages": list(FLOW_STAGES[start_index:]),
        "prepared_stages": [],
        "created_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
    }
    atomic_write_json(video_dir / "orchestration" / "rebuild_plan.json", plan)
    return plan


def _update_rebuild_stage(
    video_dir: Path,
    stage: str,
    *,
    completed: bool,
) -> None:
    plan = _rebuild_plan(video_dir)
    if not plan or stage not in plan["pending_stages"]:
        return
    pending = list(plan["pending_stages"])
    prepared = list(plan.get("prepared_stages", []))
    if completed:
        pending.remove(stage)
        prepared = [item for item in prepared if item != stage]
    elif stage not in prepared:
        prepared.append(stage)
    plan["pending_stages"] = pending
    plan["prepared_stages"] = prepared
    plan["updated_at_utc"] = utc_now()
    if not pending:
        plan["status"] = "COMPLETE"
        plan["completed_at_utc"] = utc_now()
    atomic_write_json(video_dir / "orchestration" / "rebuild_plan.json", plan)


def _run_subprocess_once(
    video_dir: Path,
    stage: str,
    arguments: list[str],
    *,
    progress: ProgressDisplay | None = None,
) -> None:
    directory = video_dir / "orchestration" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = directory / f"{stamp}_{stage}.log"
    command = [sys.executable, str(source_file(arguments[0])), *arguments[1:]]
    _event(video_dir, {"type": "stage_started", "stage": stage, "command": command})
    started = time.monotonic()
    started_utc = utc_now()
    child_environment = os.environ.copy()
    child_environment["NEWS_STUDIO_DETAILED_ERRORS"] = "1"
    with log_path.open("w", encoding="utf-8") as log:
        log.write("NEWS STUDIO DETAILED STAGE LOG\n")
        log.write("=" * 76 + "\n")
        log.write(f"Stage       : {stage}\n")
        log.write(f"Video       : {video_dir.name}\n")
        log.write(f"Project     : {video_dir}\n")
        log.write(f"Started UTC : {started_utc}\n")
        log.write(f"Working dir : {AUTOMATION_ROOT}\n")
        log.write(f"Python      : {sys.executable}\n")
        log.write(f"Command     : {subprocess.list2cmdline(command)}\n")
        log.write("-" * 76 + "\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=AUTOMATION_ROOT,
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.write("\nPROCESS LAUNCH TRACEBACK\n")
            log.write(traceback.format_exc())
            log.write(f"\nFinished UTC : {utc_now()}\n")
            log.write("Exit code    : process did not start\n")
            log.flush()
            _event(
                video_dir,
                {
                    "type": "stage_failed",
                    "stage": stage,
                    "exit_code": None,
                    "log_file": str(log_path),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise RuntimeError(
                f"Stage {stage} could not start. Log: {log_path}"
            ) from exc
        assert process.stdout is not None
        output_tail: list[str] = []
        network_failure_seen = False
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            try:
                for output_line in process.stdout:
                    output_queue.put(output_line)
            finally:
                output_queue.put(None)

        output_thread = threading.Thread(target=read_output, daemon=True)
        output_thread.start()
        try:
            while True:
                if progress is not None and bool(
                    getattr(progress, "cancellation_requested", lambda: False)()
                ):
                    if os.name == "nt":
                        try:
                            process.send_signal(signal.CTRL_BREAK_EVENT)
                        except (OSError, ValueError):
                            process.terminate()
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    _event(video_dir, {"type": "stage_interrupted", "stage": stage})
                    raise KeyboardInterrupt
                try:
                    line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None and not output_thread.is_alive():
                        break
                    continue
                if line is None:
                    break
                network_failure_seen = network_failure_seen or _is_network_failure(line)
                output_tail.append(line)
                if len(output_tail) > 500:
                    del output_tail[:100]
                if progress is not None:
                    progress.write_subprocess_line(line)
                else:
                    print(line, end="")
                log.write(line)
                log.flush()
            return_code = process.wait()
        except KeyboardInterrupt:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                _event(video_dir, {"type": "stage_interrupted", "stage": stage})
            raise
        finally:
            output_thread.join(timeout=1)
            process.stdout.close()
        elapsed = round(time.monotonic() - started, 3)
        log.write("\n" + "-" * 76 + "\n")
        log.write(f"Finished UTC : {utc_now()}\n")
        log.write(f"Exit code    : {return_code}\n")
        log.write(f"Elapsed      : {elapsed:.3f} seconds\n")
        if return_code != 0:
            try:
                recovery = collect_project_status(parse_video_number(video_dir.name))
            except Exception as exc:  # noqa: BLE001
                recovery = {"status_error": f"{type(exc).__name__}: {exc}"}
            log.write("\nRECOVERY SNAPSHOT\n")
            log.write(json.dumps(recovery, ensure_ascii=False, indent=2))
            log.write("\n")
        log.flush()
    if return_code != 0:
        event_type = (
            "stage_network_interrupted"
            if network_failure_seen or _is_network_failure("".join(output_tail))
            else "stage_failed"
        )
        _event(
            video_dir,
            {
                "type": event_type,
                "stage": stage,
                "exit_code": return_code,
                "elapsed_seconds": elapsed,
                "log_file": str(log_path),
            },
        )
        if event_type == "stage_network_interrupted":
            raise NetworkStageError(stage, log_path)
        raise RuntimeError(f"Stage {stage} failed. Log: {log_path}")
    _event(
        video_dir,
        {
            "type": "stage_completed",
            "stage": stage,
            "elapsed_seconds": elapsed,
            "log_file": str(log_path),
        },
    )


def _wait_for_internet(
    *,
    progress: ProgressDisplay | None,
    attempt: int,
) -> None:
    try:
        configured_retry = float(os.getenv("NETWORK_RETRY_INTERVAL_SECONDS", "15"))
    except ValueError:
        configured_retry = 15.0
    retry_seconds = max(2.0, min(300.0, configured_retry))
    while True:
        if progress is not None:
            progress.network_waiting(attempt, retry_seconds)
        else:
            print(
                "[NETWORK WAITING] Internet connection was interrupted. "
                f"Keeping this stage open; checking again in {retry_seconds:g}s. "
                "Press Ctrl+C only to cancel.",
                flush=True,
            )
        time.sleep(retry_seconds)
        if _internet_available():
            if progress is not None:
                progress.network_resumed()
            else:
                print(
                    "[NETWORK RESTORED] Retrying the same stage from its cached state.",
                    flush=True,
                )
            return
        attempt += 1


def _run_subprocess(
    video_dir: Path,
    stage: str,
    arguments: list[str],
    *,
    progress: ProgressDisplay | None = None,
) -> None:
    attempt = 1
    while True:
        try:
            _run_subprocess_once(
                video_dir,
                stage,
                arguments,
                progress=progress,
            )
            return
        except NetworkStageError:
            _wait_for_internet(progress=progress, attempt=attempt)
            attempt += 1


def approve_preview(video_number: int) -> dict:
    video_dir = project_dir(video_number)
    proof = video_dir / "07_effects" / "milestone6_reference_60s.mp4"
    if not proof.is_file():
        raise FileNotFoundError(f"Review preview is missing: {proof}")
    approval = {
        "schema_version": 1,
        "status": "APPROVED",
        "video_number": video_number,
        "preview_file": str(proof),
        "preview_sha256": sha256_file(proof),
        "approved_at_utc": utc_now(),
    }
    path = video_dir / "orchestration" / "preview_approval.json"
    atomic_write_json(path, approval)
    _event(video_dir, {"type": "preview_approved", "preview_file": str(proof)})
    return approval


def _validate_manual_checkpoint(
    video_number: int,
    progress: ProgressDisplay | None,
) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            str(source_file("manual_checkpoint.py")),
            "--video",
            str(video_number),
            "--action",
            "validate",
        ],
        cwd=AUTOMATION_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in result.stdout.splitlines(keepends=True):
        if progress is not None:
            progress.write_subprocess_line(line)
        else:
            print(line, end="")
    return result.returncode == 0


def _validate_ai_video_checkpoint(
    video_number: int,
    progress: ProgressDisplay | None,
) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            str(source_file("ai_video_checkpoint.py")),
            "--video",
            str(video_number),
            "--action",
            "validate",
        ],
        cwd=AUTOMATION_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in result.stdout.splitlines(keepends=True):
        if progress is not None:
            progress.write_subprocess_line(line)
        else:
            print(line, end="")
    return result.returncode == 0


def _gui_checkpoint_choice(
    prompt: str,
    *,
    continue_label: str,
    asset_path: Path,
) -> str | None:
    """Wait for an explicit GUI checkpoint action without a timeout.

    A redirected CLI has no way to answer an interactive checkpoint, so it
    keeps the historical return-and-resume behavior.  The desktop bridge has a
    bidirectional input channel and can safely keep the current flow alive.
    """
    if not _bool_env(GUI_BRIDGE_ENV, False):
        return None
    return timed_choice(
        prompt,
        {
            "1": continue_label,
            "0": "Stop safely and continue later",
        },
        default="1",
        timeout_seconds=0,
        wait_indefinitely=True,
        asset_path=str(asset_path),
        asset_label="Open folder",
    )


def _saved_checkpoint_modes(
    video_dir: Path,
) -> tuple[Literal["manual", "skip", "defer"], Literal["manual", "skip", "defer"]]:
    """Restore durable manual-checkpoint decisions for a normal resume."""
    state = _stage_state(video_dir)
    separator_status = str(
        state.get("manual_separators", {}).get("status", "")
    ).upper()
    if separator_status == "SKIPPED":
        separator_mode: Literal["manual", "skip", "defer"] = "skip"
    elif separator_status in {"WAITING", "CANCELLED", "VALIDATING", "READY"}:
        separator_mode = "manual"
    else:
        separator_mode = "defer"

    ai_manifest = _read_json(
        video_dir / "04_ai_story_videos" / "ai_video_manifest.json"
    )
    ai_status = str(ai_manifest.get("status", "")).upper()
    if ai_status == "SKIPPED":
        ai_video_mode: Literal["manual", "skip", "defer"] = "skip"
    elif (
        ai_manifest.get("decision") == "use_ai_story_videos"
        and ai_status in {"WAITING", "READY"}
    ):
        ai_video_mode = "manual"
    else:
        ai_video_mode = "defer"
    return separator_mode, ai_video_mode


def execute_stage(
    video_number: int,
    stage: str,
    *,
    category: str,
    confirm_api_costs: bool,
    approve_current_preview: bool,
    manual_separator_mode: Literal["manual", "skip", "defer"] = "manual",
    ai_video_mode: Literal["manual", "skip", "defer"] = "skip",
    progress: ProgressDisplay | None = None,
    force_rebuild: bool = False,
) -> StageResult:
    video_dir = project_dir(video_number)
    if stage in COST_STAGES and not confirm_api_costs:
        return StageResult(
            TERMINAL_WAITING,
            stage,
            "API cost confirmation required. Review the dry-run and pass "
            "--confirm-api-costs.",
        )
    if stage == "news":
        # A completed news pipeline can be waiting only for the lead decision.
        # In that case, do not repeat paid collection or summary calls.
        if not selection_is_pending(video_dir):
            _run_subprocess(
                video_dir,
                stage,
                ["news_pipeline.py", "--video", str(video_number), "--category", category],
                progress=progress,
            )
        if selection_is_pending(video_dir):
            selection = finalize_lead_story(
                video_dir,
                choose=timed_choice,
                timeout_seconds=120.0,
            )
            _event(
                video_dir,
                {
                    "type": "lead_story_selected",
                    "story_id": selection["selected_story_id"],
                    "display_position": selection["selected_display_position"],
                    "decision_reused": selection["decision_reused"],
                },
            )
    elif stage == "tts":
        tts_provider = os.getenv("TTS_PROVIDER", "elevenlabs").strip().lower()
        if tts_provider in {"qwen", "qwen3", "qwen_tts", "local"}:
            tts_cmd = ["qwen_tts_local.py", "--video", str(video_number)]
        else:
            tts_cmd = ["elevenlabs_tts.py", "--video", str(video_number), "--compact"]
        _run_subprocess(
            video_dir,
            stage,
            tts_cmd,
            progress=progress,
        )
    elif stage == "alignment":
        _run_subprocess(
            video_dir,
            stage,
            ["alignment_stage.py", "--video", str(video_number)],
            progress=progress,
        )
    elif stage == "manual_separators":
        if manual_separator_mode == "defer":
            return StageResult(
                TERMINAL_WAITING,
                stage,
                "Choose manual separator production or skip it.",
            )
        if manual_separator_mode == "skip":
            _run_subprocess(
                video_dir,
                stage,
                [
                    "manual_checkpoint.py",
                    "--video",
                    str(video_number),
                    "--action",
                    "skip",
                ],
                progress=progress,
            )
            return StageResult(
                TERMINAL_COMPLETE,
                stage,
                "Manual separators skipped; images start at each chapter beginning.",
            )
        if not _validate_manual_checkpoint(video_number, progress):
            _run_subprocess(
                video_dir,
                stage,
                [
                    "manual_checkpoint.py",
                    "--video",
                    str(video_number),
                    "--action",
                    "prepare",
                ],
                progress=progress,
            )
            separator_directory = video_dir / "03_manual_separators"
            if not sys.stdin.isatty() and not _bool_env(GUI_BRIDGE_ENV, False):
                return StageResult(
                    TERMINAL_WAITING,
                    stage,
                    f"Add the separator files to {separator_directory}, then resume.",
                )
            while True:
                if sys.stdin.isatty():
                    try:
                        choice = input(
                            "\nThe folders are open. Save all separator files, then "
                            "press Enter to validate and continue (or type 0 to stop): "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        print("\nCheckpoint remains waiting; generated work was preserved.")
                        return StageResult(
                            TERMINAL_WAITING,
                            stage,
                            "Manual separator work was left waiting.",
                        )
                else:
                    choice = _gui_checkpoint_choice(
                        "Add the manual separator files, then validate and continue.",
                        continue_label="Validate separator files and continue",
                        asset_path=separator_directory,
                    )
                    if choice is None:
                        return StageResult(
                            TERMINAL_WAITING,
                            stage,
                            f"Add the separator files to {separator_directory}, then resume.",
                        )
                if choice == "0":
                    _run_subprocess(
                        video_dir,
                        stage,
                        [
                            "manual_checkpoint.py",
                            "--video",
                            str(video_number),
                            "--action",
                            "cancel",
                        ],
                        progress=progress,
                    )
                    return StageResult(
                        TERMINAL_WAITING,
                        stage,
                        "Manual separator work was cancelled and preserved.",
                    )
                if choice not in {"", "1"}:
                    print("Press Enter when ready, or type 0 to stop.")
                    continue
                if _validate_manual_checkpoint(video_number, progress):
                    break
                print(
                    "Separator validation did not pass. Fix the listed files, "
                    "then press Enter again."
                )
    elif stage == "storyboard":
        _run_subprocess(
            video_dir,
            stage,
            [
                "storyboard_stage.py",
                "--video",
                str(video_number),
                "--action",
                "all",
                "--confirm-cost",
            ],
            progress=progress,
        )
    elif stage == "ai_story_videos":
        if int(os.getenv("AI_GENERATED_IMAGES_PER_STORY", "2")) == 0:
            _run_subprocess(
                video_dir,
                stage,
                [
                    "ai_video_checkpoint.py",
                    "--video",
                    str(video_number),
                    "--action",
                    "skip",
                ],
                progress=progress,
            )
            return StageResult(
                TERMINAL_COMPLETE,
                stage,
                "AI story videos skipped because generated-image mode is disabled; "
                "stock videos and images remain in use.",
            )
        if ai_video_mode == "defer":
            return StageResult(
                TERMINAL_WAITING,
                stage,
                "Choose whether to provide AI-generated story videos.",
            )
        if ai_video_mode == "skip":
            _run_subprocess(
                video_dir,
                stage,
                [
                    "ai_video_checkpoint.py",
                    "--video",
                    str(video_number),
                    "--action",
                    "skip",
                ],
                progress=progress,
            )
            return StageResult(
                TERMINAL_COMPLETE,
                stage,
                "Optional AI-generated story videos skipped; storyboard images remain in use.",
            )
        ai_video_manifest = (
            video_dir / "04_ai_story_videos" / "ai_video_manifest.json"
        )
        # A missing manifest is the normal first-run state, not a validation
        # failure. Prepare it directly so the user never sees a false error.
        checkpoint_ready = (
            _validate_ai_video_checkpoint(video_number, progress)
            if ai_video_manifest.is_file()
            else False
        )
        if not checkpoint_ready:
            _run_subprocess(
                video_dir,
                stage,
                [
                    "ai_video_checkpoint.py",
                    "--video",
                    str(video_number),
                    "--action",
                    "prepare",
                ],
                progress=progress,
            )
            ai_video_directory = video_dir / "04_ai_story_videos"
            if not sys.stdin.isatty() and not _bool_env(GUI_BRIDGE_ENV, False):
                return StageResult(
                    TERMINAL_WAITING,
                    stage,
                    f"Add 1..5 video files to {ai_video_directory}, then resume.",
                )
            while True:
                if sys.stdin.isatty():
                    try:
                        choice = input(
                            "\nThe prompt sheet and folder are open. Save clips as "
                            "1, 2, 3, 4, 5, then press Enter to validate and continue "
                            "(or type 0 to stop): "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        print("\nAI-video checkpoint remains waiting; files were preserved.")
                        return StageResult(
                            TERMINAL_WAITING,
                            stage,
                            "AI-generated story videos were left waiting.",
                        )
                else:
                    choice = _gui_checkpoint_choice(
                        "Generate clips 1 through 5, then validate and continue.",
                        continue_label="Validate AI video files and continue",
                        asset_path=ai_video_directory,
                    )
                    if choice is None:
                        return StageResult(
                            TERMINAL_WAITING,
                            stage,
                            f"Add 1..5 video files to {ai_video_directory}, then resume.",
                        )
                if choice == "0":
                    return StageResult(
                        TERMINAL_WAITING,
                        stage,
                        "AI-generated story videos were left waiting.",
                    )
                if choice not in {"", "1"}:
                    print("Press Enter when all five clips are ready, or type 0 to stop.")
                    continue
                if _validate_ai_video_checkpoint(video_number, progress):
                    break
                print(
                    "AI-video validation did not pass. Fix the listed files, "
                    "then press Enter again."
                )
    elif stage == "archive_visuals":
        _run_subprocess(
            video_dir,
            stage,
            [
                "media_selection_stage.py",
                "--video",
                str(video_number),
                "--confirm-ai-cost",
            ],
            progress=progress,
        )
    elif stage == "captions":
        _run_subprocess(
            video_dir,
            stage,
            ["caption_stage.py", "--video", str(video_number), "--action", "generate"],
            progress=progress,
        )
    elif stage == "effects":
        _run_subprocess(
            video_dir,
            stage,
            ["effects_stage.py", "--video", str(video_number), "--action", "generate"],
            progress=progress,
        )
    elif stage == "preview_approval":
        proof = video_dir / "07_effects" / "milestone6_reference_60s.mp4"
        preview_status, _ = _preview_status(video_dir)
        if preview_status == "NOT_STARTED" or force_rebuild:
            _run_subprocess(
                video_dir,
                "preview",
                [
                    "effects_stage.py",
                    "--video",
                    str(video_number),
                    "--action",
                    "reference-proof",
                    "--proof-seconds",
                    "60",
                ],
                progress=progress,
            )
        if not approve_current_preview:
            return StageResult(
                TERMINAL_WAITING,
                stage,
                f"Review {proof}, then resume with --approve-preview.",
            )
        approve_preview(video_number)
    elif stage == "render":
        if _bool_env("REQUIRE_PREVIEW_APPROVAL", True):
            preview_status, _ = _preview_status(video_dir)
            if preview_status != TERMINAL_COMPLETE:
                return StageResult(
                    TERMINAL_WAITING,
                    stage,
                    "The current preview must be approved before the final render.",
                )
        render_action = (
            "render" if force_rebuild else _render_recovery_action(video_dir)
        )
        _run_subprocess(
            video_dir,
            stage,
            [
                "render_stage.py",
                "--video",
                str(video_number),
                "--action",
                render_action,
            ],
            progress=progress,
        )
    elif stage == "publish":
        publish_command = [
            "publish_stage.py",
            "--video",
            str(video_number),
            "--confirm-ai-cost",
        ]
        if _bool_env("AI_GENERATED_THUMBNAIL_ENABLED", True):
            publish_command.extend(
                ["--generate-thumbnail", "--confirm-thumbnail-cost"]
            )
        _run_subprocess(
            video_dir,
            stage,
            publish_command,
            progress=progress,
        )
    else:
        raise ValueError(f"Unknown stage: {stage}")
    return StageResult(TERMINAL_COMPLETE, stage, f"{stage} completed.")


def run_flow(
    video_number: int,
    *,
    category: str,
    confirm_api_costs: bool,
    approve_current_preview: bool,
    recover_stale_lock: bool,
    dry_run: bool,
    manual_separator_mode: Literal["manual", "skip", "defer"] = "manual",
    ai_video_mode: Literal["manual", "skip", "defer"] = "skip",
    progress: ProgressDisplay | None = None,
    start_stage: str | None = None,
) -> dict:
    before = collect_project_status(video_number)
    if start_stage is not None and start_stage not in FLOW_STAGES:
        raise ValueError(f"Unknown resume start stage: {start_stage}")
    start_index = FLOW_STAGES.index(start_stage) if start_stage else 0
    if start_stage is not None:
        incomplete_prerequisites = [
            stage
            for stage in FLOW_STAGES[:start_index]
            if before["stages"][stage]["status"] != TERMINAL_COMPLETE
        ]
        if incomplete_prerequisites:
            raise RuntimeError(
                f"Cannot continue from {start_stage}; complete these earlier "
                f"sections first: {', '.join(incomplete_prerequisites)}."
            )
    if start_stage is not None:
        # "Rebuild from here" is a pipeline boundary, not a one-stage retry.
        # Keep every downstream stage queued even when its previous artifact is
        # still checksum-valid; a persistent rebuild plan carries that intent
        # across interactive checkpoints such as preview approval.
        planned = list(FLOW_STAGES[start_index:])
    else:
        planned = [
            stage
            for stage in FLOW_STAGES
            if before["stages"][stage]["status"] != TERMINAL_COMPLETE
        ]
    if dry_run:
        return {
            "status": "DRY_RUN",
            "video_number": video_number,
            "planned_stages": planned,
            "api_cost_plan": api_cost_plan(video_number if before["exists"] else None),
            "writes_performed": False,
            "api_requests_performed": False,
        }
    if planned and any(stage in COST_STAGES for stage in planned) and not confirm_api_costs:
        first_cost = next(stage for stage in planned if stage in COST_STAGES)
        return {
            "status": "COST_CONFIRMATION_REQUIRED",
            "video_number": video_number,
            "next_stage": first_cost,
            "api_cost_plan": api_cost_plan(video_number if before["exists"] else None),
        }
    video_dir = project_dir(video_number)
    with ProjectLock(video_dir, recover_stale=recover_stale_lock):
        if start_stage is not None:
            _begin_rebuild(video_dir, start_stage)
        _event(
            video_dir,
            {
                "type": "flow_started",
                "planned_stages": planned,
                "requested_start_stage": start_stage,
                "skeptical": _skeptical_mode_enabled(),
            },
        )
        if not planned and progress is not None:
            progress.complete_stage(len(FLOW_STAGES))
        for stage in FLOW_STAGES[start_index:]:
            current = collect_project_status(video_number)
            rebuild_plan = _rebuild_plan(video_dir)
            rebuild_pending = stage in rebuild_plan.get("pending_stages", [])
            force_rebuild = (
                rebuild_pending
                and stage not in rebuild_plan.get("prepared_stages", [])
            )
            if (
                current["stages"][stage]["status"] == TERMINAL_COMPLETE
                and not rebuild_pending
            ):
                continue
            completed_before = sum(
                item["status"] == TERMINAL_COMPLETE
                for item in current["stages"].values()
            )
            if progress is not None:
                progress.start_stage(stage, completed_before)
            try:
                result = execute_stage(
                    video_number,
                    stage,
                    category=category,
                    confirm_api_costs=confirm_api_costs,
                    approve_current_preview=approve_current_preview,
                    manual_separator_mode=manual_separator_mode,
                    ai_video_mode=ai_video_mode,
                    progress=progress,
                    force_rebuild=force_rebuild,
                )
            except Exception:
                if progress is not None:
                    progress.failed(completed_before)
                raise
            if result.status == TERMINAL_WAITING:
                _update_rebuild_stage(video_dir, stage, completed=False)
                if progress is not None:
                    progress.waiting(completed_before)
                _event(
                    video_dir,
                    {"type": "flow_waiting", "stage": stage, "detail": result.detail},
                )
                return {
                    "status": "WAITING",
                    "video_number": video_number,
                    "stage": stage,
                    "detail": result.detail,
                }
            _update_rebuild_stage(video_dir, stage, completed=True)
            if progress is not None:
                if stage == "preview_approval":
                    # Approval has just been written and hash-validated. Avoid a
                    # redundant full-project checksum scan here; the next loop
                    # iteration performs the authoritative pre-render scan.
                    completed_after = completed_before + 1
                else:
                    after_stage = collect_project_status(video_number)
                    completed_after = sum(
                        item["status"] == TERMINAL_COMPLETE
                        for item in after_stage["stages"].values()
                    )
                progress.complete_stage(max(completed_before + 1, completed_after))
        final = collect_project_status(video_number)
        _event(video_dir, {"type": "flow_completed", "complete": final["complete"]})
        return {
            "status": "COMPLETE" if final["complete"] else "INCOMPLETE",
            "video_number": video_number,
            "project_directory": str(video_dir),
            "publish_directory": str(video_dir / "09_publish"),
        }


def validate_project(video_number: int) -> dict:
    preflight = subprocess.run(
        [sys.executable, str(source_file("preflight.py")), "--json"],
        cwd=AUTOMATION_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    status = collect_project_status(video_number)
    failures = [
        name
        for name, value in status["stages"].items()
        if value["status"] not in {TERMINAL_COMPLETE, TERMINAL_WAITING}
    ]
    return {
        "status": "VALID" if preflight.returncode == 0 and not failures else "INVALID",
        "preflight_passed": preflight.returncode == 0,
        "video_number": video_number,
        "incomplete_or_invalid_stages": failures,
        "project": status,
    }


def _print_stage_table(status: dict) -> None:
    print(f"{status['video_name']} — {status['project_directory']}")
    print("-" * 72)
    for stage in FLOW_STAGES:
        value = status["stages"][stage]
        print(f"{stage:22} {value['status']:12} {value['detail']}")
    print("-" * 72)
    print(f"Next stage: {status['next_stage'] or 'none — production complete'}")
    if status.get("rebuild_warnings"):
        print("Rebuild warnings:")
        for warning in status["rebuild_warnings"]:
            print(f"  - {warning}")


_last_choice_custom_text: str | None = None


def consume_last_choice_custom_text() -> str | None:
    global _last_choice_custom_text
    value = _last_choice_custom_text
    _last_choice_custom_text = None
    return value


def timed_choice(
    prompt: str,
    options: dict[str, str],
    *,
    default: str,
    timeout_seconds: float = 30.0,
    separator: str = ". ",
    allow_shift_enter_fast_mode: bool = False,
    detail_items: list[str] | None = None,
    wait_indefinitely: bool = False,
    asset_path: str | None = None,
    asset_label: str | None = None,
    editable_text: str | None = None,
    editable_label: str | None = None,
    editable_max_length: int = 100,
) -> str:
    """Read one menu key, falling back predictably when nobody is present."""
    global _last_choice_custom_text
    _last_choice_custom_text = None
    if default not in options:
        raise ValueError("The timed-choice default must be one of the options.")
    if timeout_seconds < 0:
        raise ValueError("The timed-choice timeout cannot be negative.")
    # Manual file checkpoints must never inherit fast/timed defaults.  They
    # advance only after the user explicitly asks for validation.
    fast_defaults = not wait_indefinitely and _bool_env(FAST_DEFAULTS_ENV, False)
    windows_console = None
    if not fast_defaults and sys.stdin.isatty() and os.name == "nt":
        import msvcrt

        windows_console = msvcrt
        # Single-key menus intentionally do not require Enter. Clear anything
        # left by an earlier `key + Enter` response before showing a new menu;
        # otherwise that stale Enter would silently select this menu's default.
        while windows_console.kbhit():
            windows_console.getwch()
    color = _console_color_enabled()
    print()
    print(_paint(prompt, ANSI_BOLD + ANSI_CYAN, enabled=color))
    for item in detail_items or []:
        print(f"  - {item}")
    for key, label in options.items():
        suffix = " [default]" if key == default else ""
        key_text = _paint(key, ANSI_GREEN, enabled=color)
        default_text = _paint(suffix, ANSI_YELLOW, enabled=color)
        print(f"  {key_text}{separator}{label}{default_text}")
    if allow_shift_enter_fast_mode:
        print(
            "  Shift+Enter - choose the default now and automatically accept "
            "all later timed defaults"
        )
    if fast_defaults:
        print(
            f"Fast mode: selecting default {default} immediately — "
            f"{options[default]}"
        )
        return default
    if wait_indefinitely:
        print(
            "Waiting without a timeout. Press Enter for the default, or a choice key: ",
            end="",
            flush=True,
        )
    else:
        print(
            f"Press Enter for the default, or a choice key within "
            f"{timeout_seconds:g} seconds; otherwise {default} is selected "
            "automatically: ",
            end="",
            flush=True,
        )

    selected: str | None = None
    deadline = None if wait_indefinitely else time.monotonic() + timeout_seconds
    if windows_console is not None:
        while deadline is None or time.monotonic() < deadline:
            if windows_console.kbhit():
                key = windows_console.getwch()
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in {"\r", "\n"}:
                    if allow_shift_enter_fast_mode and _windows_shift_pressed():
                        os.environ[FAST_DEFAULTS_ENV] = "1"
                    selected = default
                    break
                if key in options:
                    selected = key
                    break
                if key.casefold() in options:
                    selected = key.casefold()
                    break
            time.sleep(0.05)
    elif sys.stdin.isatty():
        import select

        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([sys.stdin], [], [], remaining)
        if readable:
            candidate = sys.stdin.readline().strip()
            if not candidate:
                selected = default
            elif candidate in options:
                selected = candidate
            elif candidate.casefold() in options:
                selected = candidate.casefold()
    else:
        if wait_indefinitely:
            candidate = sys.stdin.readline().strip()
            if not candidate:
                selected = default
            elif candidate in options:
                selected = candidate
            elif candidate.casefold() in options:
                selected = candidate.casefold()
        else:
            time.sleep(timeout_seconds)

    selected = selected or default
    print(selected)
    if allow_shift_enter_fast_mode and _bool_env(FAST_DEFAULTS_ENV, False):
        print("Fast mode enabled: all later timed menus will use their defaults immediately.")
    print(_paint(f"Selected: {options[selected]}", ANSI_GREEN, enabled=color))
    return selected


def _prompt_custom_youtube_title(current_title: str) -> str:
    """Prompt the console user for a custom YouTube title within 100 chars."""
    from publish_stage import YOUTUBE_TITLE_LIMIT, validate_custom_youtube_title

    if sys.stdin.isatty() and os.name == "nt":
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()
    while True:
        try:
            raw = input(
                f"\nEnter the new YouTube title (1-{YOUTUBE_TITLE_LIMIT} characters, "
                "or press Enter to keep current): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return current_title
        if not raw:
            return current_title
        try:
            return validate_custom_youtube_title(raw)
        except ValueError as exc:
            print(f"Invalid YouTube title: {exc}")
            if not sys.stdin.isatty():
                return current_title


def review_youtube_title(
    video_number: int,
    *,
    choose: Any | None = None,
    timeout_seconds: float = 120.0,
) -> dict:
    """Let the user view and change the YouTube title before upload."""
    from publish_stage import (
        YOUTUBE_TITLE_LIMIT,
        update_publish_title,
        validate_custom_youtube_title,
    )

    choose_fn = timed_choice if choose is None else choose
    video_dir = project_dir(video_number)
    publish_dir = video_dir / "09_publish"
    title_path = publish_dir / "title.txt"
    package = _read_json(publish_dir / "publish_package.json")
    manifest = _read_json(publish_dir / "youtube_upload_manifest.json")

    raw_current_title = ""
    if title_path.is_file():
        raw_current_title = title_path.read_text(encoding="utf-8").strip()
    if not raw_current_title:
        raw_current_title = str(
            manifest.get("metadata", {}).get("snippet", {}).get("title", "")
        ).strip()
    if not raw_current_title:
        raw_current_title = str(package.get("recommended_title", "")).strip()
    if not raw_current_title:
        return {"status": "skipped", "reason": "no_publish_package"}

    current_title = validate_custom_youtube_title(raw_current_title)
    options: dict[str, str] = {"1": f"Keep current title: {current_title}"}
    candidate_map: dict[str, str] = {"1": current_title}
    seen = {current_title.casefold()}
    next_index = 2
    candidates = package.get("title_candidates", [])
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            raw_candidate = str(item.get("title", "")).strip()
            if not raw_candidate:
                continue
            try:
                normalized_candidate = validate_custom_youtube_title(raw_candidate)
            except ValueError:
                continue
            if normalized_candidate.casefold() in seen:
                continue
            seen.add(normalized_candidate.casefold())
            key = str(next_index)
            options[key] = f"Use candidate: {normalized_candidate}"
            candidate_map[key] = normalized_candidate
            next_index += 1
            if next_index > 9:
                break
    options["c"] = "Enter a custom YouTube title"

    detail_items = [
        f"Current YouTube title ({len(current_title)}/{YOUTUBE_TITLE_LIMIT} chars): {current_title}"
    ]
    scheduled_at = str(manifest.get("scheduled_publish_at", "")).strip()
    if scheduled_at:
        detail_items.append(f"Scheduled publish slot: {scheduled_at}")
    thumbnail_file = str(
        manifest.get("thumbnail_file")
        or package.get("thumbnails", {}).get("final_file")
        or ""
    ).strip()

    selected_key = choose_fn(
        "Review or change the YouTube title before upload.",
        options,
        default="1",
        timeout_seconds=timeout_seconds,
        detail_items=detail_items,
        asset_path=thumbnail_file or None,
        asset_label="Open thumbnail" if thumbnail_file else None,
        editable_text=current_title,
        editable_label="YouTube title",
        editable_max_length=YOUTUBE_TITLE_LIMIT,
    )
    custom_from_bridge = consume_last_choice_custom_text()
    if custom_from_bridge is not None and selected_key in {"1", "c"}:
        chosen_title = validate_custom_youtube_title(custom_from_bridge)
        selection_source = (
            "custom_input"
            if chosen_title != current_title
            else "current_default"
        )
    elif selected_key == "c":
        chosen_title = _prompt_custom_youtube_title(current_title)
        selection_source = (
            "custom_input"
            if chosen_title != current_title
            else "current_default"
        )
    elif selected_key in candidate_map:
        chosen_title = candidate_map[selected_key]
        selection_source = (
            "current_default"
            if selected_key == "1"
            else f"candidate_{selected_key}"
        )
    else:
        chosen_title = current_title
        selection_source = "current_default"

    updated = update_publish_title(video_number, chosen_title)
    _event(
        video_dir,
        {
            "type": "youtube_title_reviewed",
            "previous_title": updated["previous_title"],
            "title": updated["title"],
            "changed": updated["changed"],
            "selection_source": selection_source,
            "source": "interactive",
        },
    )
    color = _console_color_enabled()
    if updated["changed"]:
        print(
            _paint(
                f"YouTube title updated: {updated['title']}",
                ANSI_BOLD + ANSI_GREEN,
                enabled=color,
            )
        )
    else:
        print(
            _paint(
                f"YouTube title confirmed: {updated['title']}",
                ANSI_GREEN,
                enabled=color,
            )
        )
    return updated


def _previous_upload_for_different_render(video_number: int) -> dict[str, str] | None:
    """Return the prior completed YouTube upload when this render has changed."""
    video_dir = project_dir(video_number)
    previous = _read_json(video_dir / "09_publish" / "youtube_upload_result.json")
    manifest = _read_json(video_dir / "09_publish" / "youtube_upload_manifest.json")
    previous_sha = str(previous.get("video_sha256", "")).strip()
    current_sha = str(manifest.get("video_sha256", "")).strip()
    video_id = str(previous.get("video_id", "")).strip()
    if (
        previous.get("status") != "UPLOAD_COMPLETE"
        or not video_id
        or not previous_sha
        or not current_sha
        or previous_sha == current_sha
    ):
        return None
    return {
        "video_id": video_id,
        "watch_url": str(previous.get("watch_url", "")),
        "previous_sha256": previous_sha,
        "current_sha256": current_sha,
    }


def upload_project(video_number: int, *, force_new_upload: bool = False) -> dict:
    video_dir = project_dir(video_number)
    arguments = [
        "youtube_upload.py",
        "--video",
        str(video_number),
        "--action",
        "upload",
        "--confirm-upload",
    ]
    if force_new_upload:
        arguments.append("--force-new-upload")
    _run_subprocess(
        video_dir,
        "youtube_upload",
        arguments,
    )
    result = _read_json(video_dir / "09_publish" / "youtube_upload_result.json")
    if result.get("status") != "UPLOAD_COMPLETE":
        raise RuntimeError("YouTube upload did not produce a complete result file.")
    return result


def run_preflight() -> bool:
    color = _console_color_enabled()
    print(
        _paint(
            "Checking the production environment (no API calls)...",
            ANSI_CYAN,
            enabled=color,
        )
    )
    result = subprocess.run(
        [sys.executable, str(source_file("preflight.py")), "--json"],
        cwd=AUTOMATION_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    checks = payload.get("checks", []) if isinstance(payload, dict) else []
    failures = [item for item in checks if item.get("status") == "FAIL"]
    warnings = [item for item in checks if item.get("status") == "WARN"]
    if result.returncode != 0 or failures:
        print(_paint("PREFLIGHT FAILED", ANSI_BOLD + ANSI_RED, enabled=color))
        if failures:
            for item in failures:
                print(f"  - {item.get('name')}: {item.get('detail')}")
        else:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "Unknown preflight error."
            )
            print(detail)
        print("No project or provider request was started.")
        return False
    print(
        _paint(
            f"PREFLIGHT PASSED  |  {len(checks)} checks, {len(warnings)} warning(s)",
            ANSI_BOLD + ANSI_GREEN,
            enabled=color,
        )
    )
    for item in warnings:
        print(
            _paint(
                f"  Warning: {item.get('name')} - {item.get('detail')}",
                ANSI_YELLOW,
                enabled=color,
            )
        )
    return True


def print_api_cost_plan(plan: dict) -> None:
    color = _console_color_enabled()
    print(
        _paint(
            "Provider request ceiling for this run",
            ANSI_BOLD + ANSI_CYAN,
            enabled=color,
        )
    )
    for key, label in API_COST_LABELS.items():
        if key in plan:
            print(f"  {label:22} {plan[key]}")


def resume_section_choices(status: dict) -> tuple[dict[str, str], dict[str, str], str]:
    """Build a safe resume menu up to the first unfinished section."""
    next_stage = status.get("next_stage")
    last_index = (
        FLOW_STAGES.index(next_stage)
        if next_stage in FLOW_STAGES
        else len(FLOW_STAGES) - 1
    )
    options: dict[str, str] = {}
    targets: dict[str, str] = {}
    for index, stage in enumerate(FLOW_STAGES[: last_index + 1]):
        key = RESUME_STAGE_KEYS[index]
        state = status["stages"][stage]["status"]
        note = "recommended next" if stage == next_stage else "rebuild from here"
        options[key] = (
            f"Stage {index + 1:02d}/{len(FLOW_STAGES):02d} - "
            f"{STAGE_LABELS[stage]} [{state}; {note}]"
        )
        targets[key] = stage

    if next_stage is None:
        options["u"] = "Upload/publish the completed package to YouTube [recommended]"
        targets["u"] = "upload"
        default = "u"
    else:
        default = RESUME_STAGE_KEYS[last_index]
    return options, targets, default


def interactive() -> int:
    clear_console()
    print_header("NEWS STUDIO", "Autonomous technology-news production")
    if not run_preflight():
        return 1
    latest = latest_project_number()
    decision_timeout = max(
        120.0,
        float(os.getenv("AUTOMATION_DECISION_TIMEOUT_SECONDS", "120")),
    )
    upload_timeout = max(
        120.0,
        float(os.getenv("AUTOMATION_UPLOAD_TIMEOUT_SECONDS", "120")),
    )
    direct_resume_video_raw = os.getenv(
        "NEWS_STUDIO_GUI_DIRECT_RESUME_VIDEO", ""
    ).strip()
    direct_resume_stage_raw = os.getenv(
        "NEWS_STUDIO_GUI_DIRECT_RESUME_STAGE", ""
    ).strip()
    direct_resume_video = (
        int(direct_resume_video_raw)
        if direct_resume_video_raw.isdigit() and int(direct_resume_video_raw) >= 1
        else None
    )
    direct_resume_stage = (
        direct_resume_stage_raw
        if direct_resume_stage_raw in (*FLOW_STAGES, "upload")
        else None
    )

    if direct_resume_video is not None:
        choice = "2"
    else:
        start_options = {"1": "Create a new Tech News video"}
        if latest is not None:
            start_options["2"] = f"Continue from {project_name(latest)}"
        choice = timed_choice(
            "Choose the production action.",
            start_options,
            default="1",
            timeout_seconds=decision_timeout,
            allow_shift_enter_fast_mode=True,
        )
    if choice == "1":
        video_number = next_project_number()
        category = "tech"
        resume_start_stage = None
        upload_only = False
        restore_checkpoint_modes = False
        planned_provider_stages = list(FLOW_STAGES)
    else:
        video_number = (
            direct_resume_video if direct_resume_video is not None else latest
        )
        category = _read_json(project_dir(video_number) / "project_info.json").get(
            "category", "tech"
        )
        status = collect_project_status(video_number)
        section_options, section_targets, section_default = resume_section_choices(status)
        clear_console()
        print_header("NEWS STUDIO", f"Continue from: {project_name(video_number)}")
        if status.get("next_stage"):
            detected = status["next_stage"]
            print(
                f"Detected next section: Stage {FLOW_STAGES.index(detected) + 1:02d}/"
                f"{len(FLOW_STAGES):02d} - {STAGE_LABELS[detected]}"
            )
        else:
            print("Production package is complete; YouTube upload is ready.")
        if direct_resume_stage is not None:
            section_target = direct_resume_stage
        else:
            section_choice = timed_choice(
                "Choose the section to continue from.",
                section_options,
                default=section_default,
                timeout_seconds=decision_timeout,
            )
            section_target = section_targets[section_choice]
        upload_only = section_target == "upload"
        # Choosing the detected next stage is a normal resume: re-check its
        # files and reuse every valid artifact.  Only choosing an earlier,
        # already completed stage means "rebuild from here".
        natural_resume = section_target == status.get("next_stage")
        restore_checkpoint_modes = natural_resume or direct_resume_stage is not None
        resume_start_stage = (
            None if upload_only or natural_resume else section_target
        )
        if upload_only:
            planned_provider_stages = []
        elif natural_resume:
            planned_provider_stages = [
                stage
                for stage in FLOW_STAGES
                if status["stages"][stage]["status"] != TERMINAL_COMPLETE
            ]
        else:
            planned_provider_stages = list(
                FLOW_STAGES[FLOW_STAGES.index(section_target) :]
            )
            if (
                "tts" in planned_provider_stages
                and _tts_complete(project_dir(video_number))
            ):
                planned_provider_stages.remove("tts")

    provider_plan = api_cost_plan(video_number, planned_provider_stages)
    if provider_plan:
        clear_console()
        print_header("NEWS STUDIO", f"Selected project: {project_name(video_number)}")
        print_api_cost_plan(provider_plan)
        confirmation = timed_choice(
            "Allow the listed provider requests?",
            {"1": "Continue with provider requests", "0": "Cancel safely"},
            default="1",
            timeout_seconds=decision_timeout,
            detail_items=api_cost_plan_items(provider_plan),
        )
        if confirmation != "1":
            print("Cancelled before any provider request.")
            return 0

    clear_console()
    progress_display = ProgressDisplay(video_number)
    separator_mode: Literal["manual", "skip", "defer"] = "defer"
    ai_video_mode: Literal["manual", "skip", "defer"] = "defer"
    if restore_checkpoint_modes:
        separator_mode, ai_video_mode = _saved_checkpoint_modes(
            project_dir(video_number)
        )
    preview_approved = False
    requested_start_stage = resume_start_stage
    result = {"status": "COMPLETE", "video_number": video_number}
    while not upload_only:
        result = run_flow(
            video_number,
            category=category,
            confirm_api_costs=True,
            approve_current_preview=preview_approved,
            recover_stale_lock=False,
            dry_run=False,
            manual_separator_mode=separator_mode,
            ai_video_mode=ai_video_mode,
            progress=progress_display,
            start_stage=requested_start_stage,
        )
        requested_start_stage = None
        if result.get("stage") == "manual_separators":
            clear_console()
            print_header(
                "NEWS STUDIO - ACTION REQUIRED",
                f"{project_name(video_number)} | Manual separator checkpoint",
            )
            separator_choice = timed_choice(
                "Manual video separator checkpoint.",
                {
                    "1": "Wait for manually created separators",
                    "2": "Skip separators and show images from chapter start",
                },
                default="2",
                timeout_seconds=decision_timeout,
            )
            separator_mode = "manual" if separator_choice == "1" else "skip"
            if separator_mode == "manual":
                result = run_flow(
                    video_number,
                    category=category,
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    recover_stale_lock=False,
                    dry_run=False,
                    manual_separator_mode="manual",
                    progress=progress_display,
                )
                if result.get("stage") == "manual_separators":
                    return 0
            else:
                continue
        if result.get("stage") == "ai_story_videos":
            clear_console()
            print_header(
                "NEWS STUDIO - OPTIONAL AI VIDEOS",
                f"{project_name(video_number)} | One timed clip per story",
            )
            ai_video_choice = timed_choice(
                "Will you add AI-generated videos for the story sections?",
                {
                    "1": "Yes - create English prompts and open the destination folder",
                    "2": "No - keep the generated storyboard images",
                },
                default="2",
                timeout_seconds=decision_timeout,
            )
            ai_video_mode = "manual" if ai_video_choice == "1" else "skip"
            if ai_video_mode == "manual":
                result = run_flow(
                    video_number,
                    category=category,
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    recover_stale_lock=False,
                    dry_run=False,
                    manual_separator_mode=separator_mode,
                    ai_video_mode="manual",
                    progress=progress_display,
                )
                if result.get("stage") == "ai_story_videos":
                    return 0
            else:
                continue
        if result.get("stage") == "preview_approval":
            proof = (
                project_dir(video_number)
                / "07_effects"
                / "milestone6_reference_60s.mp4"
            )
            if sys.stdin.isatty() and hasattr(os, "startfile"):
                os.startfile(proof)  # type: ignore[attr-defined]
            clear_console()
            print_header(
                "NEWS STUDIO - PREVIEW REVIEW",
                f"{project_name(video_number)} | {proof}",
            )
            preview_choice = timed_choice(
                f"Approve the review preview? {proof}",
                {"1": "Approve and render", "0": "Stop before final render"},
                default="1",
                timeout_seconds=decision_timeout,
            )
            if preview_choice != "1":
                print("Stopped before final render; generated work was preserved.")
                return 0
            preview_approved = True
            continue
        break

    if result.get("status") != "COMPLETE":
        print("Production did not complete, so upload was not attempted.")
        return 1
    clear_console()
    print_header(
        "NEWS STUDIO - PRODUCTION COMPLETE",
        f"{project_name(video_number)} is ready for YouTube",
    )
    upload_choice = timed_choice(
        "Upload the video, thumbnail, title, description, and chapters to YouTube?",
        {"1": "Upload to YouTube", "0": "Do not upload"},
        default="1",
        timeout_seconds=upload_timeout,
    )
    force_new_upload = False
    previous_upload = None
    if upload_choice == "1":
        previous_upload = _previous_upload_for_different_render(video_number)
        if previous_upload is not None:
            duplicate_choice = timed_choice(
                "This project already has a completed YouTube upload with different video content. "
                "Creating another video will not replace the existing scheduled or published video. "
                "Do you want to create a separate upload?",
                {
                    "1": "Create a second scheduled YouTube video",
                    "0": "Keep the existing upload and leave this render ready",
                },
                default="0",
                timeout_seconds=upload_timeout,
                detail_items=[
                    f"Existing video: {previous_upload['watch_url'] or previous_upload['video_id']}",
                    f"Previous render SHA-256: {previous_upload['previous_sha256']}",
                    f"Current render SHA-256: {previous_upload['current_sha256']}",
                ],
            )
            force_new_upload = duplicate_choice == "1"
            _event(
                project_dir(video_number),
                {
                    "type": "youtube_second_video_decision",
                    "decision": "create_separate_upload" if force_new_upload else "keep_existing_upload",
                    "existing_video_id": previous_upload["video_id"],
                    "current_video_sha256": previous_upload["current_sha256"],
                    "source": "interactive",
                },
            )
            if not force_new_upload:
                upload_choice = "0"

    _event(
        project_dir(video_number),
        {
            "type": "youtube_upload_decision",
            "decision": "upload" if upload_choice == "1" else "do_not_upload",
            "force_new_upload": force_new_upload,
            "source": "interactive",
        },
    )
    if upload_choice == "1":
        review_youtube_title(
            video_number,
            choose=timed_choice,
            timeout_seconds=upload_timeout,
        )
        uploaded = (
            upload_project(video_number, force_new_upload=True)
            if force_new_upload
            else upload_project(video_number)
        )
        print(
            _paint(
                f"Upload complete: {uploaded.get('watch_url', uploaded.get('status', 'complete'))}",
                ANSI_BOLD + ANSI_GREEN,
                enabled=_console_color_enabled(),
            )
        )
    else:
        if previous_upload is not None:
            print(
                "Upload skipped; the existing YouTube video is unchanged and this "
                "publish package was preserved."
            )
        else:
            print("Upload skipped; the complete publish package was preserved.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news-studio")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("new", "resume"):
        sub = subparsers.add_parser(command)
        if command == "new":
            sub.add_argument("--category", choices=("tech", "gaming"), default="tech")
        else:
            sub.add_argument("video")
            sub.add_argument("--from-stage", choices=FLOW_STAGES)
        sub.add_argument("--confirm-api-costs", action="store_true")
        sub.add_argument("--approve-preview", action="store_true")
        sub.add_argument("--skip-separators", action="store_true")
        sub.add_argument("--use-ai-videos", action="store_true")
        sub.add_argument("--recover-stale-lock", action="store_true")
        sub.add_argument("--dry-run", action="store_true")
        sub.add_argument("--json", action="store_true")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("video")
    status_parser.add_argument("--json", action="store_true")

    stage_parser = subparsers.add_parser("run-stage")
    stage_parser.add_argument("video")
    stage_parser.add_argument("stage", choices=FLOW_STAGES)
    stage_parser.add_argument("--category", choices=("tech", "gaming"), default="tech")
    stage_parser.add_argument("--confirm-api-costs", action="store_true")
    stage_parser.add_argument("--approve-preview", action="store_true")
    stage_parser.add_argument("--skip-separators", action="store_true")
    stage_parser.add_argument("--use-ai-videos", action="store_true")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("video")
    validate_parser.add_argument("--json", action="store_true")

    preview_parser = subparsers.add_parser("preview")
    preview_parser.add_argument("video")

    approve_parser = subparsers.add_parser("approve-preview")
    approve_parser.add_argument("video")

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("video")

    subparsers.add_parser("interactive")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "interactive":
            return interactive()
        if args.command == "new":
            number = next_project_number()
            result = run_flow(
                number,
                category=args.category,
                confirm_api_costs=args.confirm_api_costs,
                approve_current_preview=args.approve_preview,
                recover_stale_lock=args.recover_stale_lock,
                dry_run=args.dry_run,
                manual_separator_mode="skip" if args.skip_separators else "manual",
                ai_video_mode=(
                    "manual" if getattr(args, "use_ai_videos", False) else "skip"
                ),
                progress=(
                    None
                    if args.json or args.dry_run
                    else ProgressDisplay(number)
                ),
            )
        else:
            number = parse_video_number(args.video)
            if args.command == "resume":
                category = _read_json(project_dir(number) / "project_info.json").get(
                    "category", "tech"
                )
                result = run_flow(
                    number,
                    category=category,
                    confirm_api_costs=args.confirm_api_costs,
                    approve_current_preview=args.approve_preview,
                    recover_stale_lock=args.recover_stale_lock,
                    dry_run=args.dry_run,
                    manual_separator_mode="skip" if args.skip_separators else "manual",
                    ai_video_mode=(
                        "manual" if getattr(args, "use_ai_videos", False) else "skip"
                    ),
                    progress=(
                        None
                        if args.json or args.dry_run
                        else ProgressDisplay(number)
                    ),
                    start_stage=getattr(args, "from_stage", None),
                )
            elif args.command == "status":
                result = collect_project_status(number)
                if not args.json:
                    _print_stage_table(result)
                    return 0
            elif args.command == "run-stage":
                status = collect_project_status(number)
                if status["stages"][args.stage]["status"] == TERMINAL_COMPLETE:
                    result = {
                        "status": "CACHED",
                        "video_number": number,
                        "stage": args.stage,
                    }
                else:
                    with ProjectLock(project_dir(number)):
                        stage_result = execute_stage(
                            number,
                            args.stage,
                            category=args.category,
                            confirm_api_costs=args.confirm_api_costs,
                            approve_current_preview=args.approve_preview,
                            manual_separator_mode=(
                                "skip" if args.skip_separators else "manual"
                            ),
                            ai_video_mode=(
                                "manual"
                                if getattr(args, "use_ai_videos", False)
                                else "skip"
                            ),
                        )
                    result = stage_result.__dict__
            elif args.command == "validate":
                result = validate_project(number)
            elif args.command == "preview":
                with ProjectLock(project_dir(number)):
                    result = execute_stage(
                        number,
                        "preview_approval",
                        category="tech",
                        confirm_api_costs=False,
                        approve_current_preview=False,
                    ).__dict__
            elif args.command == "approve-preview":
                result = approve_preview(number)
            elif args.command == "render":
                with ProjectLock(project_dir(number)):
                    result = execute_stage(
                        number,
                        "render",
                        category="tech",
                        confirm_api_costs=False,
                        approve_current_preview=False,
                    ).__dict__
            else:
                raise ValueError(f"Unsupported command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") not in {"INVALID", TERMINAL_FAILED} else 1
    except KeyboardInterrupt:
        print("\nInterrupted safely. Completed artifacts were preserved.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(
            _paint(
                f"[NEWS STUDIO ERROR] {exc}",
                ANSI_BOLD + ANSI_RED,
                enabled=_console_color_enabled(),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
