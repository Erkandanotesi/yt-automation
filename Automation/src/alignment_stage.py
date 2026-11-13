from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from alignment_utils import (  # noqa: E402
    AlignmentValidationError,
    alignment_input_fingerprint,
    build_alignment_artifact,
)
from elevenlabs_tts import (  # noqa: E402
    API_BASE,
    REQUEST_ATTEMPT_EVENTS,
    headers,
    probe_audio,
    request_with_retry,
    with_key_fallback,
)
from file_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    report_cli_error,
    sha256_file,
    sha256_text,
)
from file_locations import project_dir, project_name, resolve_portable_path  # noqa: E402
from tooling import find_executable  # noqa: E402


SEPARATOR_DURATION_SECONDS = float(
    os.getenv("SEPARATOR_DURATION_SECONDS", "3.0")
)
INTER_STORY_GAP_SECONDS = float(
    os.getenv("INTER_STORY_GAP_SECONDS", "2.0")
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build word/sentence/visual-beat timing from ElevenLabs alignment."
        )
    )
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-align audio even when a valid cached sidecar exists.",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Render a timer/transcript diagnostic for one story.",
    )
    parser.add_argument(
        "--story",
        type=int,
        default=1,
        help="1-based story number for --diagnostic (default: 1).",
    )
    return parser.parse_args()


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


def load_project_records(video_dir: Path) -> tuple[dict, dict, list[dict]]:
    project_info = load_json(video_dir / "project_info.json", "Project manifest")
    if project_info.get("status") != "complete":
        raise RuntimeError("News project is incomplete; alignment cannot start.")

    tts_info = load_json(video_dir / "Sesler" / "tts_info.json", "TTS manifest")
    tts_by_text = {
        Path(item.get("text_file", "")).name: item
        for item in tts_info.get("files", [])
        if isinstance(item, dict) and item.get("text_file")
    }

    stories: list[dict] = []
    for story in project_info.get("stories", []):
        text_path = resolve_portable_path(story["text"], video_dir=video_dir)
        tts_entry = tts_by_text.get(text_path.name)
        if tts_entry is None:
            raise RuntimeError(f"TTS metadata is missing for {text_path.name}.")
        audio_path = resolve_portable_path(tts_entry["audio_file"], video_dir=video_dir)
        if not text_path.is_file() or not audio_path.is_file():
            raise FileNotFoundError(
                f"Story text/audio pair is incomplete: {text_path}, {audio_path}"
            )
        stories.append(
            {
                "story": story,
                "text_path": text_path,
                "audio_path": audio_path,
                "tts_entry": tts_entry,
            }
        )

    expected = int(project_info.get("expected_count", len(stories)))
    if len(stories) != expected:
        raise RuntimeError(
            f"Project has {len(stories)} aligned story records; expected {expected}."
        )
    return project_info, tts_info, stories


def cached_alignment(
    path: Path,
    *,
    input_fingerprint: str,
    text_sha256: str,
    audio_sha256: str,
) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = load_json(path, "Alignment")
    except RuntimeError:
        return None
    if payload.get("input_fingerprint") != input_fingerprint:
        return None
    if payload.get("text_sha256") != text_sha256:
        return None
    if payload.get("audio_sha256") != audio_sha256:
        return None
    quality = payload.get("quality", {})
    if quality.get("status") != "passed":
        return None
    if abs(float(quality.get("final_word_to_alignment_end_seconds", 999))) > 0.1:
        return None
    return payload


def forced_alignment_request(
    *,
    text: str,
    audio_path: Path,
) -> tuple[dict, dict]:
    audio_bytes = audio_path.read_bytes()
    attempt_start = len(REQUEST_ATTEMPT_EVENTS)

    def operation(api_key: str):
        return request_with_retry(
            "POST",
            f"{API_BASE}/v1/forced-alignment",
            headers=headers(api_key),
            files={
                "file": (
                    audio_path.name,
                    audio_bytes,
                    "audio/mpeg",
                )
            },
            data={"text": text},
        )

    response, key_label = with_key_fallback(operation)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("ElevenLabs forced alignment returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ElevenLabs forced alignment returned no JSON object.")
    return payload, {
        "api_key_slot": key_label,
        "request_id": response.headers.get("request-id", ""),
        "character_cost": response.headers.get("character-cost", ""),
        "retry_history": REQUEST_ATTEMPT_EVENTS[attempt_start:],
    }


