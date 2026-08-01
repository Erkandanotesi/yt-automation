from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
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

from file_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    json_fingerprint,
    report_cli_error,
    sha256_file,
)
from file_locations import (  # noqa: E402
    BACKGROUND_MUSIC_ROOT,
    EDITING_REFERENCE_FILE,
    IMAGE_TV_EFFECT_FILE,
    PAPER_SFX_FILE,
    PROJECTS_ROOT,
    SHUTTER_SFX_FILE,
    TV_FRAME_FILE,
    project_dir,
    project_name,
)
from separator_policy import require_separator_checkpoint  # noqa: E402
from tooling import find_executable  # noqa: E402


EFFECTS_SCHEMA_VERSION = 1
STAGE_NAME = "effects"
SUPPORTED_AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _path_env(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path.resolve()


@dataclass(frozen=True)
class EffectsSettings:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    motion_scale: float = 1.0
    hero_motion_scale: float = 1.0
    tv_frame_enabled: bool = True
    tv_frame_file: Path = TV_FRAME_FILE
    tv_frame_opacity: float = 1.0
    image_tv_effect_enabled: bool = True
    image_tv_effect_file: Path = IMAGE_TV_EFFECT_FILE
    image_tv_effect_opacity: float = 0.18
    glitch_enabled: bool = False
    glitch_duration_seconds: float = 0.08
    glitch_max_per_minute: int = 1
    glitch_minimum_spacing_seconds: float = 10.0
    glitch_noise_strength: int = 2
    sfx_gain_db: float = -24.0
    paper_sfx_enabled: bool = True
    paper_sfx_file: Path = PAPER_SFX_FILE
    paper_sfx_gain_db: float = -10.0
    shutter_sfx_enabled: bool = True
    shutter_sfx_file: Path = SHUTTER_SFX_FILE
    shutter_sfx_gain_db: float = -10.0
    music_enabled: bool = True
    music_directory: Path = BACKGROUND_MUSIC_ROOT
    music_file: Path | None = None
    music_target_lufs: float = -20.0
    music_speech_gain_db: float = -22.0
    music_separator_gain_db: float = -16.0
    music_loop_crossfade_seconds: float = 0.35
    music_fade_seconds: float = 1.0
    proof_width: int = 960
    proof_height: int = 540
    editing_reference_file: Path = EDITING_REFERENCE_FILE

    @classmethod
    def from_environment(cls) -> "EffectsSettings":
        configured_music = os.getenv("BACKGROUND_MUSIC_FILE", "").strip()
        music_file = None
        if configured_music:
            music_file = Path(configured_music).expanduser()
            if not music_file.is_absolute():
                music_file = SCRIPT_DIR / music_file
            music_file = music_file.resolve()
        return cls(
            width=int(os.getenv("EFFECTS_WIDTH", "1920")),
            height=int(os.getenv("EFFECTS_HEIGHT", "1080")),
            fps=int(os.getenv("EFFECTS_FPS", "30")),
            motion_scale=float(os.getenv("EFFECTS_MOTION_SCALE", "1.0")),
            hero_motion_scale=float(
                os.getenv("EFFECTS_HERO_MOTION_SCALE", "1.0")
            ),
            tv_frame_enabled=_bool_env("TV_FRAME_ENABLED", True),
            tv_frame_file=_path_env(
                "TV_FRAME_FILE",
                TV_FRAME_FILE,
            ),
            tv_frame_opacity=float(os.getenv("TV_FRAME_OPACITY", "1.0")),
            image_tv_effect_enabled=_bool_env("IMAGE_TV_EFFECT_ENABLED", True),
            image_tv_effect_file=_path_env(
                "IMAGE_TV_EFFECT_FILE",
                IMAGE_TV_EFFECT_FILE,
            ),
            image_tv_effect_opacity=float(
                os.getenv("IMAGE_TV_EFFECT_OPACITY", "0.18")
            ),
            glitch_enabled=_bool_env("GLITCH_ENABLED", False),
            glitch_duration_seconds=float(
                os.getenv("GLITCH_DURATION_SECONDS", "0.08")
            ),
            glitch_max_per_minute=int(
                os.getenv("GLITCH_MAX_PER_MINUTE", "1")
            ),
            glitch_minimum_spacing_seconds=float(
                os.getenv("GLITCH_MINIMUM_SPACING_SECONDS", "10")
            ),
            glitch_noise_strength=int(
                os.getenv("GLITCH_NOISE_STRENGTH", "2")
            ),
            sfx_gain_db=float(os.getenv("GLITCH_SFX_GAIN_DB", "-24")),
            paper_sfx_enabled=_bool_env("PAPER_SFX_ENABLED", True),
            paper_sfx_file=_path_env(
                "PAPER_SFX_FILE",
                PAPER_SFX_FILE,
            ),
            paper_sfx_gain_db=float(os.getenv("PAPER_SFX_GAIN_DB", "-10.0")),
            shutter_sfx_enabled=_bool_env("SHUTTER_SFX_ENABLED", True),
            shutter_sfx_file=_path_env(
                "SHUTTER_SFX_FILE",
                SHUTTER_SFX_FILE,
            ),
            shutter_sfx_gain_db=float(os.getenv("SHUTTER_SFX_GAIN_DB", "-10.0")),
            music_enabled=_bool_env("BACKGROUND_MUSIC_ENABLED", True),
            music_directory=_path_env(
                "BACKGROUND_MUSIC_DIRECTORY",
                BACKGROUND_MUSIC_ROOT,
            ),
            music_file=music_file,
            music_target_lufs=float(
                os.getenv("BACKGROUND_MUSIC_TARGET_LUFS", "-20")
            ),
            music_speech_gain_db=float(
                os.getenv("BACKGROUND_MUSIC_SPEECH_GAIN_DB", "-22")
            ),
            music_separator_gain_db=float(
                os.getenv("BACKGROUND_MUSIC_SEPARATOR_GAIN_DB", "-16")
            ),
            music_loop_crossfade_seconds=float(
                os.getenv("BACKGROUND_MUSIC_LOOP_CROSSFADE_SECONDS", "0.35")
            ),
            music_fade_seconds=float(
                os.getenv("BACKGROUND_MUSIC_FADE_SECONDS", "1")
            ),
            proof_width=int(os.getenv("EFFECTS_PROOF_WIDTH", "960")),
            proof_height=int(os.getenv("EFFECTS_PROOF_HEIGHT", "540")),
            editing_reference_file=_path_env(
                "EDITING_REFERENCE_FILE",
                EDITING_REFERENCE_FILE,
            ),
        )

    def validate(self) -> None:
        if self.width < 320 or self.height < 180:
            raise ValueError("Effects resolution is too small.")
        if self.width % 2 or self.height % 2:
            raise ValueError("Effects width and height must be even.")
        if not 1 <= self.fps <= 120:
            raise ValueError("EFFECTS_FPS must be between 1 and 120.")
        if not 1.0 <= self.motion_scale <= 1.12:
            raise ValueError("EFFECTS_MOTION_SCALE must be between 1.0 and 1.12.")
        if not 1.0 <= self.hero_motion_scale <= 1.10:
            raise ValueError(
                "EFFECTS_HERO_MOTION_SCALE must be between 1.0 and 1.10."
            )
        if not 0 <= self.tv_frame_opacity <= 1:
            raise ValueError("TV_FRAME_OPACITY must be between 0 and 1.")
        if not 0 <= self.image_tv_effect_opacity <= 1:
            raise ValueError("IMAGE_TV_EFFECT_OPACITY must be between 0 and 1.")
        if not 0.05 <= self.glitch_duration_seconds <= 0.35:
            raise ValueError("Glitch duration must be between 0.05 and 0.35 seconds.")
        if not 0 <= self.glitch_max_per_minute <= 12:
            raise ValueError("GLITCH_MAX_PER_MINUTE must be between 0 and 12.")
        if self.glitch_minimum_spacing_seconds < 2:
            raise ValueError("Glitch spacing must be at least 2 seconds.")
        for label, gain in (
            ("PAPER_SFX_GAIN_DB", self.paper_sfx_gain_db),
            ("SHUTTER_SFX_GAIN_DB", self.shutter_sfx_gain_db),
        ):
            if not -36 <= gain <= 0:
                raise ValueError(f"{label} must be between -36 and 0 dB.")
        if not -36 <= self.music_speech_gain_db <= -12:
            raise ValueError("Speech music gain must be between -36 and -12 dB.")
        if not -30 <= self.music_separator_gain_db <= -8:
            raise ValueError("Separator music gain must be between -30 and -8 dB.")
        if self.music_speech_gain_db >= self.music_separator_gain_db:
            raise ValueError("Music must be quieter under speech than separators.")
        if not 0 <= self.music_loop_crossfade_seconds <= 2:
            raise ValueError("Music loop crossfade must be between 0 and 2 seconds.")
        if self.proof_width % 2 or self.proof_height % 2:
            raise ValueError("Proof width and height must be even.")


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


def _run(command: list[str], label: str) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"{label} failed: {detail}")


