from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
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

from alignment_utils import (  # noqa: E402
    alignment_input_fingerprint,
    build_alignment_artifact,
)
import elevenlabs_tts  # noqa: E402
from file_locations import (  # noqa: E402
    project_dir,
    project_name,
    resolve_portable_path,
)
from file_utils import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_json,
    json_fingerprint,
    report_cli_error,
    sha256_file,
    sha256_text,
)
import render_stage  # noqa: E402
from tooling import find_executable  # noqa: E402
import youtube_upload  # noqa: E402


STAGING_DIR_NAME = ".staging_reset"
STAGING_MANIFEST_NAME = "staged_voiceover.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _progress(message: str) -> None:
    print(f"[PROGRESS] {message}", file=sys.stderr, flush=True)


def _record_event(video_dir: Path, payload: dict[str, Any]) -> None:
    events_path = video_dir / "orchestration" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at_utc": utc_now(), **payload}
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _staging_dir(video_dir: Path) -> Path:
    return video_dir / "Sesler" / STAGING_DIR_NAME


def _remove_staging_dir(video_dir: Path) -> bool:
    staging = _staging_dir(video_dir)
    if not staging.exists():
        return False
    shutil.rmtree(staging, ignore_errors=True)
    return not staging.exists()


def _concat_preview_mp3(audio_files: list[Path], output_path: Path) -> dict[str, Any]:
    if not audio_files:
        raise RuntimeError("No voiceover audio files were generated to combine.")
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required to combine voiceover preview audio.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if len(audio_files) == 1:
        shutil.copy2(audio_files[0], output_path)
        return elevenlabs_tts.probe_audio(output_path)

    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
    ]
    filter_inputs = []
    for index, path in enumerate(audio_files):
        command.extend(["-i", str(path)])
        filter_inputs.append(f"[{index}:a]")
    filter_complex = (
        f"{''.join(filter_inputs)}concat=n={len(audio_files)}:v=0:a=1,"
        "aresample=44100,aformat=channel_layouts=stereo[outa]"
    )
    temp_output = output_path.with_suffix(".tmp.mp3")
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[outa]",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-y",
            str(temp_output),
        ]
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not temp_output.is_file():
        temp_output.unlink(missing_ok=True)
        raise RuntimeError(
            "Failed to combine voiceover preview audio: "
            f"{completed.stderr.strip() or 'FFmpeg error'}"
        )
    media = elevenlabs_tts.probe_audio(temp_output)
    os.replace(temp_output, output_path)
    return media


