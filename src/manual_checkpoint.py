from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_utils import (  # noqa: E402
    atomic_write_json,
    json_fingerprint,
    report_cli_error,
    sha256_file,
)
from file_locations import project_dir, project_name  # noqa: E402
from tooling import find_executable  # noqa: E402


STAGE_NAME = "manual_separators"
STATE_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    )


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


@dataclass(frozen=True)
class SeparatorSpec:
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    fps_tolerance: float = 0.05
    allowed_codecs: tuple[str, ...] = ("h264", "hevc")
    allowed_pixel_formats: tuple[str, ...] = (
        "yuv420p",
        "yuv420p10le",
    )
    audio_policy: str = "consistent"
    minimum_duration_seconds: float = 0.1

    @classmethod
    def from_environment(cls) -> "SeparatorSpec":
        return cls(
            width=int(os.getenv("SEPARATOR_WIDTH", "1920")),
            height=int(os.getenv("SEPARATOR_HEIGHT", "1080")),
            fps=float(os.getenv("SEPARATOR_FPS", "30")),
            fps_tolerance=float(
                os.getenv("SEPARATOR_FPS_TOLERANCE", "0.05")
            ),
            allowed_codecs=env_list(
                "SEPARATOR_ALLOWED_CODECS",
                "h264,hevc",
            ),
            allowed_pixel_formats=env_list(
                "SEPARATOR_ALLOWED_PIXEL_FORMATS",
                "yuv420p,yuv420p10le",
            ),
            audio_policy=os.getenv(
                "SEPARATOR_AUDIO_POLICY",
                "consistent",
            ).strip().lower(),
            minimum_duration_seconds=float(
                os.getenv("SEPARATOR_MIN_DURATION_SECONDS", "0.1")
            ),
        )

    def validate(self) -> None:
        if self.width < 16 or self.height < 16:
            raise ValueError("Separator resolution must be at least 16×16.")
        if self.fps <= 0 or self.fps_tolerance < 0:
            raise ValueError("Separator FPS values must be positive.")
        if not self.allowed_codecs:
            raise ValueError("At least one separator codec must be allowed.")
        if not self.allowed_pixel_formats:
            raise ValueError("At least one separator pixel format must be allowed.")
        if self.audio_policy not in {
            "consistent",
            "require",
            "forbid",
            "allow_mixed",
        }:
            raise ValueError(
                "SEPARATOR_AUDIO_POLICY must be consistent, require, "
                "forbid, or allow_mixed."
            )
        if self.minimum_duration_seconds <= 0:
            raise ValueError("Separator minimum duration must be positive.")


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


def expected_separator_paths(video_dir: Path, count: int) -> list[Path]:
    if count < 1:
        raise ValueError("At least one separator is required.")
    folder = video_dir / "03_manual_separators"
    return [folder / f"{index:02d}_separator.mp4" for index in range(1, count + 1)]


def _file_identity(path: Path) -> dict:
    return {
        "path": str(path),
        "sha256": sha256_file(path) if path.is_file() else "missing",
    }


def checkpoint_input_fingerprint(
    timing: dict,
    spec: SeparatorSpec,
) -> str:
    stories = []
    for story in timing.get("stories", []):
        audio_path = Path(story["audio_file"])
        alignment_path = Path(story["alignment_file"])
        stories.append(
            {
                "order": story["order"],
                "story_id": story["story_id"],
                "audio": _file_identity(audio_path),
                "alignment": _file_identity(alignment_path),
            }
        )
    return json_fingerprint(
        {
            "required_separator_count": len(stories),
            "stories": stories,
            "spec": asdict(spec),
        }
    )


def _load_state(path: Path, video_number: int) -> dict:
    if path.exists():
        state = load_json(path, "Stage state")
    else:
        state = {}
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("video_number", video_number)
    state.setdefault("video_name", project_name(video_number))
    state.setdefault("stages", {})
    return state


def _write_stage(
    state_path: Path,
    state: dict,
    stage: dict,
) -> None:
    stage["updated_at_utc"] = utc_now()
    state["updated_at_utc"] = stage["updated_at_utc"]
    state["stages"][STAGE_NAME] = stage
    atomic_write_json(state_path, state)