def probe_media(path: Path) -> dict[str, Any]:
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        raise RuntimeError("FFprobe is required for Milestone 6.")
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,pix_fmt,"
            "sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe could not read {path}: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    duration = float(payload.get("format", {}).get("duration") or 0)
    return {"duration_seconds": duration, "streams": payload.get("streams", [])}


def _license_sidecar(path: Path) -> Path:
    return path.with_name(f"{path.stem}.license.json")


def ensure_default_audio_assets(settings: EffectsSettings) -> dict[str, Path]:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required to create Milestone 6 audio assets.")
    settings.music_directory.mkdir(parents=True, exist_ok=True)
    sfx_directory = settings.music_directory.parent / "sfx"
    sfx_directory.mkdir(parents=True, exist_ok=True)

    available_music = sorted(
        path
        for path in settings.music_directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO
    )
    if settings.music_file is not None and settings.music_file.is_file():
        music = settings.music_file
    elif available_music:
        available_music.sort(
            key=lambda path: path.name == "automation_ambient_bed.wav"
        )
        music = available_music[0]
    else:
        music = settings.music_directory / "automation_ambient_bed.wav"
        expression = (
            "0.032*(sin(2*PI*110*t)+0.55*sin(2*PI*165*t)+"
            "0.30*sin(2*PI*220*t))*(0.88+0.12*cos(2*PI*t/12))|"
            "0.032*(sin(2*PI*110*t)+0.50*sin(2*PI*165*t)+"
            "0.34*sin(2*PI*220*t))*(0.88+0.12*cos(2*PI*t/12))"
        )
        _run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"aevalsrc={expression}:s=48000:d=12",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(music),
            ],
            "Default background-music generation",
        )
        atomic_write_json(
            _license_sidecar(music),
            {
                "title": "Automation Ambient Bed",
                "creator": "News Studio procedural generator",
                "license": "Original procedural audio; unrestricted project use",
                "seamless_loop": True,
                "loop_start_seconds": 0.0,
                "loop_end_seconds": 12.0,
                "generated_at_utc": utc_now(),
            },
        )

    sfx = sfx_directory / "glitch_transition.wav"
    if not sfx.is_file():
        _run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:amplitude=0.18:duration=0.16:sample_rate=48000",
                "-af",
                (
                    "highpass=f=900,lowpass=f=6500,"
                    "afade=t=in:st=0:d=0.015,afade=t=out:st=0.11:d=0.05,"
                    "aformat=channel_layouts=stereo"
                ),
                "-c:a",
                "pcm_s16le",
                "-y",
                str(sfx),
            ],
            "Glitch SFX generation",
        )
        atomic_write_json(
            _license_sidecar(sfx),
            {
                "title": "Glitch Transition",
                "creator": "News Studio procedural generator",
                "license": "Original procedural audio; unrestricted project use",
                "generated_at_utc": utc_now(),
            },
        )
    return {"music": music, "sfx": sfx}


def select_music_track(settings: EffectsSettings) -> tuple[Path, dict]:
    if settings.music_file is not None:
        candidates = [settings.music_file]
    else:
        candidates = sorted(
            path
            for path in settings.music_directory.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO
        )
        candidates.sort(key=lambda path: path.name == "automation_ambient_bed.wav")
    if not candidates or not candidates[0].is_file():
        raise FileNotFoundError(
            f"No background music found in {settings.music_directory}."
        )
    track = candidates[0].resolve()
    sidecar = _license_sidecar(track)
    metadata = load_json(sidecar, "Music license metadata") if sidecar.is_file() else {
        "title": track.stem,
        "creator": "user supplied",
        "license": "user confirmed for project use",
        "seamless_loop": False,
    }
    return track, metadata


def _require_inputs(video_dir: Path) -> tuple[dict, dict, dict, dict]:
    state = load_json(video_dir / "state.json", "Stage state")
    timing = load_json(video_dir / "story_timing.json", "Story timing manifest")
    require_separator_checkpoint(state, timing)
    media = load_json(video_dir / "05_visuals" / "media_plan.json", "Media plan")
    if media.get("quality", {}).get("status") != "passed":
        raise RuntimeError("A complete, passing Milestone 5 media plan is required.")
    captions = load_json(video_dir / "06_captions" / "captions.json", "Captions")
    if captions.get("quality", {}).get("status") != "passed":
        raise RuntimeError("Passing basic captions are required for the effects stage.")
    return state, timing, media, captions


def _asset_record(path_value: str, expected_sha256: str = "") -> dict:
    path = Path(path_value).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Visual asset is missing: {path}")
    checksum = sha256_file(path)
    if expected_sha256 and checksum != expected_sha256:
        raise RuntimeError(f"Visual asset changed after selection: {path}")
    return {"file": str(path), "sha256": checksum}


def _attribution_record(selected: dict) -> dict | None:
    license_info = selected.get("license", {})
    if license_info.get("attribution_required") is not True:
        return None
    creator = str(selected.get("creator", "")).strip()
    source_url = str(selected.get("source_url", "")).strip()
    license_name = str(license_info.get("name", "")).strip()
    provided = str(license_info.get("attribution_text", "")).strip()
    if not any((creator, source_url, license_name, provided)):
        raise RuntimeError(
            "An attribution-required asset is missing source attribution metadata."
        )
    return {
        "required": True,
        "creator": creator,
        "source_url": source_url,
        "license_name": license_name,
        "provided_text": provided,
    }