def generate_voiceover_preview(video_number: int) -> dict[str, Any]:
    if video_number < 1:
        raise ValueError("--video must be 1 or greater.")
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")

    audio_dir = video_dir / "Sesler"
    audio_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = _staging_dir(video_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    _progress(f"Generating voiceovers for {project_name(video_number)}...")
    log_path = video_dir / "elevenlabs.log"
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    with log_path.open("a", encoding="utf-8") as log_file:
        sys.stdout = elevenlabs_tts.TeeStream(original_stdout, log_file, compact=True)
        sys.stderr = elevenlabs_tts.TeeStream(original_stderr, log_file, compact=True)
        try:
            print()
            elevenlabs_tts.info("=" * 68)
            elevenlabs_tts.info(
                f"ElevenLabs voiceover reset started. Log file: {log_path}"
            )
            tts_provider = os.getenv("TTS_PROVIDER", "elevenlabs").strip().lower()
            if tts_provider in {"qwen", "qwen3", "qwen_tts", "local"}:
                cmd = [
                    sys.executable,
                    str(SOURCE_ROOT / "qwen_tts_local.py"),
                    "--video", str(video_number),
                    "--overwrite",
                    "--output-dir", str(staging_dir),
                ]
                subprocess.run(cmd, check=True)
            else:
                elevenlabs_tts.synthesize_video(
                    video_number,
                    overwrite=True,
                    output_dir=staging_dir,
                )
        finally:
            elevenlabs_tts.info("ElevenLabs voiceover reset finished.")
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    staged_tts_info = _load_optional_json(staging_dir / "tts_info.json")
    staged_files = staged_tts_info.get("files", [])
    if not staged_files:
        raise RuntimeError("Voiceover synthesis produced no audio files.")

    staged_audio_paths = [
        Path(str(item["audio_file"]))
        for item in staged_files
        if isinstance(item, dict) and item.get("audio_file")
    ]

    _progress("Combining story voiceovers into preview audio...")
    preview_filename = f"{project_name(video_number)}_voiceover_preview.mp3"
    preview_path = staging_dir / preview_filename
    preview_media = _concat_preview_mp3(staged_audio_paths, preview_path)

    manifest = {
        **staged_tts_info,
        "video_number": video_number,
        "preview_file": str(preview_path),
        "file_name": f"{project_name(video_number)}_voiceover.mp3",
        "duration_seconds": preview_media["duration_seconds"],
        "story_count": len(staged_files),
    }
    atomic_write_json(staging_dir / STAGING_MANIFEST_NAME, manifest)
    _progress("Voiceover preview ready.")
    return {
        "status": "PREVIEW_READY",
        "video_number": video_number,
        "video_name": project_name(video_number),
        "preview_file": str(preview_path),
        "file_name": manifest["file_name"],
        "duration_seconds": preview_media["duration_seconds"],
        "story_count": len(staged_files),
        "voice_name": str(staged_tts_info.get("voice_name", "")),
    }


def _mix_audio_track_preserving_timeline(
    video_dir: Path,
    plan: dict[str, Any],
    separators: list[Path],
    settings: render_stage.RenderSettings,
) -> Path:
    """Mix updated story voiceovers into final_mix.wav while keeping chapter slots intact."""
    output = video_dir / "08_render" / "intermediates" / "final_mix.wav"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(render_stage._ffmpeg()),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-filter_complex_threads",
        "1",
    ]
    for narration in plan["narration"]:
        narration_path = resolve_portable_path(narration["file"], video_dir=video_dir)
        command.extend(["-i", str(narration_path)])
    separator_start_index = len(plan["narration"])
    for path in separators:
        command.extend(["-i", str(resolve_portable_path(path, video_dir=video_dir))])
    next_input_index = separator_start_index + len(separators)
    music = plan.get("background_music")
    music_inputs: list[tuple[int, dict[str, Any]]] = []
    if music:
        playlist = music.get("tracks") or []
        if playlist:
            for track in playlist:
                track_path = resolve_portable_path(track.get("track_file", ""))
                if track_path.is_file():
                    music_inputs.append((next_input_index, track))
                    command.extend(["-stream_loop", "-1", "-i", str(track_path)])
                    next_input_index += 1
        elif music.get("track_file"):
            track_path = resolve_portable_path(music["track_file"])
            if track_path.is_file():
                music_inputs.append(
                    (
                        next_input_index,
                        {
                            "track_file": str(track_path),
                            "timeline_start": 0.0,
                            "segment_duration_seconds": float(plan["duration_seconds"]),
                            "fade_in_seconds": music["fade_in_seconds"],
                            "fade_out_seconds": music["fade_out_seconds"],
                        },
                    )
                )
                command.extend(["-stream_loop", "-1", "-i", str(track_path)])
                next_input_index += 1
    sfx = plan.get("glitch_sfx") or {}
    sfx_index = -1
    if sfx.get("file") and plan.get("glitch_events"):
        sfx_path = resolve_portable_path(sfx["file"])
        if sfx_path.is_file():
            sfx_index = next_input_index
            command.extend(["-i", str(sfx_path)])
            next_input_index += 1
    timeline_sfx = plan.get("timeline_sfx") or {}
    timeline_sfx_indices: dict[str, int] = {}
    for asset_id, asset in sorted(timeline_sfx.get("assets", {}).items()):
        asset_file = resolve_portable_path(asset.get("file", ""))
        if not asset_file.is_file():
            continue
        if not any(
            event.get("sound") == asset_id
            for event in timeline_sfx.get("events", [])
        ):
            continue
        timeline_sfx_indices[asset_id] = next_input_index
        command.extend(["-i", str(asset_file)])
        next_input_index += 1

    filters: list[str] = []
    labels: list[str] = []
    voiceover_gain = render_stage._db_factor(settings.voiceover_gain_db)
    for index, narration in enumerate(plan["narration"]):
        delay = round(float(narration["start"]) * 1000)
        slot_duration = float(narration["duration"])
        narration_path = resolve_portable_path(narration["file"], video_dir=video_dir)
        actual_duration = slot_duration
        try:
            probed = elevenlabs_tts.probe_audio(narration_path)
            actual_duration = float(probed.get("duration_seconds") or slot_duration)
        except Exception:
            actual_duration = slot_duration

        tempo_filter = ""
        if slot_duration > 0.1 and actual_duration > slot_duration + 0.02:
            ratio = min(2.0, max(1.0, actual_duration / slot_duration))
            tempo_filter = f"atempo={ratio:.6f},"

        label = f"voice{index}"
        filters.append(
            f"[{index}:a]{tempo_filter}atrim=start=0:end={slot_duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={voiceover_gain:.6f},"
            f"adelay={delay}|{delay}[{label}]"
        )
        labels.append(f"[{label}]")

    separator_gain = render_stage._db_factor(settings.separator_audio_gain_db)
    for offset, item in enumerate(plan.get("separators", [])):
        if not item.get("has_audio") or offset >= len(separators):
            continue
        index = separator_start_index + offset
        delay = round(float(item["start"]) * 1000)
        duration = float(item["duration"])
        label = f"sepaudio{offset}"
        filters.append(
            f"[{index}:a]atrim=start=0:end={duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={separator_gain:.6f},adelay={delay}|{delay}[{label}]"
        )
        labels.append(f"[{label}]")

    if music_inputs and music:
        speech_factor = float(music["ducking"]["speech_gain_factor"])
        for playlist_index, (input_index, track) in enumerate(music_inputs):
            segment_duration = float(track["segment_duration_seconds"])
            timeline_start = float(track.get("timeline_start", 0.0))
            fade_in = min(float(track["fade_in_seconds"]), segment_duration / 2)
            fade_out = min(float(track["fade_out_seconds"]), segment_duration / 2)
            delay = round(timeline_start * 1000)
            label = f"music{playlist_index}"
            filters.append(
                f"[{input_index}:a]atrim=duration={segment_duration},"
                "asetpts=PTS-STARTPTS,aresample=48000,"
                "aformat=channel_layouts=stereo,"
                f"loudnorm=I={music['normalization']['target_lufs']}:TP=-2:LRA=7,"
                f"volume={speech_factor:.6f},afade=t=in:st=0:d={fade_in},"
                f"afade=t=out:st={max(0.0, segment_duration - fade_out)}:d={fade_out},"
                f"adelay={delay}|{delay}[{label}]"
            )
            labels.append(f"[{label}]")

    if sfx_index >= 0:
        glitches = plan["glitch_events"]
        filters.append(
            f"[{sfx_index}:a]asplit={len(glitches)}"
            + "".join(f"[sfx{index}]" for index in range(len(glitches)))
        )
        sfx_factor = render_stage._db_factor(float(sfx.get("gain_db", -15)))
        for index, event in enumerate(glitches):
            delay = round(float(event["start"]) * 1000)
            label = f"sfxdelayed{index}"
            filters.append(
                f"[sfx{index}]aresample=48000,aformat=channel_layouts=stereo,"
                f"volume={sfx_factor:.6f},adelay={delay}|{delay}[{label}]"
            )
            labels.append(f"[{label}]")

    for asset_id, input_index in timeline_sfx_indices.items():
        asset = timeline_sfx["assets"][asset_id]
        events = [
            event
            for event in timeline_sfx.get("events", [])
            if event.get("sound") == asset_id
        ]
        if len(events) == 1:
            source_labels = [f"{input_index}:a"]
        else:
            source_labels = [
                f"timeline_{asset_id}_{index}"
                for index in range(len(events))
            ]
            filters.append(
                f"[{input_index}:a]asplit={len(events)}"
                + "".join(f"[{label}]" for label in source_labels)
            )
        gain_factor = render_stage._db_factor(float(asset.get("gain_db", -12)))
        asset_duration = float(asset.get("duration_seconds", 3.0))
        for index, (event, source_label) in enumerate(
            zip(events, source_labels, strict=True)
        ):
            delay = round(float(event["start"]) * 1000)
            label = f"timeline_delayed_{asset_id}_{index}"
            filters.append(
                f"[{source_label}]atrim=start=0:end={asset_duration},"
                "asetpts=PTS-STARTPTS,aresample=48000,"
                "aformat=channel_layouts=stereo,"
                f"volume={gain_factor:.6f},adelay={delay}|{delay}[{label}]"
            )
            labels.append(f"[{label}]")

    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
        "dropout_transition=0:normalize=0,"
        f"loudnorm=I={settings.final_target_lufs}:TP={settings.final_true_peak_db}:LRA=11,"
        "alimiter=limit=0.794:level=false[mix]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[mix]",
            "-t",
            str(plan["duration_seconds"]),
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-y",
            str(output),
        ]
    )
    render_stage._run(command, "Mix updated voiceover audio track")
    return output


