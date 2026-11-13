from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from file_locations import project_dir, project_name, resolve_portable_path
from file_utils import atomic_write_json, atomic_write_text, sha256_file
from tooling import find_executable


STAGE_NAME = "ai_story_videos"
SCHEMA_VERSION = 1
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MINIMUM_WIDTH = 640
MINIMUM_HEIGHT = 360
SHORTFALL_TOLERANCE_SECONDS = 0.15


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def checkpoint_directory(video_dir: Path) -> Path:
    return video_dir / "04_ai_story_videos"


def manifest_path(video_dir: Path) -> Path:
    return checkpoint_directory(video_dir) / "ai_video_manifest.json"


def _state(video_dir: Path) -> dict:
    path = video_dir / "state.json"
    return load_json(path, "Project state") if path.is_file() else {}


def _write_state(video_dir: Path, status: str, reason: str, **extra: object) -> None:
    state = _state(video_dir)
    now = utc_now()
    state.setdefault("stages", {})[STAGE_NAME] = {
        "status": status,
        "reason": reason,
        "updated_at_utc": now,
        **extra,
    }
    state["updated_at_utc"] = now
    atomic_write_json(video_dir / "state.json", state)


def _expected_entries(video_dir: Path) -> list[dict]:
    storyboard_path = video_dir / "04_storyboard" / "storyboard.json"
    storyboard = load_json(storyboard_path, "Storyboard")
    if storyboard.get("quality", {}).get("status") != "passed":
        raise RuntimeError("AI-video prompts require a complete passing storyboard.")
    entries: list[dict] = []
    for story in sorted(
        storyboard.get("stories", []),
        key=lambda item: int(item.get("order", 0)),
    ):
        images = story.get("images", [])
        if not images:
            raise RuntimeError(f"Storyboard has no generated interval for {story.get('story_id')}.")
        image = images[0]
        time_range = image.get("time_range", {})
        start = float(time_range.get("start", 0))
        end = float(time_range.get("end", 0))
        duration = end - start
        if duration <= 0:
            raise RuntimeError(f"Storyboard AI-video interval is invalid for {story.get('story_id')}.")
        order = int(story["order"])
        depiction = str(
            image.get("ai_video_prompt")
            or image.get("visual_concept")
            or image.get("prompt")
            or ""
        ).strip()
        if not depiction:
            raise RuntimeError(f"Storyboard has no AI-video depiction for story {order}.")
        entries.append(
            {
                "story_id": story["story_id"],
                "order": order,
                "title": story.get("title", ""),
                "image_index": int(image.get("image_index", 1)),
                "role": image.get("role", "timed_story_hero"),
                "local_start_seconds": round(start, 6),
                "local_end_seconds": round(end, 6),
                "target_duration_seconds": round(duration, 6),
                # The user deliberately supplies the five clips as 1..5.
                # Keep the stem extension-agnostic so 1.mp4, 1.mov, etc. work.
                "expected_stem": str(order),
                "depiction": depiction,
            }
        )
    if not entries:
        raise RuntimeError("Storyboard contains no stories for AI-video prompts.")
    return entries


def _prompt_text(entry: dict) -> str:
    duration = float(entry["target_duration_seconds"])
    return (
        f"{int(entry['order'])}- {entry['title']}\n"
        f"Target interval: {float(entry['local_start_seconds']):.3f}s to "
        f"{float(entry['local_end_seconds']):.3f}s "
        f"({duration:.3f} seconds)\n"
        f"Save as: {entry['expected_stem']}.mp4 (the extension may vary)\n\n"
        "Text-to-video depiction (English):\n"
        f"{entry['depiction']}\n\n"
        "Production constraints: 16:9 landscape, natural coherent "
        "motion for the full target duration, no cuts outside the described action, "
        "no watermark, no subtitles, no Turkish text, no invented logos or people. "
        "Any visible words must be exact English wording supplied in the depiction."
    )