def ready_files_are_current(stage: dict) -> bool:
    validated = stage.get("validation", {}).get("files", [])
    if not validated:
        return False
    for item in validated:
        path = Path(item.get("path", ""))
        expected = item.get("sha256", "")
        if not path.is_file() or not expected:
            return False
        if sha256_file(path) != expected:
            return False
    return True


def prepare_checkpoint(
    video_number: int,
    spec: SeparatorSpec | None = None,
) -> tuple[Path, dict, dict, dict]:
    spec = spec or SeparatorSpec.from_environment()
    spec.validate()
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")

    timing_path = video_dir / "story_timing.json"
    timing = load_json(timing_path, "Story timing manifest")
    if timing.get("quality", {}).get("status") != "passed":
        raise RuntimeError("Story timing quality gate has not passed.")

    required_count = len(timing.get("stories", []))
    paths = expected_separator_paths(video_dir, required_count)
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    fingerprint = checkpoint_input_fingerprint(timing, spec)

    state_path = video_dir / "state.json"
    state = _load_state(state_path, video_number)
    existing = state["stages"].get(STAGE_NAME, {})
    same_inputs = existing.get("input_fingerprint") == fingerprint
    preserve_status = same_inputs and existing.get("status") in {
        "WAITING",
        "CANCELLED",
        "READY",
        "SKIPPED",
    }
    if preserve_status:
        stage = dict(existing)
        if stage["status"] == "READY" and not ready_files_are_current(stage):
            stage["status"] = "WAITING"
            stage["reason"] = "A validated separator is missing or changed."
            stage.pop("ready_at_utc", None)
    else:
        stage = {
            "created_at_utc": utc_now(),
            "status": "WAITING",
            "reason": "Waiting for manual separator animations.",
        }

    stage.update(
        {
            "input_fingerprint": fingerprint,
            "project_directory": str(video_dir),
            "news_directory": str(video_dir / "Haberler"),
            "separator_directory": str(paths[0].parent),
            "required_count": required_count,
            "expected_files": [str(path) for path in paths],
            "spec": asdict(spec),
            "resume_command": f'news-studio.bat resume "{project_name(video_number)}"',
        }
    )
    if (
        stage["status"] == "READY"
        and not timing.get("quality", {}).get("manual_separators_validated", False)
    ):
        timing = apply_validated_separators(timing, stage["validation"])
        atomic_write_json(timing_path, timing)
    _write_stage(state_path, state, stage)
    return state_path, state, stage, timing


def _open_directory(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    command = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_checkpoint_folders(
    stage: dict,
    *,
    opener=None,
) -> list[Path]:
    """Open source news first, then the folder where separators must be saved."""
    if not env_bool("AUTO_OPEN_SEPARATOR_FOLDERS", True):
        return []
    open_function = opener or _open_directory
    folders = [
        Path(stage["news_directory"]),
        Path(stage["separator_directory"]),
    ]
    opened: list[Path] = []
    for folder in folders:
        if not folder.is_dir():
            print(f"[CHECKPOINT WARNING] Folder does not exist: {folder}")
            continue
        try:
            open_function(folder)
            opened.append(folder)
        except OSError as exc:
            print(f"[CHECKPOINT WARNING] Could not open {folder}: {exc}")
    return opened


def parse_frame_rate(value: str) -> float:
    try:
        fraction = Fraction(value)
        rate = float(fraction)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid frame rate: {value}") from exc
    if rate <= 0:
        raise ValueError(f"Frame rate must be positive: {value}")
    return rate


def probe_separator(path: Path) -> dict:
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        raise RuntimeError("FFprobe is required to validate separator animations.")
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
        raise RuntimeError(f"FFprobe returned invalid JSON for {path.name}.") from exc

    streams = payload.get("streams", [])
    video_streams = [
        item for item in streams if item.get("codec_type") == "video"
    ]
    audio_streams = [
        item for item in streams if item.get("codec_type") == "audio"
    ]
    if not video_streams:
        raise RuntimeError(f"{path.name} contains no video stream.")
    video = video_streams[0]
    duration = float(
        payload.get("format", {}).get("duration")
        or video.get("duration")
        or 0
    )
    frame_rate_text = (
        video.get("avg_frame_rate")
        if video.get("avg_frame_rate") not in {"0/0", "", None}
        else video.get("r_frame_rate", "0/0")
    )
    return {
        "path": str(path),
        "filename": path.name,
        "format_name": payload.get("format", {}).get("format_name", ""),
        "duration_seconds": round(duration, 6),
        "codec": str(video.get("codec_name", "")).lower(),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "pixel_format": str(video.get("pix_fmt", "")).lower(),
        "frame_rate": round(parse_frame_rate(frame_rate_text), 6),
        "frame_rate_source": frame_rate_text,
        "has_audio": bool(audio_streams),
        "audio_codec": (
            str(audio_streams[0].get("codec_name", "")).lower()
            if audio_streams
            else ""
        ),
    }


def decode_separator(path: Path, *, has_audio: bool) -> None:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required to decode separator animations.")
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
    ]
    if has_audio:
        command.extend(["-map", "0:a:0"])
    command.extend(["-sn", "-f", "null", os.devnull])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"FFmpeg could not fully decode {path.name}: "
            f"{completed.stderr.strip() or 'unknown decode error'}"
        )