def story_alignment_path(video_dir: Path, text_path: Path) -> Path:
    return (
        video_dir
        / "Sesler"
        / "alignments"
        / f"{text_path.stem}.alignment.json"
    )


def build_story_alignment(
    *,
    story_id: str,
    text: str,
    audio_path: Path,
    alignment_path: Path,
    force: bool,
) -> tuple[dict, dict]:
    media = probe_audio(audio_path)
    audio_checksum = sha256_file(audio_path)
    text_checksum = sha256_text(text)
    fingerprint = alignment_input_fingerprint(
        text=text,
        audio_sha256=audio_checksum,
    )

    if not force:
        cached = cached_alignment(
            alignment_path,
            input_fingerprint=fingerprint,
            text_sha256=text_checksum,
            audio_sha256=audio_checksum,
        )
        if cached is not None:
            return cached, {
                "alignment_status": "cached",
                "alignment_file": str(alignment_path),
                "alignment_sha256": sha256_file(alignment_path),
                "alignment_method": cached.get("method", ""),
                "alignment_input_fingerprint": fingerprint,
            }

    raw_alignment, request_metadata = forced_alignment_request(
        text=text,
        audio_path=audio_path,
    )
    artifact = build_alignment_artifact(
        story_id=story_id,
        canonical_text=text,
        raw_alignment=raw_alignment,
        method="forced_alignment",
        audio_sha256=audio_checksum,
        audio_duration_seconds=media["duration_seconds"],
        request_id=str(request_metadata.get("request_id", "")),
    )
    artifact["input_fingerprint"] = fingerprint
    artifact["provider"] = {
        **request_metadata,
        "overall_loss": raw_alignment.get("loss"),
    }
    atomic_write_json(alignment_path, artifact)
    return artifact, {
        "alignment_status": "generated",
        "alignment_file": str(alignment_path),
        "alignment_sha256": sha256_file(alignment_path),
        "alignment_method": "forced_alignment",
        "alignment_input_fingerprint": fingerprint,
        "alignment_api_key_slot": request_metadata.get("api_key_slot", ""),
        "alignment_request_id": request_metadata.get("request_id", ""),
        "alignment_character_cost": request_metadata.get("character_cost", ""),
        "alignment_retry_history": request_metadata.get("retry_history", []),
    }


def _offset_records(records: list[dict], offset: float) -> list[dict]:
    result: list[dict] = []
    for record in records:
        copy = dict(record)
        copy["timeline_start"] = round(offset + float(record["start"]), 6)
        copy["timeline_end"] = round(offset + float(record["end"]), 6)
        result.append(copy)
    return result


def aggregate_entities(beats: list[dict]) -> dict[str, list[str]]:
    result = {
        "people": [],
        "companies_organizations": [],
        "products": [],
        "places": [],
        "unclassified": [],
    }
    for beat in beats:
        for category, values in beat.get("named_entities", {}).items():
            target = result.setdefault(category, [])
            for value in values:
                if value not in target:
                    target.append(value)
    return result


