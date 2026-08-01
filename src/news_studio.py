from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
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
    source_file,
)
from file_utils import atomic_write_json, sha256_file
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
    "archive_visuals": "Selecting contextual archive images",
    "captions": "Creating timed captions",
    "effects": "Planning frames, effects, and music",
    "preview_approval": "Preparing and approving the preview",
    "render": "Encoding the final video",
    "publish": "Creating thumbnail and YouTube metadata",
}


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


def _file_matches(path_value: Any, expected_sha256: Any = None) -> bool:
    path = Path(str(path_value or ""))
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
    for item in files:
        path = Path(str(item.get("audio_file", "")))
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
    return all(
        _file_matches(item.get("text"), item.get("narration_sha256"))
        and _file_matches(item.get("original"), item.get("source_sha256"))
        for item in stories
    )


def _alignment_complete(video_dir: Path) -> bool:
    project = _read_json(video_dir / "project_info.json")
    timing = _read_json(video_dir / "story_timing.json")
    stories = timing.get("stories", [])
    if not project.get("stories") or len(stories) != len(project["stories"]):
        return False
    tts = _read_json(video_dir / "Sesler" / "tts_info.json")
    return all(
        _file_matches(item.get("alignment_file"), item.get("alignment_sha256"))
        for item in tts.get("files", [])
    )


def _storyboard_complete(video_dir: Path) -> bool:
    manifest = _read_json(video_dir / "04_storyboard" / "storyboard.json")
    quality = manifest.get("quality", {})
    if quality.get("status") != "passed" or not quality.get(
        "all_images_generated_and_validated"
    ):
        return False
    images = [
        image
        for story in manifest.get("stories", [])
        for image in story.get("images", [])
    ]
    return bool(images) and all(
        image.get("validation", {}).get("status") == "passed"
        and _file_matches(
            image.get("output_file"), image.get("generation", {}).get("file_sha256")
        )
        for image in images
    )


def _archive_visuals_complete(video_dir: Path) -> bool:
    manifest = _read_json(video_dir / "05_visuals" / "media_plan.json")
    if manifest.get("quality", {}).get("status") != "passed":
        return False
    assets = [
        window.get("selected_asset", {})
        for story in manifest.get("stories", [])
        for window in story.get("windows", [])
    ]
    return bool(assets) and all(
        _file_matches(asset.get("absolute_path"), asset.get("sha256"))
        for asset in assets
    )


def _captions_complete(video_dir: Path, state: dict) -> bool:
    manifest = _read_json(video_dir / "06_captions" / "captions.json")
    if (
        state.get("captions", {}).get("status") != "CAPTIONS_READY"
        or manifest.get("quality", {}).get("status") != "passed"
    ):
        return False
    if not _file_matches(
        manifest.get("combined_srt_file"), manifest.get("combined_srt_sha256")
    ):
        return False
    return all(
        _file_matches(item.get("srt_file"), item.get("srt_sha256"))
        and _file_matches(item.get("text_file"), item.get("text_sha256"))
        and _file_matches(item.get("alignment_file"), item.get("alignment_sha256"))
        for item in manifest.get("stories", [])
    )


def _effects_complete(video_dir: Path, state: dict) -> bool:
    manifest = _read_json(video_dir / "07_effects" / "effects_plan.json")
    if (
        state.get("effects", {}).get("status") != "EFFECTS_READY"
        or manifest.get("quality", {}).get("status") != "passed"
    ):
        return False
    assets = [item.get("asset", {}) for item in manifest.get("visual_events", [])]
    return bool(assets) and all(
        _file_matches(asset.get("file"), asset.get("sha256")) for asset in assets
    )


def _render_complete(video_dir: Path, state: dict) -> bool:
    report = _read_json(video_dir / "08_render" / "render_report.json")
    if (
        state.get("render", {}).get("status") != "RENDER_READY"
        or report.get("quality", {}).get("status") != "passed"
    ):
        return False
    return all(
        _file_matches(item.get("file"), item.get("qa", {}).get("sha256"))
        and item.get("qa", {}).get("status") == "passed"
        for item in (report.get("final", {}), report.get("preview", {}))
    )


def _publish_complete(video_dir: Path, state: dict) -> bool:
    if state.get("publish", {}).get("status") != "PUBLISH_READY":
        return False
    package = video_dir / "09_publish" / "publish_package.json"
    manifest = _read_json(video_dir / "09_publish" / "youtube_upload_manifest.json")
    return (
        package.is_file()
        and manifest.get("chapters_embedded_in_description") is True
        and _file_matches(manifest.get("video_file"), manifest.get("video_sha256"))
        and _file_matches(
            manifest.get("thumbnail_file"), manifest.get("thumbnail_sha256")
        )
    )