def validate_separator_file(path: Path, spec: SeparatorSpec) -> dict:
    result: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "status": "failed",
        "errors": [],
        "warnings": [],
    }
    if not path.is_file():
        result["errors"].append("File is missing.")
        return result
    if path.stat().st_size == 0:
        result["errors"].append("File is empty.")
        return result

    try:
        media = probe_separator(path)
        result.update(media)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(str(exc))
        return result

    if media["duration_seconds"] < spec.minimum_duration_seconds:
        result["errors"].append(
            f"Duration {media['duration_seconds']:.3f}s is below "
            f"{spec.minimum_duration_seconds:.3f}s."
        )
    result["normalization_required"] = (
        media["width"], media["height"]
    ) != (spec.width, spec.height)
    if result["normalization_required"]:
        result["warnings"].append(
            f"Resolution is {media['width']}×{media['height']}; it will be "
            f"scaled and padded to {spec.width}×{spec.height} during render."
        )
    if abs(media["frame_rate"] - spec.fps) > spec.fps_tolerance:
        result["errors"].append(
            f"Frame rate is {media['frame_rate']:.3f}; "
            f"expected {spec.fps:.3f}±{spec.fps_tolerance:.3f}."
        )
    if media["codec"] not in spec.allowed_codecs:
        result["errors"].append(
            f"Codec {media['codec'] or 'unknown'} is not allowed "
            f"({', '.join(spec.allowed_codecs)})."
        )
    if media["pixel_format"] not in spec.allowed_pixel_formats:
        result["errors"].append(
            f"Pixel format {media['pixel_format'] or 'unknown'} is not allowed "
            f"({', '.join(spec.allowed_pixel_formats)})."
        )
    if spec.audio_policy == "require" and not media["has_audio"]:
        result["errors"].append("An audio stream is required.")
    if spec.audio_policy == "forbid" and media["has_audio"]:
        result["errors"].append("Audio is forbidden for separator animations.")

    if not result["errors"]:
        try:
            decode_separator(path, has_audio=media["has_audio"])
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(str(exc))

    if not result["errors"]:
        result["status"] = "passed"
        result["sha256"] = sha256_file(path)
        result["bytes"] = path.stat().st_size
    return result


def validate_separator_set(
    paths: list[Path],
    spec: SeparatorSpec,
) -> dict:
    spec.validate()
    files = [validate_separator_file(path, spec) for path in paths]
    global_errors: list[str] = []
    passed_files = [item for item in files if item["status"] == "passed"]

    if spec.audio_policy == "consistent" and len(passed_files) == len(files):
        audio_states = {bool(item["has_audio"]) for item in passed_files}
        if len(audio_states) > 1:
            global_errors.append(
                "Separator audio presence is inconsistent; all files must either "
                "contain audio or all omit it."
            )

    status = (
        "passed"
        if all(item["status"] == "passed" for item in files)
        and not global_errors
        else "failed"
    )
    return {
        "status": status,
        "validated_at_utc": utc_now(),
        "spec": asdict(spec),
        "global_errors": global_errors,
        "files": files,
    }