def build_story_timing_manifest(
    *,
    video_number: int,
    records: list[dict],
    separator_duration_seconds: float,
    inter_story_gap_seconds: float = 2.0,
) -> dict:
    if separator_duration_seconds < 0:
        raise ValueError("SEPARATOR_DURATION_SECONDS cannot be negative.")
    if inter_story_gap_seconds < 0:
        raise ValueError("INTER_STORY_GAP_SECONDS cannot be negative.")

    cursor = 0.0
    timeline_stories: list[dict] = []
    for index, record in enumerate(records, start=1):
        artifact = record["alignment"]
        audio_path = record["audio_path"]
        narration_duration = float(artifact["audio_duration_seconds"])
        separator_start = cursor
        separator_end = separator_start + separator_duration_seconds
        narration_start = cursor
        narration_end = narration_start + narration_duration

        beats = artifact.get("visual_beats", [])
        timeline_stories.append(
            {
                "order": index,
                "story_id": record["story_id"],
                "title": record.get("title", ""),
                "text_file": str(record["text_path"]),
                "audio_file": str(audio_path),
                "alignment_file": str(record["alignment_path"]),
                "separator": {
                    "start": round(separator_start, 6),
                    "end": round(separator_end, 6),
                    "duration": round(separator_duration_seconds, 6),
                    "expected_filename": f"{index:02d}_separator.mp4",
                },
                "narration": {
                    "start": round(narration_start, 6),
                    "end": round(narration_end, 6),
                    "duration": round(narration_duration, 6),
                },
                "gap_after_seconds": round(
                    inter_story_gap_seconds if index < len(records) else 0.0,
                    6,
                ),
                "sentences": _offset_records(
                    artifact.get("sentences", []),
                    narration_start,
                ),
                "phrases": _offset_records(
                    artifact.get("phrases", []),
                    narration_start,
                ),
                "visual_beats": _offset_records(beats, narration_start),
                "named_entities": aggregate_entities(beats),
                "important_claims": [
                    {
                        "text": beat["important_claim"],
                        "importance": beat["importance"],
                        "start": beat["start"],
                        "end": beat["end"],
                        "timeline_start": round(
                            narration_start + beat["start"],
                            6,
                        ),
                        "timeline_end": round(
                            narration_start + beat["end"],
                            6,
                        ),
                    }
                    for beat in beats
                ],
            }
        )
        cursor = max(narration_end, separator_end)
        if index < len(records):
            cursor += inter_story_gap_seconds

    for previous, current in zip(timeline_stories, timeline_stories[1:]):
        if (
            current["separator"]["start"]
            < max(
                previous["narration"]["end"],
                previous["separator"]["end"],
            )
            - 0.001
        ):
            raise AlignmentValidationError("Story timing ranges overlap.")

    return {
        "schema_version": 1,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "separator_duration_seconds": round(separator_duration_seconds, 6),
        "inter_story_gap_seconds": round(inter_story_gap_seconds, 6),
        "separator_timing_mode": "overlay_at_narration_start",
        "required_separator_count": len(timeline_stories),
        "duration_seconds": round(cursor, 6),
        "quality": {
            "status": "passed",
            "story_count": len(timeline_stories),
            "no_story_or_separator_overlaps": True,
            "separator_overlays_narration": True,
            "inter_story_gaps_are_exact": True,
        },
        "stories": timeline_stories,
    }


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _ass_text(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def diagnostic_ass(story: dict) -> str:
    duration = float(story["narration"]["duration"])
    events: list[str] = []
    for second in range(0, int(duration) + 1):
        events.append(
            "Dialogue: 0,"
            f"{_ass_timestamp(second)},{_ass_timestamp(min(duration, second + 1))},"
            "Timer,,0,0,0,,"
            f"{_ass_text(f'TIMER {second:04d}.00s')}"
        )
    for sentence in story["sentences"]:
        local_start = float(sentence["start"])
        local_end = float(sentence["end"])
        label = (
            f"SENTENCE {int(sentence['index']) + 1}  "
            f"[{local_start:.3f}s → {local_end:.3f}s]\n"
            f"{sentence['text']}"
        )
        events.append(
            "Dialogue: 0,"
            f"{_ass_timestamp(local_start)},{_ass_timestamp(local_end)},"
            "Transcript,,60,60,70,,"
            f"{_ass_text(label)}"
        )

    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1280",
            "PlayResY: 720",
            "WrapStyle: 2",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding"
            ),
            (
                "Style: Timer,Arial,30,&H00FFFFFF,&H000000FF,&H00000000,"
                "&H80000000,1,0,0,0,100,100,0,0,1,2,1,8,30,30,24,1"
            ),
            (
                "Style: Transcript,Arial,34,&H00FFFFFF,&H000000FF,&H00000000,"
                "&HA0000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,70,1"
            ),
            "",
            "[Events]",
            (
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text"
            ),
            *events,
            "",
        ]
    )