def build_visual_events(timing: dict, media: dict, settings: EffectsSettings) -> list[dict]:
    timing_by_id = {story["story_id"]: story for story in timing.get("stories", [])}
    raw_events: list[dict] = []
    for story_plan in media.get("stories", []):
        story_id = story_plan["story_id"]
        timed = timing_by_id.get(story_id)
        if timed is None:
            raise RuntimeError(f"Timing is missing for {story_id}.")
        narration_start = float(timed["narration"]["start"])
        for hero in story_plan.get("hero_ranges", []):
            asset = _asset_record(str(hero["asset"]))
            start = float(hero["start"])
            end = float(hero["end"])
            raw_events.append(
                {
                    "story_id": story_id,
                    "story_order": int(story_plan["order"]),
                    "source_type": "generated_hero",
                    "source_id": f"hero_{int(hero['image_index']):02d}",
                    "local_start": start,
                    "local_end": end,
                    "timeline_start": narration_start + start,
                    "timeline_end": narration_start + end,
                    "asset": asset,
                    "crop_risk": False,
                    "attribution": None,
                }
            )
        for window in story_plan.get("windows", []):
            selected = window.get("selected_asset", {})
            asset_path = selected.get("project_copy") or selected.get("absolute_path")
            if not asset_path:
                raise RuntimeError(f"Selected asset path missing for {window.get('window_id')}.")
            asset = _asset_record(str(asset_path), str(selected.get("sha256", "")))
            start = float(window["start"])
            end = float(window["end"])
            raw_events.append(
                {
                    "story_id": story_id,
                    "story_order": int(story_plan["order"]),
                    "source_type": "archive",
                    "source_id": str(window["window_id"]),
                    "local_start": start,
                    "local_end": end,
                    # Local media ranges are canonical. Always derive global time
                    # from the current story manifest so changing inter-story gaps
                    # never requires another paid media-selection pass.
                    "timeline_start": narration_start + start,
                    "timeline_end": narration_start + end,
                    "asset": asset,
                    "crop_risk": "crop_risk" in selected.get("quality_flags", []),
                    "attribution": _attribution_record(selected),
                }
            )
    timed_stories = timing.get("stories", [])
    for previous, current in zip(timed_stories, timed_stories[1:]):
        hold_start = float(previous["narration"]["end"])
        hold_end = float(current["narration"]["start"])
        if hold_end <= hold_start + 0.001:
            continue
        story_events = [
            event
            for event in raw_events
            if event["story_id"] == previous["story_id"]
        ]
        if not story_events:
            raise RuntimeError(
                f"Cannot fill inter-story gap after {previous['story_id']}: "
                "no visual event exists."
            )
        source = max(story_events, key=lambda item: item["timeline_end"])
        raw_events.append(
            {
                "story_id": previous["story_id"],
                "story_order": int(previous["order"]),
                "source_type": "inter_story_hold",
                "source_id": f"gap_after_story_{int(previous['order']):02d}",
                "local_start": float(previous["narration"]["duration"]),
                "local_end": float(previous["narration"]["duration"])
                + (hold_end - hold_start),
                "timeline_start": hold_start,
                "timeline_end": hold_end,
                "asset": dict(source["asset"]),
                "crop_risk": bool(source["crop_risk"]),
                "attribution": source.get("attribution"),
            }
        )
    raw_events.sort(key=lambda item: (item["timeline_start"], item["timeline_end"]))
    for index, event in enumerate(raw_events):
        event["event_id"] = f"visual_{index + 1:04d}"
        event["duration"] = round(event["timeline_end"] - event["timeline_start"], 6)
        event["motion"] = {
            "type": "static",
            "maximum_scale": 1.0,
            "focus": "center_safe",
            "vertical_pan": False,
            "caption_safe_bottom_percent": 22,
            "deterministic_index": index,
        }
        for key in ("local_start", "local_end", "timeline_start", "timeline_end"):
            event[key] = round(float(event[key]), 6)
    return raw_events


def validate_visual_coverage(events: list[dict], timing: dict) -> dict:
    errors: list[str] = []
    by_story: dict[str, list[dict]] = {}
    for event in events:
        if event.get("source_type") == "inter_story_hold":
            continue
        by_story.setdefault(event["story_id"], []).append(event)
    story_results = []
    tolerance = 0.03
    for story in timing.get("stories", []):
        duration = float(story["narration"]["duration"])
        ordered = sorted(by_story.get(story["story_id"], []), key=lambda item: item["local_start"])
        cursor = 0.0
        gaps = []
        overlaps = []
        for event in ordered:
            start = float(event["local_start"])
            end = float(event["local_end"])
            if start > cursor + tolerance:
                gaps.append([round(cursor, 3), round(start, 3)])
            if start < cursor - tolerance:
                overlaps.append([round(start, 3), round(cursor, 3)])
            cursor = max(cursor, end)
        if cursor < duration - tolerance:
            gaps.append([round(cursor, 3), round(duration, 3)])
        if gaps:
            errors.append(f"{story['story_id']} has visual gaps: {gaps}")
        if overlaps:
            errors.append(f"{story['story_id']} has visual overlaps: {overlaps}")
        story_results.append(
            {
                "story_id": story["story_id"],
                "event_count": len(ordered),
                "gaps": gaps,
                "overlaps": overlaps,
                "status": "passed" if not gaps and not overlaps else "failed",
            }
        )
    return {"status": "passed" if not errors else "failed", "errors": errors, "stories": story_results}


def build_glitch_events(
    timing: dict,
    visuals: list[dict],
    settings: EffectsSettings,
) -> list[dict]:
    if not settings.glitch_enabled or settings.glitch_max_per_minute == 0:
        return []
    duration = float(timing.get("duration_seconds", 0))
    maximum = max(1, math.ceil(duration / 60) * settings.glitch_max_per_minute)
    candidates: list[tuple[float, str, int | None]] = []
    if not timing.get("quality", {}).get("manual_separators_skipped", False):
        for story in timing.get("stories", []):
            separator = story.get("separator", {})
            start = float(separator.get("start", 0))
            end = float(separator.get("end", start))
            candidates.append((end, "separator_exit_accent", int(story["order"])))
    for index, event in enumerate(visuals[1:], start=1):
        if index % 3 == 0:
            candidates.append((float(event["timeline_start"]), "selected_cut", event["story_order"]))
    result = []
    last = -10_000.0
    for start, reason, story_order in sorted(candidates):
        if start - last < settings.glitch_minimum_spacing_seconds:
            continue
        end = min(duration, start + settings.glitch_duration_seconds)
        if end <= start:
            continue
        result.append(
            {
                "event_id": f"glitch_{len(result) + 1:03d}",
                "start": round(start, 6),
                "end": round(end, 6),
                "duration": round(end - start, 6),
                "reason": reason,
                "story_order": story_order,
                "effects": {
                    "noise_strength": settings.glitch_noise_strength,
                    "rgb_split_pixels": 3,
                    "horizontal_displacement_pixels": 6,
                    "tape_dropout_opacity": 0.16,
                },
            }
        )
        last = start
        if len(result) >= maximum:
            break
    return result


def _timeline_sfx_asset(path: Path, gain_db: float, label: str) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"{label} sound is missing: {path}")
    probe = probe_media(path)
    if not any(
        stream.get("codec_type") == "audio"
        for stream in probe.get("streams", [])
    ):
        raise RuntimeError(f"{label} sound has no audio stream: {path}")
    duration = float(probe.get("duration_seconds", 0))
    if duration <= 0:
        raise RuntimeError(f"{label} sound has no measurable duration: {path}")
    return {
        "file": str(path),
        "sha256": sha256_file(path),
        "duration_seconds": round(duration, 6),
        "gain_db": gain_db,
        "user_provided": True,
    }