def _offset_records(records: list[dict], offset: float) -> list[dict]:
    updated = []
    for record in records:
        item = dict(record)
        item["timeline_start"] = round(offset + float(item["start"]), 6)
        item["timeline_end"] = round(offset + float(item["end"]), 6)
        updated.append(item)
    return updated


def apply_validated_separators(timing: dict, validation: dict) -> dict:
    if validation.get("status") != "passed":
        raise ValueError("Cannot apply separator timing from a failed validation.")
    stories = timing.get("stories", [])
    files = validation.get("files", [])
    if len(stories) != len(files):
        raise ValueError("Separator validation count does not match story count.")

    cursor = 0.0
    inter_story_gap = float(timing.get("inter_story_gap_seconds", 1.0))
    if inter_story_gap < 0:
        raise ValueError("INTER_STORY_GAP_SECONDS cannot be negative.")
    updated_stories: list[dict] = []
    for index, (story, media) in enumerate(
        zip(stories, files, strict=True),
        start=1,
    ):
        item = dict(story)
        separator_duration = float(media["duration_seconds"])
        narration_duration = float(story["narration"]["duration"])
        separator_start = cursor
        separator_end = separator_start + separator_duration
        narration_start = cursor
        narration_end = narration_start + narration_duration
        item["separator"] = {
            "start": round(separator_start, 6),
            "end": round(separator_end, 6),
            "duration": round(separator_duration, 6),
            "expected_filename": media["filename"],
            "file": media["path"],
            "sha256": media["sha256"],
            "codec": media["codec"],
            "width": media["width"],
            "height": media["height"],
            "frame_rate": media["frame_rate"],
            "pixel_format": media["pixel_format"],
            "has_audio": media["has_audio"],
            "audio_codec": media["audio_codec"],
        }
        item["narration"] = {
            **story["narration"],
            "start": round(narration_start, 6),
            "end": round(narration_end, 6),
        }
        item["gap_after_seconds"] = round(
            inter_story_gap if index < len(stories) else 0.0,
            6,
        )
        item["sentences"] = _offset_records(
            story.get("sentences", []),
            narration_start,
        )
        item["phrases"] = _offset_records(
            story.get("phrases", []),
            narration_start,
        )
        item["visual_beats"] = _offset_records(
            story.get("visual_beats", []),
            narration_start,
        )
        item["important_claims"] = _offset_records(
            story.get("important_claims", []),
            narration_start,
        )
        updated_stories.append(item)
        cursor = max(narration_end, separator_end)
        if index < len(stories):
            cursor += inter_story_gap

    updated = dict(timing)
    updated["stories"] = updated_stories
    updated["duration_seconds"] = round(cursor, 6)
    updated["manual_separators_validated_at_utc"] = validation[
        "validated_at_utc"
    ]
    updated["separator_duration_seconds"] = None
    updated["inter_story_gap_seconds"] = round(inter_story_gap, 6)
    updated["separator_timing_mode"] = "overlay_at_narration_start"
    updated["manual_separator_mode"] = "manual"
    updated.pop("manual_separators_skipped_at_utc", None)
    audio_states = {bool(item["has_audio"]) for item in files}
    audio_presence = (
        "mixed"
        if len(audio_states) > 1
        else "present"
        if audio_states == {True}
        else "absent"
    )
    updated["quality"] = {
        **timing.get("quality", {}),
        "status": "passed",
        "manual_separators_validated": True,
        "manual_separators_skipped": False,
        "separator_audio_presence": audio_presence,
        "no_story_or_separator_overlaps": True,
        "separator_overlays_narration": True,
        "images_start_at_chapter_start": False,
        "inter_story_gaps_are_exact": True,
    }
    return updated