def _remix_project_video_audio(video_number: int, video_dir: Path) -> dict[str, Any]:
    render_dir = video_dir / "08_render"
    plan_path = render_dir / "render_plan.json"
    report_path = render_dir / "render_report.json"
    final_video = render_dir / f"{project_name(video_number)}_final.mp4"
    preview_video = render_dir / f"{project_name(video_number)}_preview_540p.mp4"

    if not final_video.is_file() or not plan_path.is_file():
        return {"remixed": False, "reason": "No existing final render to remix."}

    _progress("Mixing new voiceover into the final rendered video...")
    settings = render_stage.RenderSettings.from_environment()
    settings.validate()
    plan = _load_optional_json(plan_path)
    if not plan.get("narration"):
        return {"remixed": False, "reason": "Render plan has no narration entries."}

    for narration_item in plan.get("narration", []):
        narration_file = resolve_portable_path(narration_item.get("file", ""), video_dir=video_dir)
        if narration_file.is_file():
            narration_item["sha256"] = sha256_file(narration_file)
    atomic_write_json(plan_path, plan)

    separators: list[Path] = []
    if plan.get("separators"):
        separators = render_stage._normalize_separators(video_dir, plan, settings)

    audio_track = _mix_audio_track_preserving_timeline(
        video_dir,
        plan,
        separators,
        settings,
    )

    temp_final = render_dir / f"{project_name(video_number)}_final.voiceover_tmp.mp4"
    render_stage._run(
        [
            str(render_stage._ffmpeg()),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(final_video),
            "-i",
            str(audio_track),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-aac_coder",
            "fast",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-t",
            str(plan["duration_seconds"]),
            "-movflags",
            "+faststart",
            "-y",
            str(temp_final),
        ],
        "Remux final master with updated voiceover",
    )
    os.replace(temp_final, final_video)

    if preview_video.is_file():
        temp_preview = render_dir / f"{project_name(video_number)}_preview_540p.voiceover_tmp.mp4"
        render_stage._run(
            [
                str(render_stage._ffmpeg()),
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(preview_video),
                "-i",
                str(final_video),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                str(temp_preview),
            ],
            "Remux 540p preview with updated voiceover",
        )
        os.replace(temp_preview, preview_video)
    else:
        preview_video = render_stage._render_preview(final_video, video_dir, settings)

    _progress("Running quality checks and updating project manifests...")
    expected_duration = float(plan["duration_seconds"])
    final_qa = render_stage._qa_output(
        final_video,
        settings.width,
        settings.height,
        expected_duration,
        settings,
    )
    preview_qa = render_stage._qa_output(
        preview_video,
        settings.preview_width,
        settings.preview_height,
        expected_duration,
        settings,
    )

    existing_report = _load_optional_json(report_path)
    report = {
        **existing_report,
        "schema_version": existing_report.get("schema_version", render_stage.SCHEMA_VERSION),
        "video_number": video_number,
        "created_at_utc": utc_now(),
        "duration_seconds": plan["duration_seconds"],
        "final": {
            "file": str(final_video),
            "qa": final_qa,
        },
        "preview": {
            "file": str(preview_video),
            "qa": preview_qa,
        },
        "quality": {
            "status": (
                "passed"
                if final_qa["status"] == "passed" and preview_qa["status"] == "passed"
                else "failed"
            )
        },
    }
    if settings.cleanup_intermediates:
        render_stage._cleanup_paths(
            video_dir,
            [video_dir / "08_render" / "intermediates"],
            label="voiceover remix intermediates",
        )
    atomic_write_json(report_path, report)

    state_path = video_dir / "state.json"
    state = _load_optional_json(state_path)
    if state:
        render_state = state.setdefault("stages", {}).setdefault("render", {})
        render_state.update(
            {
                "status": "RENDER_READY" if report["quality"]["status"] == "passed" else "FAILED",
                "render_plan": str(plan_path),
                "render_report": str(report_path),
                "final_file": str(final_video),
                "preview_file": str(preview_video),
                "input_fingerprint": plan.get("input_fingerprint", render_state.get("input_fingerprint", "")),
                "updated_at_utc": utc_now(),
            }
        )
        state["updated_at_utc"] = render_state["updated_at_utc"]

    publish_dir = video_dir / "09_publish"
    manifest_path = publish_dir / "youtube_upload_manifest.json"
    package_path = publish_dir / "publish_package.json"
    new_video_sha256 = final_qa["sha256"]

    if manifest_path.is_file():
        upload_manifest = _load_optional_json(manifest_path)
        if upload_manifest:
            upload_manifest["video_file"] = str(final_video)
            upload_manifest["video_sha256"] = new_video_sha256
            thumbnail_path = resolve_portable_path(
                upload_manifest.get("thumbnail_file", ""), video_dir=video_dir
            )
            if thumbnail_path.is_file():
                upload_manifest["thumbnail_sha256"] = sha256_file(thumbnail_path)
            atomic_write_json(manifest_path, upload_manifest)

    if package_path.is_file():
        package = _load_optional_json(package_path)
        copy_path = publish_dir / "publishing_copy.json"
        bg_path = resolve_portable_path(
            package.get("thumbnail_background", {}).get("file", ""), video_dir=video_dir
        )
        if (
            package
            and copy_path.is_file()
            and (video_dir / "project_info.json").is_file()
            and (video_dir / "story_timing.json").is_file()
            and bg_path.is_file()
        ):
            package["input_fingerprint"] = json_fingerprint(
                {
                    "copy": sha256_file(copy_path),
                    "project": sha256_file(video_dir / "project_info.json"),
                    "timing": sha256_file(video_dir / "story_timing.json"),
                    "render": sha256_file(report_path),
                    "background": sha256_file(bg_path),
                    "thumbnail_headline": package.get("recommended_thumbnail_text", ""),
                    "thumbnail_text_rendering": package.get("thumbnail_text_rendering", ""),
                    "settings": package.get("configuration", {}),
                }
            )
            atomic_write_json(package_path, package)
            if state and "publish" in state.get("stages", {}):
                state["stages"]["publish"]["input_fingerprint"] = package["input_fingerprint"]
                state["stages"]["publish"]["updated_at_utc"] = utc_now()
                state["updated_at_utc"] = state["stages"]["publish"]["updated_at_utc"]

    if state:
        atomic_write_json(state_path, state)

    return {
        "remixed": True,
        "final_file": str(final_video),
        "final_sha256": new_video_sha256,
        "preview_file": str(preview_video),
        "qa_status": report["quality"]["status"],
    }