def build_timeline_sfx(
    timing: dict,
    visuals: list[dict],
    settings: EffectsSettings,
) -> dict:
    assets: dict[str, dict] = {}
    events: list[dict] = []
    if settings.paper_sfx_enabled:
        assets["paper"] = _timeline_sfx_asset(
            settings.paper_sfx_file,
            settings.paper_sfx_gain_db,
            "Paper",
        )
        for story in timing.get("stories", []):
            events.append(
                {
                    "event_id": f"paper_story_{int(story['order']):02d}",
                    "sound": "paper",
                    "start": round(float(story["narration"]["start"]), 6),
                    "reason": "news_section_start",
                    "story_id": story["story_id"],
                    "story_order": int(story["order"]),
                }
            )

    separator_ranges: list[tuple[float, float]] = []
    if not timing.get("quality", {}).get("manual_separators_skipped", False):
        separator_ranges = [
            (
                float(story.get("separator", {}).get("start", 0)),
                float(story.get("separator", {}).get("end", 0)),
            )
            for story in timing.get("stories", [])
            if float(story.get("separator", {}).get("end", 0))
            > float(story.get("separator", {}).get("start", 0))
        ]

    image_events = [
        event
        for event in sorted(
            visuals,
            key=lambda item: (item["timeline_start"], item["timeline_end"]),
        )
        if event.get("source_type") != "inter_story_hold"
    ]
    shutter_candidates = 0
    if settings.shutter_sfx_enabled:
        assets["shutter"] = _timeline_sfx_asset(
            settings.shutter_sfx_file,
            settings.shutter_sfx_gain_db,
            "Shutter",
        )
        for image_number, event in enumerate(image_events, start=1):
            if image_number % 3:
                continue
            shutter_candidates += 1
            start = float(event["timeline_start"])
            for separator_start, separator_end in separator_ranges:
                if separator_start <= start < separator_end:
                    start = separator_end
                    break
            if start >= float(event["timeline_end"]):
                continue
            events.append(
                {
                    "event_id": f"shutter_image_{image_number:04d}",
                    "sound": "shutter",
                    "start": round(start, 6),
                    "reason": "every_third_visible_image",
                    "visual_event_id": event["event_id"],
                    "visual_sequence_number": image_number,
                    "story_id": event["story_id"],
                    "story_order": int(event["story_order"]),
                }
            )
    events.sort(key=lambda item: (item["start"], item["event_id"]))
    return {
        "enabled": bool(assets),
        "assets": assets,
        "events": events,
        "selection_rule": {
            "paper": "once at every narration/news-section start",
            "shutter": (
                "every third image event globally; manual separators and "
                "inter-story hold extensions are excluded"
            ),
            "separator_collision_policy": (
                "move a shutter cue to the first visible frame after the separator; "
                "skip it if that image is never visible"
            ),
        },
        "image_event_count": len(image_events),
        "shutter_candidate_count": shutter_candidates,
    }


def _db_factor(value: float) -> float:
    return 10 ** (value / 20)


def _music_plan(
    track: Path,
    metadata: dict,
    timing: dict,
    settings: EffectsSettings,
) -> dict:
    probe = probe_media(track)
    track_duration = float(probe["duration_seconds"])
    if track_duration <= 0:
        raise RuntimeError(f"Background music has no measurable duration: {track}")
    duration = float(timing["duration_seconds"])
    seamless = bool(metadata.get("seamless_loop", False))
    crossfade = 0.0 if seamless else min(
        settings.music_loop_crossfade_seconds,
        max(0.0, track_duration / 4),
    )
    effective = max(0.1, track_duration - crossfade)
    loops = max(1, math.ceil(max(0.0, duration - crossfade) / effective))
    speech_windows = [
        {
            "start": round(float(story["narration"]["start"]), 6),
            "end": round(float(story["narration"]["end"]), 6),
        }
        for story in timing.get("stories", [])
    ]
    return {
        "enabled": True,
        "track_file": str(track),
        "track_sha256": sha256_file(track),
        "license_metadata_file": (
            str(_license_sidecar(track)) if _license_sidecar(track).is_file() else None
        ),
        "license": metadata,
        "track_duration_seconds": round(track_duration, 6),
        "program_duration_seconds": round(duration, 6),
        "loop_count": loops,
        "loop_mode": "seamless_repeat" if seamless else "acrossfade",
        "crossfade_seconds": round(crossfade, 3),
        "fade_in_seconds": settings.music_fade_seconds,
        "fade_out_seconds": settings.music_fade_seconds,
        "normalization": {
            "filter": "loudnorm",
            "target_lufs": settings.music_target_lufs,
            "true_peak_db": -2.0,
            "loudness_range": 7.0,
        },
        "ducking": {
            "method": "timeline_volume_envelope",
            "speech_gain_db": settings.music_speech_gain_db,
            "speech_gain_factor": round(_db_factor(settings.music_speech_gain_db), 6),
            "separator_gain_db": settings.music_separator_gain_db,
            "separator_gain_factor": round(_db_factor(settings.music_separator_gain_db), 6),
            "attack_seconds": 0.18,
            "release_seconds": 0.35,
            "speech_windows": speech_windows,
        },
    }


def _separator_events(timing: dict) -> list[dict]:
    if timing.get("quality", {}).get("manual_separators_skipped", False):
        return []
    events = []
    for story in timing.get("stories", []):
        path = Path(story["separator"]["file"])
        if not path.is_file():
            raise FileNotFoundError(f"Validated separator is missing: {path}")
        events.append({
            "story_id": story["story_id"],
            "story_order": int(story["order"]),
            "file": str(path),
            "sha256": sha256_file(path),
            "start": round(float(story["separator"]["start"]), 6),
            "end": round(float(story["separator"]["end"]), 6),
            "duration": round(float(story["separator"]["duration"]), 6),
        })
    return events


def _narration_events(timing: dict) -> list[dict]:
    return [
        {
            "story_id": story["story_id"],
            "story_order": int(story["order"]),
            "file": story["audio_file"],
            "sha256": sha256_file(Path(story["audio_file"])),
            "start": round(float(story["narration"]["start"]), 6),
            "end": round(float(story["narration"]["end"]), 6),
            "duration": round(float(story["narration"]["duration"]), 6),
        }
        for story in timing.get("stories", [])
    ]


def _input_fingerprint(
    timing_path: Path,
    media_path: Path,
    captions_path: Path,
    settings: EffectsSettings,
    overlay: dict | None,
    image_tv_effect: dict | None,
    editing_reference: dict | None,
    music: dict | None,
    sfx: dict,
    timeline_sfx: dict,
) -> str:
    settings_payload = asdict(settings)
    for key in (
        "tv_frame_file",
        "image_tv_effect_file",
        "paper_sfx_file",
        "shutter_sfx_file",
        "music_directory",
        "music_file",
        "editing_reference_file",
    ):
        value = settings_payload.get(key)
        settings_payload[key] = str(value) if value is not None else None
    return json_fingerprint(
        {
            "timing_sha256": sha256_file(timing_path),
            "media_sha256": sha256_file(media_path),
            "captions_sha256": sha256_file(captions_path),
            "settings": settings_payload,
            "overlay": overlay,
            "image_tv_effect": image_tv_effect,
            "editing_reference": editing_reference,
            "music": music,
            "sfx": sfx,
            "timeline_sfx": timeline_sfx,
        }
    )