def render_diagnostic(video_dir: Path, timing: dict, story_number: int) -> Path:
    if not 1 <= story_number <= len(timing["stories"]):
        raise ValueError(
            f"--story must be between 1 and {len(timing['stories'])}."
        )
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required for the timing diagnostic.")

    story = timing["stories"][story_number - 1]
    alignment_dir = resolve_portable_path(
        story["alignment_file"], video_dir=video_dir
    ).parent
    ass_path = alignment_dir / f"{story_number:02d}_timing_diagnostic.ass"
    output_path = video_dir / f"timing_diagnostic_story_{story_number:02d}.mp4"
    atomic_write_text(ass_path, diagnostic_ass(story))

    escaped_ass = (
        str(ass_path.resolve())
        .replace("\\", "/")
        .replace(":", r"\:")
        .replace("'", r"\'")
    )
    duration = float(story["narration"]["duration"])
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x10131a:s=1280x720:r=30:d={duration:.6f}",
        "-i",
        story["audio_file"],
        "-vf",
        f"ass='{escaped_ass}'",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=max(120, int(duration * 4)),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "FFmpeg timing diagnostic failed: "
            + (completed.stderr.strip() or "unknown error")
        )
    media = probe_audio(output_path)
    if media["duration_seconds"] <= 0:
        raise RuntimeError("Timing diagnostic has no valid audio duration.")
    return output_path


def run_alignment_stage(
    *,
    video_number: int,
    force: bool = False,
    diagnostic: bool = False,
    story_number: int = 1,
) -> dict:
    if video_number < 1:
        raise ValueError("--video must be 1 or greater.")
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")

    _, tts_info, stories = load_project_records(video_dir)
    timing_records: list[dict] = []
    updated_tts_by_name: dict[str, dict] = {}

    for index, record in enumerate(stories, start=1):
        text = record["text_path"].read_text(encoding="utf-8").strip()
        alignment_path = story_alignment_path(video_dir, record["text_path"])
        print(f"[{index}/{len(stories)}] Aligning {record['text_path'].name}")
        artifact, metadata = build_story_alignment(
            story_id=record["story"]["story_id"],
            text=text,
            audio_path=record["audio_path"],
            alignment_path=alignment_path,
            force=force,
        )
        print(f"  {metadata['alignment_status']}: {alignment_path.name}")
        updated_tts_by_name[record["text_path"].name] = {
            **record["tts_entry"],
            **metadata,
        }
        timing_records.append(
            {
                "story_id": record["story"]["story_id"],
                "title": record["story"].get("title", ""),
                "text_path": record["text_path"],
                "audio_path": record["audio_path"],
                "alignment_path": alignment_path,
                "alignment": artifact,
            }
        )

    updated_tts_info = dict(tts_info)
    updated_tts_info["alignment_updated_at_utc"] = datetime.now(
        timezone.utc
    ).isoformat()
    updated_tts_info["files"] = [
        updated_tts_by_name.get(Path(item.get("text_file", "")).name, item)
        for item in tts_info.get("files", [])
    ]
    atomic_write_json(video_dir / "Sesler" / "tts_info.json", updated_tts_info)

    timing = build_story_timing_manifest(
        video_number=video_number,
        records=timing_records,
        separator_duration_seconds=SEPARATOR_DURATION_SECONDS,
        inter_story_gap_seconds=INTER_STORY_GAP_SECONDS,
    )
    timing_path = video_dir / "story_timing.json"
    atomic_write_json(timing_path, timing)
    print(f"Timing manifest: {timing_path}")

    if diagnostic:
        diagnostic_path = render_diagnostic(video_dir, timing, story_number)
        print(f"Diagnostic video: {diagnostic_path}")
        timing["diagnostic_video"] = str(diagnostic_path)
        atomic_write_json(timing_path, timing)
    return timing


def main() -> int:
    args = parse_arguments()
    try:
        run_alignment_stage(
            video_number=args.video,
            force=args.force,
            diagnostic=args.diagnostic,
            story_number=args.story,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        report_cli_error("ALIGNMENT", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