def save_voiceover_reset(video_number: int) -> dict[str, Any]:
    if video_number < 1:
        raise ValueError("--video must be 1 or greater.")
    video_dir = project_dir(video_number)
    staging_dir = _staging_dir(video_dir)
    manifest_path = staging_dir / STAGING_MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"No staged voiceover preview found for {project_name(video_number)}. "
            "Generate a voiceover preview first."
        )

    staged = _load_optional_json(manifest_path)
    staged_files = staged.get("files", [])
    if not staged_files:
        raise RuntimeError("Staged voiceover manifest contains no audio files.")

    audio_dir = video_dir / "Sesler"
    alignment_dir = audio_dir / "alignments"
    audio_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)

    previous_info = elevenlabs_tts.load_previous_tts_info(audio_dir)
    previous_by_name = {
        Path(str(item.get("text_file", ""))).name: item
        for item in previous_info.get("files", [])
        if isinstance(item, dict) and item.get("text_file")
    }

    _progress(f"Saving new voiceover files for {project_name(video_number)}...")
    updated_entries: list[dict[str, Any]] = []
    for item in staged_files:
        staged_audio = Path(str(item["audio_file"]))
        staged_alignment = Path(str(item["alignment_file"]))
        text_path = Path(str(item["text_file"]))
        target_audio = audio_dir / f"{text_path.stem}.mp3"
        target_alignment = alignment_dir / f"{text_path.stem}.alignment.json"
        text_name = text_path.name
        previous_entry = previous_by_name.get(text_name, {})

        if not staged_audio.is_file():
            raise FileNotFoundError(f"Staged audio file is missing: {staged_audio}")
        if not staged_alignment.is_file():
            raise FileNotFoundError(
                f"Staged alignment file is missing: {staged_alignment}"
            )
        os.replace(staged_audio, target_audio)
        os.replace(staged_alignment, target_alignment)

        entry = dict(item)
        entry["audio_file"] = str(target_audio)
        entry["audio_sha256"] = sha256_file(target_audio)
        entry["alignment_file"] = str(target_alignment)
        entry["alignment_sha256"] = sha256_file(target_alignment)
        entry["alignment_method"] = str(
            item.get("alignment_method", "tts_with_timestamps")
        )
        entry["alignment_status"] = "generated"
        updated_entries.append(entry)

    combined_output = audio_dir / f"{project_name(video_number)}_voiceover.mp3"
    staged_preview = Path(str(staged.get("preview_file", "")))
    if staged_preview.is_file():
        shutil.copy2(staged_preview, combined_output)

    tts_info = {
        "schema_version": 2,
        "video": project_name(video_number),
        "created_at_utc": utc_now(),
        "voice_id": staged.get("voice_id", elevenlabs_tts.VOICE_ID),
        "voice_name": (
            staged.get("voice_name")
            or previous_info.get("voice_name", "")
        ),
        "model_id": staged.get("model_id", elevenlabs_tts.MODEL_ID),
        "output_format": staged.get("output_format", elevenlabs_tts.OUTPUT_FORMAT),
        "settings": staged.get("settings", elevenlabs_tts.tts_settings_payload()),
        "files": updated_entries,
    }
    atomic_write_json(audio_dir / "tts_info.json", tts_info)
    _remove_staging_dir(video_dir)

    _progress("Preparing pipeline rebuild from the voiceover stage...")
    stale_files = [
        video_dir / "story_timing.json",
        video_dir / "05_visuals" / "media_plan.json",
        video_dir / "06_captions" / "captions.json",
        video_dir / "06_captions" / "captions.srt",
        video_dir / "06_captions" / "captions.ass",
        video_dir / "07_effects" / "effects_plan.json",
        video_dir / "07_effects" / "milestone6_reference_60s.mp4",
        video_dir / "07_effects" / "milestone6_reference_60s.json",
        video_dir / "orchestration" / "preview_approval.json",
        video_dir / "08_render" / "render_plan.json",
        video_dir / "08_render" / "render_report.json",
    ]
    for stale_path in stale_files:
        stale_path.unlink(missing_ok=True)

    motion_dir = video_dir / "05_visuals" / "motion_graphics"
    if motion_dir.is_dir():
        for mp4_file in motion_dir.glob("*.mp4"):
            mp4_file.unlink(missing_ok=True)

    state_path = video_dir / "state.json"
    state = _load_optional_json(state_path)
    if state:
        stages = state.setdefault("stages", {})
        stages["tts"] = {
            "status": "COMPLETE",
            "updated_at_utc": utc_now(),
            "audio_count": len(updated_entries),
            "alignment_count": len(updated_entries),
        }
        for downstream_stage in (
            "alignment",
            "storyboard",
            "archive_visuals",
            "captions",
            "effects",
            "preview_approval",
            "render",
            "publish",
        ):
            if downstream_stage in stages:
                stages[downstream_stage] = {
                    "status": "NOT_STARTED",
                    "updated_at_utc": utc_now(),
                }
        state["updated_at_utc"] = utc_now()
        atomic_write_json(state_path, state)

    import news_studio  # noqa: E402

    news_studio._begin_rebuild(video_dir, "tts")

    _record_event(
        video_dir,
        {
            "type": "voiceover_reset_saved",
            "video_number": video_number,
            "story_count": len(updated_entries),
            "combined_voiceover_file": str(combined_output),
            "restart_stage": "tts",
        },
    )
    _progress(
        f"Voiceover saved for {project_name(video_number)}. "
        "Restarting pipeline from voiceover stage..."
    )
    return {
        "status": "SAVED",
        "video_number": video_number,
        "video_name": project_name(video_number),
        "combined_voiceover_file": str(combined_output),
        "story_count": len(updated_entries),
        "restart_stage": "tts",
    }