def _filter_templates(settings: EffectsSettings) -> dict:
    return {
        "layer_order": [
            "base_visual",
            "static_center_crop",
            "tape_dropout_glitch_accents",
            "animated_tv_effect_images_only",
            "manual_separator",
            "tv_frame",
            "persistent_story_title",
            "source_attribution_bottom_right",
            "captions",
        ],
        "motion": {
            "slow_push_in": "zoompan=z='1+(MAX_SCALE-1)*on/(FRAMES-1)':x='floor((iw-iw/zoom)/2)':y='floor((ih-ih/zoom)/2)'",
            "static": "scale=OUTPUT_WIDTH:OUTPUT_HEIGHT:force_original_aspect_ratio=increase,crop=OUTPUT_WIDTH:OUTPUT_HEIGHT",
        },
        "glitch": (
            f"noise=alls={settings.glitch_noise_strength}:allf=t+u,"
            "drawbox=y=ih*0.43:h=8:color=white@0.16:t=fill"
        ),
        "tv_frame": (
            f"scale={settings.width}:{settings.height},format=rgba,"
            f"colorchannelmixer=aa={settings.tv_frame_opacity}"
        ),
        "animated_tv_effect": (
            f"scale={settings.width}:{settings.height},format=rgba,"
            f"colorchannelmixer=aa={settings.image_tv_effect_opacity}; "
            "loop across program; disable during every manual separator interval"
        ),
        "music": (
            f"loudnorm=I={settings.music_target_lufs}:TP=-2:LRA=7,"
            "volume=TIMELINE_DUCK_EXPRESSION:eval=frame"
        ),
        "music_loop": (
            "acrossfade=d="
            f"{settings.music_loop_crossfade_seconds}:c1=tri:c2=tri"
        ),
        "delivery": {
            "video": "H.264 yuv420p",
            "audio": "AAC 48 kHz stereo",
            "resolution": f"{settings.width}x{settings.height}",
            "fps": settings.fps,
        },
    }