def apply_skipped_separators(timing: dict) -> dict:
    """Remove separator media and rebuild a narration-first chapter timeline."""
    stories = timing.get("stories", [])
    if not stories:
        raise ValueError("Cannot skip separators for an empty story timeline.")
    inter_story_gap = float(timing.get("inter_story_gap_seconds", 1.0))
    if inter_story_gap < 0:
        raise ValueError("INTER_STORY_GAP_SECONDS cannot be negative.")

    cursor = 0.0
    updated_stories: list[dict] = []
    for index, story in enumerate(stories, start=1):
        item = dict(story)
        narration_duration = float(story["narration"]["duration"])
        narration_start = cursor
        narration_end = narration_start + narration_duration
        expected_filename = story.get("separator", {}).get(
            "expected_filename", f"{index:02d}_separator.mp4"
        )
        item["separator"] = {
            "start": round(narration_start, 6),
            "end": round(narration_start, 6),
            "duration": 0.0,
            "expected_filename": expected_filename,
            "file": "",
            "sha256": "",
            "has_audio": False,
            "skipped": True,
        }
        item["narration"] = {
            **story["narration"],
            "start": round(narration_start, 6),
            "end": round(narration_end, 6),
        }
        item["gap_after_seconds"] = round(
            inter_story_gap if index < len(stories) else 0.0,
            6,
        )
        for name in ("sentences", "phrases", "visual_beats", "important_claims"):
            item[name] = _offset_records(story.get(name, []), narration_start)
        updated_stories.append(item)
        cursor = narration_end
        if index < len(stories):
            cursor += inter_story_gap

    updated = dict(timing)
    updated["stories"] = updated_stories
    updated["duration_seconds"] = round(cursor, 6)
    updated["separator_duration_seconds"] = 0.0
    updated["required_separator_count"] = 0
    updated["inter_story_gap_seconds"] = round(inter_story_gap, 6)
    updated["separator_timing_mode"] = "overlay_at_narration_start"
    updated["manual_separator_mode"] = "skipped"
    updated.pop("manual_separators_validated_at_utc", None)
    updated["manual_separators_skipped_at_utc"] = utc_now()
    updated["quality"] = {
        **timing.get("quality", {}),
        "status": "passed",
        "manual_separators_validated": False,
        "manual_separators_skipped": True,
        "separator_audio_presence": "absent",
        "no_story_or_separator_overlaps": True,
        "separator_overlays_narration": False,
        "images_start_at_chapter_start": True,
        "inter_story_gaps_are_exact": True,
    }
    return updated


def skip_checkpoint(
    video_number: int,
    spec: SeparatorSpec | None = None,
) -> dict:
    """Persist an intentional no-separator mode and continue automatically."""
    state_path, state, stage, timing = prepare_checkpoint(video_number, spec)
    updated_timing = apply_skipped_separators(timing)
    atomic_write_json(project_dir(video_number) / "story_timing.json", updated_timing)
    stage["status"] = "SKIPPED"
    stage["reason"] = (
        "Manual separators were skipped; story images start with narration."
    )
    stage["required_count"] = 0
    stage["skipped_at_utc"] = utc_now()
    stage.pop("ready_at_utc", None)
    stage.pop("validation", None)
    _write_stage(state_path, state, stage)
    return stage


def validate_checkpoint(
    video_number: int,
    spec: SeparatorSpec | None = None,
) -> tuple[bool, dict]:
    spec = spec or SeparatorSpec.from_environment()
    state_path, state, stage, timing = prepare_checkpoint(video_number, spec)
    stage["status"] = "VALIDATING"
    stage["reason"] = "Validating manual separator animations."
    _write_stage(state_path, state, stage)

    paths = [Path(value) for value in stage["expected_files"]]
    validation = validate_separator_set(paths, spec)
    stage["validation"] = validation
    if validation["status"] == "passed":
        updated_timing = apply_validated_separators(timing, validation)
        atomic_write_json(project_dir(video_number) / "story_timing.json", updated_timing)
        stage["status"] = "READY"
        stage["reason"] = "All manual separator animations passed validation."
        stage["ready_at_utc"] = utc_now()
        success = True
    else:
        stage["status"] = "WAITING"
        stage["reason"] = "One or more separator animations failed validation."
        stage.pop("ready_at_utc", None)
        success = False
    _write_stage(state_path, state, stage)
    return success, stage


def cancel_checkpoint(
    video_number: int,
    spec: SeparatorSpec | None = None,
) -> dict:
    state_path, state, stage, _ = prepare_checkpoint(video_number, spec)
    stage["status"] = "CANCELLED"
    stage["reason"] = "Cancelled by the user; generated work was preserved."
    stage["cancelled_at_utc"] = utc_now()
    _write_stage(state_path, state, stage)
    return stage