def _preview_status(video_dir: Path) -> tuple[str, str]:
    proof = video_dir / "07_effects" / "milestone6_reference_60s.mp4"
    approval_path = video_dir / "orchestration" / "preview_approval.json"
    if not proof.is_file():
        return "NOT_STARTED", "The 60-second review preview has not been rendered."
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
    if (
        checks["render"]["status"] == TERMINAL_COMPLETE
        and checks["publish"]["status"] == TERMINAL_COMPLETE
    ):
        for stage in FLOW_STAGES[:-3]:
            if checks[stage]["status"] == TERMINAL_COMPLETE:
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
        "locked": bool(lock),
        "lock": lock or None,
        "news_archive": archive_status,
    }


def api_cost_plan(video_number: int | None = None) -> dict:
    top_n = int(os.getenv("TOP_N", "5"))
    missing_images = top_n * 2
    if video_number is not None:
        storyboard = _read_json(
            project_dir(video_number) / "04_storyboard" / "storyboard.json"
        )
        if storyboard:
            missing_images = max(
                0,
                int(storyboard.get("planned_image_count", top_n * 2))
                - int(storyboard.get("generated_image_count", 0)),
            )
    return {
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
        "publishing_copy_requests": 1,
        "thumbnail_image_requests": 1,
        "live_test_requests": 0,
    }


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


def _run_subprocess(
    video_dir: Path,
    stage: str,
    arguments: list[str],
    *,
    progress: ProgressDisplay | None = None,
) -> None:
    directory = video_dir / "orchestration" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = directory / f"{stamp}_{stage}.log"
    command = [sys.executable, str(source_file(arguments[0])), *arguments[1:]]
    _event(video_dir, {"type": "stage_started", "stage": stage, "command": command})
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=AUTOMATION_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                if progress is not None:
                    progress.write_subprocess_line(line)
                else:
                    print(line, end="")
                log.write(line)
                log.flush()
            return_code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            process.wait(timeout=10)
            _event(video_dir, {"type": "stage_interrupted", "stage": stage})
            raise
    elapsed = round(time.monotonic() - started, 3)
    if return_code != 0:
        _event(
            video_dir,
            {
                "type": "stage_failed",
                "stage": stage,
                "exit_code": return_code,
                "elapsed_seconds": elapsed,
                "log_file": str(log_path),
            },
        )
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