def prepare(video_number: int, *, open_folder: bool = True) -> dict:
    video_dir = project_dir(video_number)
    storyboard_path = video_dir / "04_storyboard" / "storyboard.json"
    storyboard = load_json(storyboard_path, "Storyboard")
    configured_images = storyboard.get("configuration", {}).get(
        "images_per_story"
    )
    try:
        configured_image_count = int(configured_images)
    except (TypeError, ValueError):
        configured_image_count = None
    if configured_image_count == 0 or (
        storyboard.get("quality", {}).get("ai_image_generation_disabled") is True
    ):
        return skip(
            video_number,
            reason="AI image generation is disabled.",
            decision="use_stock_media",
        )
    existing_path = manifest_path(video_dir)
    existing = load_json(existing_path, "AI-video checkpoint manifest") if existing_path.is_file() else {}
    if existing.get("status") == "READY" and status(video_number).get("status") == "READY":
        if open_folder and hasattr(os, "startfile"):
            prompt = Path(str(existing.get("combined_prompt_file", "")))
            if prompt.is_file():
                os.startfile(prompt)  # type: ignore[attr-defined]
            os.startfile(checkpoint_directory(video_dir))  # type: ignore[attr-defined]
        return existing
    entries = _expected_entries(video_dir)
    directory = checkpoint_directory(video_dir)
    directory.mkdir(parents=True, exist_ok=True)
    combined: list[str] = []
    for entry in entries:
        prompt = _prompt_text(entry)
        prompt_path = directory / f"{int(entry['order']):02d}_ai_video_prompt.txt"
        atomic_write_text(prompt_path, prompt + "\n")
        entry["prompt_file"] = str(prompt_path)
        combined.append(prompt)
    combined_path = directory / "AI_VIDEO_PROMPTS.txt"
    atomic_write_text(combined_path, ("\n\n" + "=" * 72 + "\n\n").join(combined) + "\n")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "status": "WAITING",
        "decision": "use_ai_story_videos",
        "created_at_utc": existing.get("created_at_utc", utc_now()),
        "storyboard_sha256": sha256_file(video_dir / "04_storyboard" / "storyboard.json"),
        "directory": str(directory),
        "combined_prompt_file": str(combined_path),
        "entries": entries,
    }
    atomic_write_json(manifest_path(video_dir), payload)
    _write_state(
        video_dir,
        "WAITING",
        "Waiting for user-supplied AI-generated story videos.",
        directory=str(directory),
        manifest=str(manifest_path(video_dir)),
    )
    if open_folder and hasattr(os, "startfile"):
        os.startfile(combined_path)  # type: ignore[attr-defined]
        os.startfile(directory)  # type: ignore[attr-defined]
    return payload


def _probe(path: Path) -> dict:
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        raise RuntimeError("FFprobe is required to validate AI-generated videos.")
    result = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,pix_fmt",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe rejected {path.name}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _decode_check(path: Path, duration_seconds: float) -> None:
    """Decode the required portion so a readable header cannot mask damaged frames."""
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required to validate AI-generated videos.")
    result = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:v:0", "-an", "-t", f"{duration_seconds:.6f}",
            "-f", "null", os.devnull,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg could not decode {path.name}: {result.stderr.strip()}")


def _matching_video(directory: Path, stem: str) -> Path:
    matches = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.stem.casefold() == stem.casefold()
        and path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one video named {stem} with a supported extension; "
            f"found {len(matches)}."
        )
    return matches[0]