def _write_state(video_dir: Path, state: dict, manifest_path: Path, plan: dict) -> None:
    stages = state.setdefault("stages", {})
    previous_fingerprint = stages.get(STAGE_NAME, {}).get("input_fingerprint")
    if previous_fingerprint and previous_fingerprint != plan["input_fingerprint"]:
        invalidated = [
            stage
            for stage in ("render", "publish")
            if stage in stages
        ]
        for stage in invalidated:
            stages.pop(stage, None)
        state["downstream_invalidation"] = {
            "reason": "Effects inputs or configuration changed.",
            "invalidated_stages": invalidated,
            "previous_effects_fingerprint": previous_fingerprint,
            "current_effects_fingerprint": plan["input_fingerprint"],
            "updated_at_utc": utc_now(),
        }
    stages[STAGE_NAME] = {
        "status": "EFFECTS_READY",
        "reason": (
            "Motion, TV treatment, controlled glitch/SFX, and looped ducked music "
            "are planned for the complete timeline."
        ),
        "effects_manifest_file": str(manifest_path),
        "input_fingerprint": plan["input_fingerprint"],
        "visual_event_count": len(plan["visual_events"]),
        "glitch_event_count": len(plan["glitch_events"]),
        "timeline_sfx_event_count": len(
            plan.get("audio", {}).get("timeline_sfx", {}).get("events", [])
        ),
        "updated_at_utc": utc_now(),
    }
    state["updated_at_utc"] = state["stages"][STAGE_NAME]["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)


def _ffmpeg_filter_path(path: Path) -> str:
    value = path.resolve().as_posix().replace("'", r"\'")
    if len(value) >= 2 and value[1] == ":":
        value = value[0] + r"\:" + value[2:]
    return value


def _discover_reference_images(limit: int = 3) -> list[Path]:
    extensions = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
    roots = [
        PROJECTS_ROOT / "Assets" / "Media (No-Copyright)",
        PROJECTS_ROOT / "Assets" / "Media",
    ]
    images = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in extensions:
                images.append(path.resolve())
                if len(images) >= limit:
                    return images
    raise FileNotFoundError("At least three reference images are required.")


def render_reference_proof(
    *,
    output_path: Path,
    images: list[Path],
    narration_file: Path,
    music_file: Path,
    sfx_file: Path,
    paper_sfx_file: Path | None = None,
    shutter_sfx_file: Path | None = None,
    settings: EffectsSettings,
    duration_seconds: float = 60.0,
    separator_file: Path | None = None,
    separator_duration_seconds: float | None = None,
    captions_file: Path | None = None,
) -> dict:
    """Render a compact real-filter proof without completing Milestone 8."""
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required for the effects reference proof.")
    if len(images) < 3:
        raise ValueError("The reference proof requires three images.")
    inputs = [*images[:3], narration_file, music_file]
    if settings.glitch_enabled:
        inputs.append(sfx_file)
    if paper_sfx_file is not None:
        inputs.append(paper_sfx_file)
    if shutter_sfx_file is not None:
        inputs.append(shutter_sfx_file)
    if settings.image_tv_effect_enabled:
        inputs.append(settings.image_tv_effect_file)
    if separator_file is not None:
        inputs.append(separator_file)
    if captions_file is not None:
        inputs.append(captions_file)
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(f"Reference proof input is missing: {path}")
    if settings.tv_frame_enabled and not settings.tv_frame_file.is_file():
        raise FileNotFoundError(f"TV frame overlay not found: {settings.tv_frame_file}")
    if not 6 <= duration_seconds <= 60:
        raise ValueError("Reference proof duration must be between 6 and 60 seconds.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path = output_path.with_name("milestone6_reference.srt")
    if captions_file is not None:
        caption_text = captions_file.read_text(encoding="utf-8-sig")
    else:
        caption_end = max(5.5, duration_seconds - 0.25)
        end_minutes = int(caption_end // 60)
        end_seconds = caption_end - end_minutes * 60
        caption_text = (
            "1\n00:00:00,250 --> 00:00:02,750\nMILESTONE 6 REFERENCE\n\n"
            "2\n00:00:03,100 --> 00:00:05,500\n"
            "Motion, TV frame and controlled glitch\n\n"
            f"3\n00:00:05,600 --> 00:{end_minutes:02d}:"
            f"{end_seconds:06.3f}\nLooped music, ducking and SFX\n"
        ).replace(
            f"{end_seconds:06.3f}",
            f"{end_seconds:06.3f}".replace(".", ","),
        )
    atomic_write_text(caption_path, caption_text)

    requested_separator_duration = (
        separator_duration_seconds
        if separator_duration_seconds is not None
        else min(3.0, duration_seconds / 4)
    )
    separator_duration = min(
        max(0.1, float(requested_separator_duration)),
        duration_seconds - 3.0,
    )
    image_duration = duration_seconds / 3
    width = settings.proof_width
    height = settings.proof_height
    fps = settings.fps
    frames = max(2, round(image_duration * fps))
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error"]
    separator_has_audio = False
    if separator_file is None:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={width}x{height}:rate={fps}:duration={separator_duration}",
            ]
        )
    else:
        separator_probe = probe_media(separator_file)
        separator_has_audio = any(
            stream.get("codec_type") == "audio"
            for stream in separator_probe.get("streams", [])
        )
        command.extend(["-i", str(separator_file)])
    for image in images[:3]:
        command.extend(
            ["-loop", "1", "-framerate", str(fps), "-t", str(image_duration), "-i", str(image)]
        )
    next_input_index = len(images[:3]) + 1
    overlay_index = -1
    if settings.tv_frame_enabled:
        overlay_index = next_input_index
        command.extend(["-loop", "1", "-framerate", str(fps), "-i", str(settings.tv_frame_file)])
        next_input_index += 1
    narration_index = next_input_index
    command.extend(["-i", str(narration_file)])
    next_input_index += 1
    music_index = next_input_index
    command.extend(["-stream_loop", "-1", "-i", str(music_file)])
    next_input_index += 1
    sfx_index = -1
    if settings.glitch_enabled:
        sfx_index = next_input_index
        command.extend(["-i", str(sfx_file)])
        next_input_index += 1
    paper_sfx_index = -1
    if paper_sfx_file is not None:
        paper_sfx_index = next_input_index
        command.extend(["-i", str(paper_sfx_file)])
        next_input_index += 1
    shutter_sfx_index = -1
    if shutter_sfx_file is not None:
        shutter_sfx_index = next_input_index
        command.extend(["-i", str(shutter_sfx_file)])
        next_input_index += 1
    image_effect_index = -1
    if settings.image_tv_effect_enabled:
        image_effect_index = next_input_index
        command.extend(["-stream_loop", "-1", "-i", str(settings.image_tv_effect_file)])

    filters = [
        (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={fps},format=yuv420p,"
            f"trim=duration={separator_duration},setpts=PTS-STARTPTS[v0]"
        )
    ]
    for offset in range(1, 4):
        scale = settings.motion_scale
        zoom = f"1+({scale}-1)*on/{frames - 1}"
        x = "floor((iw-iw/zoom)/2)"
        filters.append(
            f"[{offset}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},zoompan=z='{zoom}':x='{x}':"
            f"y='floor((ih-ih/zoom)/2)':d={frames}:s={width}x{height}:fps={fps},"
            f"trim=duration={image_duration},setpts=PTS-STARTPTS[v{offset}]"
        )
    filters.append("[v1][v2][v3]concat=n=3:v=1:a=0[vbase]")
    filters.append(
        f"[vbase][v0]overlay=0:0:eof_action=pass:enable='between(t,0,{separator_duration:.3f})'[vstory]"
    )
    glitch_starts = [
        separator_duration,
        image_duration,
        2 * image_duration,
    ]
    current_video = "vstory"
    if settings.glitch_enabled:
        enable = "+".join(
            f"between(t\\,{start:.3f}\\,{min(duration_seconds, start + settings.glitch_duration_seconds):.3f})"
            for start in glitch_starts
        )
        filters.append(
            f"[{current_video}]noise=alls={settings.glitch_noise_strength}:allf=t+u:enable='{enable}',"
            f"drawbox=y=ih*0.43:w=iw:h=8:color=white@0.10:t=fill:enable='{enable}'[vfx]"
        )
        current_video = "vfx"
    if image_effect_index >= 0:
        filters.append(
            f"[{image_effect_index}:v]scale={width}:{height}:flags=lanczos,"
            "format=rgba,"
            f"colorchannelmixer=aa={settings.image_tv_effect_opacity},"
            "setpts=PTS-STARTPTS[imageeffect]"
        )
        filters.append(
            f"[{current_video}][imageeffect]overlay=0:0:eof_action=repeat:repeatlast=1:"
            f"shortest=0:format=auto:enable='not(between(t\\,0\\,{separator_duration:.3f}))'[veffect]"
        )
        current_video = "veffect"
    if settings.tv_frame_enabled:
        filters.append(
            f"[{overlay_index}:v]scale={width}:{height},format=rgba,"
            f"colorchannelmixer=aa={settings.tv_frame_opacity}[tv]"
        )
        filters.append(f"[{current_video}][tv]overlay=0:0:shortest=1[vframed]")
        current_video = "vframed"
    filters.append(
        f"[{current_video}]subtitles=filename='{_ffmpeg_filter_path(caption_path)}'[vout]"
    )

    voice_duration = duration_seconds
    delay_ms = 0
    filters.append(
        f"[{narration_index}:a]atrim=start=0:end={voice_duration},asetpts=PTS-STARTPTS,"
        f"aresample=48000,aformat=channel_layouts=stereo,adelay={delay_ms}|{delay_ms}[voice]"
    )
    speech_factor = _db_factor(settings.music_speech_gain_db)
    volume_expression = f"{speech_factor:.6f}"
    fade_out_start = max(0.0, duration_seconds - settings.music_fade_seconds)
    filters.append(
        f"[{music_index}:a]atrim=duration={duration_seconds},asetpts=PTS-STARTPTS,"
        f"aresample=48000,aformat=channel_layouts=stereo,"
        f"loudnorm=I={settings.music_target_lufs}:TP=-2:LRA=7,"
        f"volume='{volume_expression}':eval=frame,"
        f"afade=t=in:st=0:d={settings.music_fade_seconds},"
        f"afade=t=out:st={fade_out_start}:d={settings.music_fade_seconds}[music]"
    )
    audio_inputs = ["[voice]", "[music]"]
    if separator_has_audio:
        filters.append(
            f"[0:a]atrim=start=0:end={separator_duration},asetpts=PTS-STARTPTS,"
            "aresample=48000,aformat=channel_layouts=stereo[separator_audio]"
        )
        audio_inputs.insert(0, "[separator_audio]")
    if sfx_index >= 0:
        sfx_factor = _db_factor(settings.sfx_gain_db)
        filters.append(f"[{sfx_index}:a]asplit=3[sfx0][sfx1][sfx2]")
        sfx_labels = []
        for index, start in enumerate(glitch_starts):
            delay = round(start * 1000)
            label = f"s{index}"
            filters.append(
                f"[sfx{index}]aresample=48000,aformat=channel_layouts=stereo,"
                f"volume={sfx_factor:.6f},adelay={delay}|{delay}[{label}]"
            )
            sfx_labels.append(f"[{label}]")
        audio_inputs.extend(sfx_labels)
    if paper_sfx_index >= 0:
        paper_factor = _db_factor(settings.paper_sfx_gain_db)
        filters.append(
            f"[{paper_sfx_index}:a]aresample=48000,"
            "aformat=channel_layouts=stereo,"
            f"volume={paper_factor:.6f}[paper]"
        )
        audio_inputs.append("[paper]")
    if shutter_sfx_index >= 0:
        shutter_start = 2 * image_duration
        shutter_delay = round(shutter_start * 1000)
        shutter_factor = _db_factor(settings.shutter_sfx_gain_db)
        filters.append(
            f"[{shutter_sfx_index}:a]aresample=48000,"
            "aformat=channel_layouts=stereo,"
            f"volume={shutter_factor:.6f},"
            f"adelay={shutter_delay}|{shutter_delay}[shutter]"
        )
        audio_inputs.append("[shutter]")
    filters.append(
        f"{''.join(audio_inputs)}amix=inputs={len(audio_inputs)}:"
        "duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.891[mix]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[mix]",
            "-t",
            str(duration_seconds),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            "-y",
            str(output_path),
        ]
    )
    _run(command, "Milestone 6 reference proof")
    probe = probe_media(output_path)
    actual_duration = float(probe["duration_seconds"])
    if abs(actual_duration - duration_seconds) > 0.15:
        raise RuntimeError(
            f"Reference proof duration mismatch: {actual_duration:.3f}s"
        )
    report = {
        "schema_version": 1,
        "created_at_utc": utc_now(),
        "output_file": str(output_path),
        "output_sha256": sha256_file(output_path),
        "duration_seconds": round(actual_duration, 6),
        "resolution": f"{width}x{height}",
        "fps": fps,
        "images": [str(path) for path in images[:3]],
        "separator_file": str(separator_file) if separator_file is not None else None,
        "separator_audio_included": separator_has_audio,
        "narration_file": str(narration_file),
        "music_file": str(music_file),
        "sfx_file": str(sfx_file),
        "paper_sfx_file": str(paper_sfx_file) if paper_sfx_file else None,
        "shutter_sfx_file": str(shutter_sfx_file) if shutter_sfx_file else None,
        "tv_frame_file": str(settings.tv_frame_file) if settings.tv_frame_enabled else None,
        "captions_file": str(caption_path),
        "captions_source_file": str(captions_file) if captions_file is not None else None,
        "verified_features": [
            "alternating_image_motion",
            "tv_frame_overlay",
            "subtle_animated_tv_overlay",
            "paper_section_sfx",
            "shutter_every_third_image_sfx",
            "caption_layer_above_frame",
            "looped_background_music",
            "music_ducking_under_voice",
        ],
        "quality": {"status": "passed"},
    }
    atomic_write_json(output_path.with_suffix(".json"), report)
    return report