def discard_voiceover_reset(video_number: int) -> dict[str, Any]:
    if video_number < 1:
        raise ValueError("--video must be 1 or greater.")
    video_dir = project_dir(video_number)
    removed = _remove_staging_dir(video_dir)
    return {
        "status": "DISCARDED",
        "video_number": video_number,
        "removed": removed,
    }


def _find_latest_video_file(video_number: int, video_dir: Path, manifest: dict[str, Any]) -> Path:
    candidates: list[Path] = []
    manifest_video = str(manifest.get("video_file", "")).strip()
    if manifest_video:
        candidates.append(resolve_portable_path(manifest_video, video_dir=video_dir))

    report = _load_optional_json(video_dir / "08_render" / "render_report.json")
    report_video = str(report.get("final", {}).get("file", "")).strip()
    if report_video:
        candidates.append(resolve_portable_path(report_video, video_dir=video_dir))

    render_dir = video_dir / "08_render"
    candidates.append(render_dir / f"{project_name(video_number)}_final.mp4")
    if render_dir.is_dir():
        candidates.extend(sorted(render_dir.glob("*_final.mp4"), key=lambda p: p.stat().st_mtime, reverse=True))

    existing = [path.resolve() for path in candidates if path.is_file()]
    if not existing:
        raise FileNotFoundError(
            f"Final rendered video MP4 not found for {project_name(video_number)} in {render_dir}."
        )
    return max(existing, key=lambda p: p.stat().st_mtime)