def print_checkpoint(stage: dict) -> None:
    spec = stage["spec"]
    print()
    print(
        "The summary and the original news texts are saved, waiting for the "
        "animation production. Press Enter after ready, or 0 to cancel."
    )
    print()
    print(f"Project folder   : {stage['project_directory']}")
    print(f"News to review   : {stage['news_directory']}")
    print(f"Separator folder : {stage['separator_directory']}")
    print(f"Required files   : {stage['required_count']}")
    for path in stage["expected_files"]:
        print(f"  - {Path(path).name}")
    print(
        "Output canvas    : "
        f"{spec['width']}x{spec['height']}; other input resolutions are normalized. "
        f"{spec['fps']} fps, "
        f"codec={','.join(spec['allowed_codecs'])}, "
        f"pixel_format={','.join(spec['allowed_pixel_formats'])}"
    )
    print(f"Audio policy     : {spec['audio_policy']}")
    print(f"Resume command   : {stage['resume_command']}")
    print()


def print_validation_failures(stage: dict) -> None:
    validation = stage.get("validation", {})
    for item in validation.get("files", []):
        if item.get("status") == "passed":
            print(f"[PASS] {item['filename']}")
            for warning in item.get("warnings", []):
                print(f"       - {warning}")
        else:
            print(f"[FAIL] {item['filename']}")
            for error in item.get("errors", []):
                print(f"       - {error}")
    for error in validation.get("global_errors", []):
        print(f"[FAIL] Set: {error}")


def prompt_checkpoint(
    video_number: int,
    spec: SeparatorSpec | None = None,
) -> str:
    _, _, stage, _ = prepare_checkpoint(video_number, spec)
    if stage["status"] == "READY":
        print("Manual separator checkpoint is already READY.")
        return "READY"

    while True:
        _, _, stage, _ = prepare_checkpoint(video_number, spec)
        print_checkpoint(stage)
        try:
            choice = input("Press Enter when ready [0 cancels]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("Checkpoint remains WAITING. Resume later with:")
            print(stage["resume_command"])
            return "WAITING"

        if choice == "0":
            cancelled = cancel_checkpoint(video_number, spec)
            print("Process cancelled. All generated files were preserved.")
            print(f"Resume later: {cancelled['resume_command']}")
            return "CANCELLED"
        if choice not in {"", "1"}:
            print("Invalid selection. Press Enter when ready or 0 to cancel.")
            continue

        success, validated = validate_checkpoint(video_number, spec)
        if success:
            print("All separator animations passed validation.")
            return "READY"
        print_validation_failures(validated)
        print("Fix or replace the files, then press Enter again.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare, validate, cancel, or resume manual separator work."
    )
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument(
        "--action",
        choices=("prompt", "prepare", "validate", "skip", "cancel", "status"),
        default="prompt",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.video < 1:
            raise ValueError("--video must be 1 or greater.")
        if args.action == "prepare":
            _, _, stage, _ = prepare_checkpoint(args.video)
            open_checkpoint_folders(stage)
            print_checkpoint(stage)
            return 0
        if args.action == "validate":
            success, stage = validate_checkpoint(args.video)
            if args.json:
                print(json.dumps(stage, ensure_ascii=False, indent=2))
            elif not success:
                print_validation_failures(stage)
            return 0 if success else 1
        if args.action == "skip":
            stage = skip_checkpoint(args.video)
            print(
                json.dumps(stage, ensure_ascii=False, indent=2)
                if args.json
                else "SKIPPED. Story images will start at each chapter's beginning."
            )
            return 0
        if args.action == "cancel":
            stage = cancel_checkpoint(args.video)
            print(
                json.dumps(stage, ensure_ascii=False, indent=2)
                if args.json
                else f"CANCELLED. Resume later: {stage['resume_command']}"
            )
            return 0
        if args.action == "status":
            _, _, stage, _ = prepare_checkpoint(args.video)
            print(
                json.dumps(stage, ensure_ascii=False, indent=2)
                if args.json
                else f"{project_name(args.video)} checkpoint: {stage['status']}"
            )
            return 0

        _, _, stage, _ = prepare_checkpoint(args.video)
        if stage["status"] != "READY":
            open_checkpoint_folders(stage)
        result = prompt_checkpoint(args.video)
        return 0 if result == "READY" else 2
    except Exception as exc:  # noqa: BLE001
        report_cli_error("CHECKPOINT", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