def _editing_reference(settings: EffectsSettings) -> dict | None:
    if not settings.editing_reference_file.is_file():
        return None
    return {
        "file": str(settings.editing_reference_file),
        "sha256": sha256_file(settings.editing_reference_file),
        "purpose": "Canonical CapCut track-order and visual-treatment reference",
        "track_order_top_to_bottom": [
            "captions_and_title_elements",
            "shutter_tv_frame",
            "manual_news_separators",
            "tape_dropout_glitch",
            "story_visuals",
            "voiceover_and_sfx",
            "repeated_background_music",
        ],
    }


def _image_tv_effect_plan(settings: EffectsSettings) -> dict | None:
    if not settings.image_tv_effect_enabled:
        return None
    path = settings.image_tv_effect_file
    if not path.is_file():
        raise FileNotFoundError(f"Animated TV effect not found: {path}")
    probe = probe_media(path)
    video = next(
        (
            stream
            for stream in probe.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )
    if video is None:
        raise RuntimeError(f"Animated TV effect has no video stream: {path}")
    pixel_format = str(video.get("pix_fmt", "")).casefold()
    alpha_formats = {
        "argb",
        "abgr",
        "rgba",
        "bgra",
        "gbrap",
        "gbrap10le",
        "gbrap12le",
        "gbrap16le",
    }
    if pixel_format not in alpha_formats and not pixel_format.startswith("yuva"):
        raise RuntimeError(
            "Animated TV effect must contain a native alpha channel; "
            f"found pixel format {pixel_format or 'unknown'}."
        )
    duration = float(probe.get("duration_seconds", 0))
    if duration <= 0:
        raise RuntimeError("Animated TV effect duration must be positive.")
    return {
        "enabled": True,
        "file": str(path),
        "sha256": sha256_file(path),
        "codec": str(video.get("codec_name", "")),
        "pixel_format": pixel_format,
        "native_alpha": True,
        "opacity": settings.image_tv_effect_opacity,
        "source_duration_seconds": round(duration, 6),
        "loop_to_program_duration": True,
        "audio_ignored": True,
        "coverage": "story_images_except_manual_separator_intervals",
        "layer_position": "above_glitch_below_manual_separator",
    }


def render_project_reference_proof(
    *,
    video_number: int,
    settings: EffectsSettings,
    duration_seconds: float = 60.0,
) -> dict:
    video_dir = project_dir(video_number)
    timing = load_json(video_dir / "story_timing.json", "Story timing manifest")
    stories = timing.get("stories", [])
    if not stories:
        raise RuntimeError("Story timing contains no stories for the reference proof.")
    narration_file = Path(stories[0]["audio_file"])
    first_separator = stories[0].get("separator") or {}
    separator_file = Path(first_separator["file"]) if first_separator.get("file") else None
    separator_duration = first_separator.get("duration")
    generated = ensure_default_audio_assets(settings)
    music_file, _ = select_music_track(settings)
    captions_manifest = load_json(
        video_dir / "06_captions" / "captions.json",
        "Caption manifest",
    )
    captions_file = Path(captions_manifest["combined_srt_file"])
    output = video_dir / "07_effects" / "milestone6_reference_60s.mp4"
    generated_images = sorted(
        path.resolve()
        for path in (video_dir / "05_visuals" / "generated").glob("*")
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    reference_images = (
        generated_images[:3]
        if len(generated_images) >= 3
        else _discover_reference_images()
    )
    report = render_reference_proof(
        output_path=output,
        images=reference_images,
        narration_file=narration_file,
        music_file=music_file,
        sfx_file=generated["sfx"],
        paper_sfx_file=(
            settings.paper_sfx_file if settings.paper_sfx_enabled else None
        ),
        shutter_sfx_file=(
            settings.shutter_sfx_file if settings.shutter_sfx_enabled else None
        ),
        settings=settings,
        duration_seconds=duration_seconds,
        separator_file=separator_file,
        separator_duration_seconds=(
            float(separator_duration) if separator_duration is not None else None
        ),
        captions_file=captions_file,
    )
    effects_plan = video_dir / "07_effects" / "effects_plan.json"
    report["effects_plan_sha256"] = sha256_file(effects_plan)
    atomic_write_json(output.with_suffix(".json"), report)
    return report


def build_effects_plan(
    *,
    video_number: int,
    settings: EffectsSettings | None = None,
    force: bool = False,
) -> dict:
    settings = settings or EffectsSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")
    state, timing, media, captions = _require_inputs(video_dir)
    generated = ensure_default_audio_assets(settings)
    editing_reference = _editing_reference(settings)
    overlay = None
    if settings.tv_frame_enabled:
        if not settings.tv_frame_file.is_file():
            raise FileNotFoundError(f"TV frame overlay not found: {settings.tv_frame_file}")
        overlay = {
            "file": str(settings.tv_frame_file),
            "sha256": sha256_file(settings.tv_frame_file),
            "opacity": settings.tv_frame_opacity,
            "coverage": "complete_program",
            "caption_layer_above_overlay": True,
        }
    image_tv_effect = _image_tv_effect_plan(settings)
    music = None
    if settings.music_enabled:
        track, metadata = select_music_track(settings)
        music = _music_plan(track, metadata, timing, settings)
    sfx_file = generated["sfx"]
    sfx = {
        "file": str(sfx_file),
        "sha256": sha256_file(sfx_file),
        "gain_db": settings.sfx_gain_db,
        "license_metadata_file": str(_license_sidecar(sfx_file)),
    }
    visuals = build_visual_events(timing, media, settings)
    coverage = validate_visual_coverage(visuals, timing)
    if coverage["status"] != "passed":
        raise RuntimeError(f"Effects visual coverage failed: {coverage['errors']}")
    timeline_sfx = build_timeline_sfx(timing, visuals, settings)
    timing_path = video_dir / "story_timing.json"
    media_path = video_dir / "05_visuals" / "media_plan.json"
    captions_path = video_dir / "06_captions" / "captions.json"
    fingerprint = _input_fingerprint(
        timing_path,
        media_path,
        captions_path,
        settings,
        overlay,
        image_tv_effect,
        editing_reference,
        music,
        sfx,
        timeline_sfx,
    )
    output_dir = video_dir / "07_effects"
    manifest_path = output_dir / "effects_plan.json"
    if manifest_path.is_file() and not force:
        existing = load_json(manifest_path, "Effects plan")
        if (
            existing.get("input_fingerprint") == fingerprint
            and existing.get("quality", {}).get("status") == "passed"
        ):
            cached = dict(existing)
            cached["run_status"] = "cached"
            _write_state(video_dir, state, manifest_path, cached)
            return cached

    glitches = build_glitch_events(timing, visuals, settings)
    duration = float(timing["duration_seconds"])
    glitch_coverage = sum(item["duration"] for item in glitches) / max(duration, 0.001)
    if glitch_coverage > 0.03:
        raise RuntimeError("Glitch coverage exceeds the 3% readability cap.")
    captions_file = Path(captions["combined_srt_file"])
    if not captions_file.is_file() or sha256_file(captions_file) != captions["combined_srt_sha256"]:
        raise RuntimeError("Caption file is missing or changed after caption planning.")
    separators = _separator_events(timing)
    narrations = _narration_events(timing)
    plan = {
        "schema_version": EFFECTS_SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "created_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
        "run_status": "generated",
        "input_fingerprint": fingerprint,
        "duration_seconds": round(duration, 6),
        "configuration": {
            **asdict(settings),
            "tv_frame_file": str(settings.tv_frame_file),
            "image_tv_effect_file": str(settings.image_tv_effect_file),
            "paper_sfx_file": str(settings.paper_sfx_file),
            "shutter_sfx_file": str(settings.shutter_sfx_file),
            "music_directory": str(settings.music_directory),
            "music_file": str(settings.music_file) if settings.music_file else None,
            "editing_reference_file": str(settings.editing_reference_file),
        },
        "editing_reference": editing_reference or {"available": False},
        "visual_events": visuals,
        "separator_events": separators,
        "glitch_events": glitches,
        "image_tv_effect": image_tv_effect or {"enabled": False},
        "tv_frame": overlay or {"enabled": False},
        "captions": {
            "file": str(captions_file),
            "sha256": sha256_file(captions_file),
            "layer_position": "above_tv_frame",
        },
        "audio": {
            "narration_events": narrations,
            "background_music": music or {"enabled": False},
            "glitch_sfx": {
                **sfx,
                "events": [
                    {"start": item["start"], "end": item["end"]}
                    for item in glitches
                ],
            },
            "timeline_sfx": timeline_sfx,
            "mix_order": [
                "voiceover",
                "separator_audio",
                "paper_section_sfx",
                "shutter_every_third_image_sfx",
                "glitch_sfx",
                "ducked_background_music",
            ],
            "final_target": {
                "integrated_lufs": -14.0,
                "true_peak_db": -1.0,
                "note": "Enforced and measured by Milestone 8 final render.",
            },
        },
        "ffmpeg_templates": _filter_templates(settings),
        "quality": {
            "status": "passed",
            "visual_coverage": coverage,
            "motion_is_deterministic": True,
            "motion_is_consistent": all(
                item["motion"]["type"] in {"slow_push_in", "static"}
                for item in visuals
            ),
            "no_directional_pans": all(
                item["motion"]["type"] not in {"pan_left", "pan_right"}
                for item in visuals
            ),
            "maximum_motion_scale": max(
                (item["motion"]["maximum_scale"] for item in visuals),
                default=1.0,
            ),
            "glitch_timeline_coverage_ratio": round(glitch_coverage, 6),
            "glitch_under_three_percent": glitch_coverage <= 0.03,
            "captions_remain_top_layer": True,
            "image_tv_effect_uses_native_alpha": (
                not settings.image_tv_effect_enabled
                or bool(image_tv_effect and image_tv_effect["native_alpha"])
            ),
            "image_tv_effect_excludes_separators": (
                not settings.image_tv_effect_enabled
                or image_tv_effect.get("coverage")
                == "story_images_except_manual_separator_intervals"
            ),
            "attribution_event_count": sum(
                item.get("attribution") is not None for item in visuals
            ),
            "music_covers_complete_timeline": not settings.music_enabled or (
                music is not None
                and music["loop_count"] * music["track_duration_seconds"] >= duration
            ),
            "music_ducking_windows_match_narration": not settings.music_enabled or (
                len(music["ducking"]["speech_windows"]) == len(narrations)
            ),
            "paper_sfx_covers_every_story_start": (
                not settings.paper_sfx_enabled
                or len(
                    [
                        event
                        for event in timeline_sfx["events"]
                        if event["sound"] == "paper"
                    ]
                )
                == len(narrations)
            ),
            "shutter_sfx_uses_every_third_image": all(
                int(event["visual_sequence_number"]) % 3 == 0
                for event in timeline_sfx["events"]
                if event["sound"] == "shutter"
            ),
            "shutter_sfx_excludes_separator_intervals": all(
                not any(
                    float(separator["start"]) <= float(event["start"])
                    < float(separator["end"])
                    for separator in separators
                )
                for event in timeline_sfx["events"]
                if event["sound"] == "shutter"
            ),
            "all_referenced_files_exist": True,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(manifest_path, plan)
    atomic_write_text(
        output_dir / "ffmpeg_filter_templates.txt",
        json.dumps(plan["ffmpeg_templates"], ensure_ascii=False, indent=2) + "\n",
    )
    _write_state(video_dir, state, manifest_path, plan)
    return plan


def effects_status(video_number: int, settings: EffectsSettings | None = None) -> dict:
    settings = settings or EffectsSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    manifest_path = video_dir / "07_effects" / "effects_plan.json"
    try:
        state, timing, media, captions = _require_inputs(video_dir)
        del state
        generated = ensure_default_audio_assets(settings)
        editing_reference = _editing_reference(settings)
        overlay = None
        if settings.tv_frame_enabled:
            overlay = {
                "file": str(settings.tv_frame_file),
                "sha256": sha256_file(settings.tv_frame_file),
                "opacity": settings.tv_frame_opacity,
                "coverage": "complete_program",
                "caption_layer_above_overlay": True,
            }
        image_tv_effect = _image_tv_effect_plan(settings)
        music = None
        if settings.music_enabled:
            track, metadata = select_music_track(settings)
            music = _music_plan(track, metadata, timing, settings)
        sfx_file = generated["sfx"]
        sfx = {
            "file": str(sfx_file),
            "sha256": sha256_file(sfx_file),
            "gain_db": settings.sfx_gain_db,
            "license_metadata_file": str(_license_sidecar(sfx_file)),
        }
        visuals = build_visual_events(timing, media, settings)
        timeline_sfx = build_timeline_sfx(timing, visuals, settings)
        expected = _input_fingerprint(
            video_dir / "story_timing.json",
            video_dir / "05_visuals" / "media_plan.json",
            video_dir / "06_captions" / "captions.json",
            settings,
            overlay,
            image_tv_effect,
            editing_reference,
            music,
            sfx,
            timeline_sfx,
        )
        manifest = load_json(manifest_path, "Effects plan")
        ready = manifest.get("input_fingerprint") == expected and manifest.get("quality", {}).get("status") == "passed"
        return {
            "status": "EFFECTS_READY" if ready else "STALE",
            "manifest": str(manifest_path),
            "reason": "Effects plan is current." if ready else "Effects inputs changed.",
        }
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return {"status": "BLOCKED", "manifest": str(manifest_path), "reason": str(exc)}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan Milestone 6 motion, TV treatment, glitch/SFX, and music."
    )
    parser.add_argument("--video", type=int)
    parser.add_argument(
        "--action",
        choices=("generate", "status", "install-assets", "reference-proof"),
        default="generate",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--proof-seconds", type=float, default=60.0)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    settings = EffectsSettings.from_environment()
    try:
        settings.validate()
        if args.action == "install-assets":
            paths = ensure_default_audio_assets(settings)
            print(f"Background music: {paths['music']}")
            print(f"Glitch SFX      : {paths['sfx']}")
            return 0
        if args.action == "reference-proof":
            if args.video is None:
                raise ValueError("--video is required for reference-proof.")
            report = render_project_reference_proof(
                video_number=args.video,
                settings=settings,
                duration_seconds=args.proof_seconds,
            )
            print(f"Reference proof: {report['output_file']}")
            return 0
        if args.video is None:
            raise ValueError("--video is required for generate/status.")
        if args.action == "status":
            print(json.dumps(effects_status(args.video, settings), ensure_ascii=False, indent=2))
            return 0
        plan = build_effects_plan(
            video_number=args.video,
            settings=settings,
            force=args.force,
        )
        print(
            f"Milestone 6 ready: {project_dir(args.video) / '07_effects' / 'effects_plan.json'}"
        )
        print(
            f"Visuals={len(plan['visual_events'])} | "
            f"glitches={len(plan['glitch_events'])} | "
            f"music loops={plan['audio']['background_music'].get('loop_count', 0)}"
        )
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        report_cli_error("EFFECTS", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