def execute_stage(
    video_number: int,
    stage: str,
    *,
    category: str,
    confirm_api_costs: bool,
    approve_current_preview: bool,
    manual_separator_mode: Literal["manual", "skip", "defer"] = "manual",
    progress: ProgressDisplay | None = None,
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
        _run_subprocess(
            video_dir,
            stage,
            ["news_pipeline.py", "--video", str(video_number), "--category", category],
            progress=progress,
        )
    elif stage == "tts":
        _run_subprocess(
            video_dir,
            stage,
            ["elevenlabs_tts.py", "--video", str(video_number), "--compact"],
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
        if result.returncode != 0:
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
            return StageResult(
                TERMINAL_WAITING,
                stage,
                f"Add the separator files to {video_dir / '03_manual_separators'}, then resume.",
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
        if not proof.is_file():
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
        _run_subprocess(
            video_dir,
            stage,
            ["render_stage.py", "--video", str(video_number), "--action", "render"],
            progress=progress,
        )
    elif stage == "publish":
        _run_subprocess(
            video_dir,
            stage,
            [
                "publish_stage.py",
                "--video",
                str(video_number),
                "--generate-thumbnail",
                "--confirm-ai-cost",
                "--confirm-thumbnail-cost",
            ],
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
    progress: ProgressDisplay | None = None,
) -> dict:
    before = collect_project_status(video_number)
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
        _event(video_dir, {"type": "flow_started", "planned_stages": planned})
        if not planned and progress is not None:
            progress.complete_stage(len(FLOW_STAGES))
        for stage in FLOW_STAGES:
            current = collect_project_status(video_number)
            if current["stages"][stage]["status"] == TERMINAL_COMPLETE:
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
                    progress=progress,
                )
            except Exception:
                if progress is not None:
                    progress.failed(completed_before)
                raise
            if result.status == TERMINAL_WAITING:
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
            if progress is not None:
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


def timed_choice(
    prompt: str,
    options: dict[str, str],
    *,
    default: str,
    timeout_seconds: float = 15.0,
) -> str:
    """Read one menu key, falling back predictably when nobody is present."""
    if default not in options:
        raise ValueError("The timed-choice default must be one of the options.")
    if timeout_seconds < 0:
        raise ValueError("The timed-choice timeout cannot be negative.")
    color = _console_color_enabled()
    print()
    print(_paint(prompt, ANSI_BOLD + ANSI_CYAN, enabled=color))
    for key, label in options.items():
        suffix = " [default]" if key == default else ""
        key_text = _paint(key, ANSI_GREEN, enabled=color)
        default_text = _paint(suffix, ANSI_YELLOW, enabled=color)
        print(f"  {key_text}. {label}{default_text}")
    print(
        f"Press a choice key within {timeout_seconds:g} seconds; "
        f"otherwise {default} is selected automatically: ",
        end="",
        flush=True,
    )

    selected: str | None = None
    deadline = time.monotonic() + timeout_seconds
    if sys.stdin.isatty() and os.name == "nt":
        import msvcrt

        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in options:
                    selected = key
                    break
            time.sleep(0.05)
    elif sys.stdin.isatty():
        import select

        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([sys.stdin], [], [], remaining)
        if readable:
            candidate = sys.stdin.readline().strip()
            if candidate in options:
                selected = candidate
    else:
        time.sleep(timeout_seconds)

    selected = selected or default
    print(selected)
    print(_paint(f"Selected: {options[selected]}", ANSI_GREEN, enabled=color))
    return selected


def upload_project(video_number: int) -> dict:
    video_dir = project_dir(video_number)
    _run_subprocess(
        video_dir,
        "youtube_upload",
        [
            "youtube_upload.py",
            "--video",
            str(video_number),
            "--action",
            "upload",
            "--confirm-upload",
        ],
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
    labels = {
        "news_text_requests": "News summaries",
        "news_duplicate_comparison_requests": "Duplicate checks",
        "elevenlabs_tts_stories": "Voiceovers",
        "forced_alignment_requests": "Alignment",
        "storyboard_planning_requests": "Storyboard plan",
        "story_image_requests_remaining": "AI images remaining",
        "archive_selection_requests": "Archive selection",
        "publishing_copy_requests": "YouTube copy",
        "thumbnail_image_requests": "Thumbnail",
    }
    for key, label in labels.items():
        if key in plan:
            print(f"  {label:22} {plan[key]}")


def interactive() -> int:
    clear_console()
    print_header("NEWS STUDIO", "Autonomous technology-news production")
    if not run_preflight():
        return 1
    latest = latest_project_number()
    decision_timeout = float(os.getenv("AUTOMATION_DECISION_TIMEOUT_SECONDS", "15"))
    upload_timeout = float(os.getenv("AUTOMATION_UPLOAD_TIMEOUT_SECONDS", "30"))
    start_options = {"1": "Create a new Tech News video"}
    if latest is not None:
        start_options["2"] = f"Resume {project_name(latest)}"
    choice = timed_choice(
        "Choose the production action.",
        start_options,
        default="1",
        timeout_seconds=decision_timeout,
    )
    if choice == "1":
        video_number = next_project_number()
        category = "tech"
    else:
        video_number = latest
        category = _read_json(project_dir(video_number) / "project_info.json").get(
            "category", "tech"
        )

    clear_console()
    print_header("NEWS STUDIO", f"Selected project: {project_name(video_number)}")
    print_api_cost_plan(api_cost_plan(video_number))
    confirmation = timed_choice(
        "Allow the listed provider requests?",
        {"1": "Continue with provider requests", "0": "Cancel safely"},
        default="1",
        timeout_seconds=decision_timeout,
    )
    if confirmation != "1":
        print("Cancelled before any provider request.")
        return 0

    clear_console()
    progress_display = ProgressDisplay(video_number)
    separator_mode: Literal["manual", "skip", "defer"] = "defer"
    preview_approved = False
    while True:
        result = run_flow(
            video_number,
            category=category,
            confirm_api_costs=True,
            approve_current_preview=preview_approved,
            recover_stale_lock=False,
            dry_run=False,
            manual_separator_mode=separator_mode,
            progress=progress_display,
        )
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
    if upload_choice == "1":
        uploaded = upload_project(video_number)
        print(
            _paint(
                f"Upload complete: {uploaded.get('video_url', uploaded.get('status', 'complete'))}",
                ANSI_BOLD + ANSI_GREEN,
                enabled=_console_color_enabled(),
            )
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
        sub.add_argument("--confirm-api-costs", action="store_true")
        sub.add_argument("--approve-preview", action="store_true")
        sub.add_argument("--skip-separators", action="store_true")
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