def validate(video_number: int) -> dict:
    video_dir = project_dir(video_number)
    path = manifest_path(video_dir)
    manifest = load_json(path, "AI-video checkpoint manifest")
    if manifest.get("status") == "READY" and status(video_number).get("status") == "READY":
        return manifest
    storyboard_path = video_dir / "04_storyboard" / "storyboard.json"
    if manifest.get("storyboard_sha256") != sha256_file(storyboard_path):
        raise RuntimeError("Storyboard changed; regenerate the AI-video prompts.")
    directory = checkpoint_directory(video_dir)
    expected_stems = {
        str(item.get("expected_stem", "")).casefold()
        for item in manifest.get("entries", [])
    }
    unexpected = sorted(
        item.name
        for item in directory.iterdir()
        if item.is_file()
        and item.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES
        and item.stem.casefold() not in expected_stems
    )
    if unexpected:
        raise RuntimeError(
            "Unexpected video files in the AI-video folder; keep only the numbered "
            f"story clips: {', '.join(unexpected)}"
        )
    validated: list[dict] = []
    for expected in manifest.get("entries", []):
        video = _matching_video(directory, str(expected["expected_stem"]))
        probe = _probe(video)
        stream = next(
            (item for item in probe.get("streams", []) if item.get("codec_type") == "video"),
            None,
        )
        if stream is None:
            raise RuntimeError(f"AI-video file has no video stream: {video}")
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width < MINIMUM_WIDTH or height < MINIMUM_HEIGHT:
            raise RuntimeError(
                f"{video.name} is {width}x{height}; minimum is "
                f"{MINIMUM_WIDTH}x{MINIMUM_HEIGHT}."
            )
        duration = float(probe.get("format", {}).get("duration") or 0)
        target = float(expected["target_duration_seconds"])
        if duration + SHORTFALL_TOLERANCE_SECONDS < target:
            raise RuntimeError(
                f"{video.name} is {duration:.3f}s; it must cover at least "
                f"{target:.3f}s. Longer clips are trimmed safely."
            )
        _decode_check(video, min(duration, target))
        validated.append(
            {
                **expected,
                "file": str(video.resolve()),
                "sha256": sha256_file(video),
                "duration_seconds": round(duration, 6),
                "width": width,
                "height": height,
                "codec": stream.get("codec_name", ""),
            }
        )
    updated = {
        **manifest,
        "status": "READY",
        "validated_at_utc": utc_now(),
        "entries": validated,
    }
    atomic_write_json(path, updated)
    _write_state(
        video_dir,
        "READY",
        "All user-supplied AI-generated story videos passed validation.",
        manifest=str(path),
        manifest_sha256=sha256_file(path),
    )
    return updated


def skip(
    video_number: int,
    *,
    reason: str = "Optional AI-generated story videos were skipped.",
    decision: str = "use_storyboard_images",
) -> dict:
    video_dir = project_dir(video_number)
    directory = checkpoint_directory(video_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = manifest_path(video_dir)
    if path.is_file():
        existing = load_json(path, "AI-video checkpoint manifest")
        if (
            existing.get("status") == "SKIPPED"
            and existing.get("decision") == decision
            and existing.get("reason") == reason
        ):
            if _state(video_dir).get("stages", {}).get(STAGE_NAME, {}).get("status") != "SKIPPED":
                _write_state(
                    video_dir,
                    "SKIPPED",
                    reason,
                    manifest=str(path),
                    manifest_sha256=sha256_file(path),
                )
            return existing
    payload = {
        "schema_version": SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "status": "SKIPPED",
        "decision": decision,
        "reason": reason,
        "updated_at_utc": utc_now(),
        "entries": [],
    }
    atomic_write_json(path, payload)
    _write_state(
        video_dir,
        "SKIPPED",
        reason,
        manifest=str(path),
        manifest_sha256=sha256_file(path),
    )
    return payload


def status(video_number: int) -> dict:
    video_dir = project_dir(video_number)
    path = manifest_path(video_dir)
    if not path.is_file():
        return {"status": "NOT_STARTED", "manifest": str(path)}
    payload = load_json(path, "AI-video checkpoint manifest")
    state_status = _state(video_dir).get("stages", {}).get(STAGE_NAME, {}).get("status")
    if payload.get("status") == "SKIPPED" and state_status == "SKIPPED":
        return {"status": "SKIPPED", "manifest": str(path)}
    if payload.get("status") != "READY" or state_status != "READY":
        return {"status": "WAITING", "manifest": str(path)}
    try:
        storyboard = video_dir / "04_storyboard" / "storyboard.json"
        if (
            not storyboard.is_file()
            or payload.get("storyboard_sha256") != sha256_file(storyboard)
        ):
            raise RuntimeError("Storyboard changed after AI-video validation.")
        for entry in payload.get("entries", []):
            video = resolve_portable_path(entry["file"], video_dir=video_dir)
            if not video.is_file() or sha256_file(video) != entry.get("sha256"):
                raise RuntimeError(f"AI video is missing or changed: {video}")
    except (KeyError, RuntimeError):
        return {"status": "STALE", "manifest": str(path)}
    return {"status": "READY", "manifest": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Optional user-supplied AI story-video checkpoint.")
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument("--action", choices=("prepare", "validate", "skip", "status"), required=True)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args.video, open_folder=not args.no_open)
        elif args.action == "validate":
            result = validate(args.video)
        elif args.action == "skip":
            result = skip(args.video)
        else:
            result = status(args.video)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[AI VIDEO ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