def _find_latest_thumbnail_file(video_dir: Path, manifest: dict[str, Any]) -> Path:
    candidates: list[Path] = []
    manifest_thumb = str(manifest.get("thumbnail_file", "")).strip()
    if manifest_thumb:
        candidates.append(resolve_portable_path(manifest_thumb, video_dir=video_dir))
    publish_dir = video_dir / "09_publish"
    candidates.append(publish_dir / "thumbnail_final.png")
    candidates.append(publish_dir / "thumbnail_final.jpg")
    existing = [path.resolve() for path in candidates if path.is_file()]
    if not existing:
        raise FileNotFoundError(
            f"Final thumbnail not found for {video_dir.name} in {publish_dir}."
        )
    return max(existing, key=lambda p: p.stat().st_mtime)


def upload_project_again(video_number: int) -> dict[str, Any]:
    if video_number < 1:
        raise ValueError("--video must be 1 or greater.")
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")

    publish_dir = video_dir / "09_publish"
    manifest_path = publish_dir / "youtube_upload_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"YouTube upload manifest not found for {project_name(video_number)}: {manifest_path}"
        )

    _progress(f"Preparing latest video package for {project_name(video_number)}...")
    manifest = youtube_upload.load_manifest(manifest_path)
    latest_video = _find_latest_video_file(video_number, video_dir, manifest)
    latest_thumbnail = _find_latest_thumbnail_file(video_dir, manifest)

    manifest["video_file"] = str(latest_video)
    manifest["video_sha256"] = sha256_file(latest_video)
    manifest["thumbnail_file"] = str(latest_thumbnail)
    manifest["thumbnail_sha256"] = sha256_file(latest_thumbnail)

    title_path = publish_dir / "title.txt"
    if title_path.is_file():
        title_text = title_path.read_text(encoding="utf-8").strip()
        if title_text:
            manifest.setdefault("metadata", {}).setdefault("snippet", {})["title"] = title_text

    description_path = publish_dir / "description.txt"
    if description_path.is_file():
        description_text = description_path.read_text(encoding="utf-8").strip()
        if description_text:
            manifest.setdefault("metadata", {}).setdefault("snippet", {})[
                "description"
            ] = description_text

    atomic_write_json(manifest_path, manifest)

    # Clear any stale resume session so "Upload to YouTube again" always starts a clean upload.
    resume_path = publish_dir / "youtube_upload_resume.json"
    resume_path.unlink(missing_ok=True)

    _record_event(
        video_dir,
        {
            "type": "youtube_upload_started",
            "video_number": video_number,
            "reupload": True,
            "video_file": str(latest_video),
        },
    )
    _progress(f"Uploading {project_name(video_number)} to YouTube...")
    settings = youtube_upload.YouTubeUploadSettings.from_environment()
    result = youtube_upload.upload_from_manifest(
        manifest_path,
        settings,
        confirm_upload=True,
        force_new_upload=True,
    )
    _record_event(
        video_dir,
        {
            "type": "youtube_upload_completed",
            "video_number": video_number,
            "reupload": True,
            "video_id": result.get("video_id", ""),
            "watch_url": result.get("watch_url", ""),
            "scheduled_publish_at": result.get("scheduled_publish_at", ""),
        },
    )
    _progress(f"YouTube upload complete: {result.get('watch_url', '')}")
    return result


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute post-production actions (voiceover reset and YouTube re-upload)."
    )
    parser.add_argument(
        "action",
        choices=(
            "generate-voiceover",
            "save-voiceover",
            "discard-voiceover",
            "upload-again",
        ),
    )
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    stdout_channel = sys.__stdout__
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if args.action == "generate-voiceover":
                result = generate_voiceover_preview(args.video)
            elif args.action == "save-voiceover":
                result = save_voiceover_reset(args.video)
            elif args.action == "discard-voiceover":
                result = discard_voiceover_reset(args.video)
            elif args.action == "upload-again":
                result = upload_project_again(args.video)
            else:
                raise ValueError(f"Unsupported action: {args.action}")

        if args.json:
            print(
                json.dumps(result, ensure_ascii=False, indent=2),
                file=stdout_channel,
                flush=True,
            )
        else:
            for key, value in result.items():
                print(f"{key}: {value}", file=stdout_channel, flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        report_cli_error("PROJECT ACTION", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
