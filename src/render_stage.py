from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from file_locations import project_dir, project_name  # noqa: E402
from news_archive import NewsArchive  # noqa: E402
from motion_filters import (  # noqa: E402
    MOTION_RENDERER_VERSION,
    smooth_push_in_filter,
)
from separator_policy import require_separator_checkpoint  # noqa: E402
from tooling import find_executable  # noqa: E402


STAGE_NAME = "render"
SCHEMA_VERSION = 1


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


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


@dataclass(frozen=True)
class RenderSettings:
    width: int = 1920
    height: int = 1080
    preview_width: int = 960
    preview_height: int = 540
    fps: int = 30
    segment_crf: int = 18
    final_crf: int = 20
    preview_crf: int = 25
    preset: str = "veryfast"
    separator_audio_gain_db: float = -9.0
    final_target_lufs: float = -14.0
    final_true_peak_db: float = -1.5
    caption_margin_vertical: int = 40
    title_font_size: int = 30
    title_margin_left: int = 110
    title_margin_top: int = 70
    attribution_font_size: int = 20
    attribution_margin_right: int = 115
    attribution_margin_bottom: int = 135
    page_flip_enabled: bool = True
    page_flip_duration_seconds: float = 0.7

    @classmethod
    def from_environment(cls) -> "RenderSettings":
        return cls(
            width=int(os.getenv("RENDER_WIDTH", "1920")),
            height=int(os.getenv("RENDER_HEIGHT", "1080")),
            preview_width=int(os.getenv("PREVIEW_WIDTH", "960")),
            preview_height=int(os.getenv("PREVIEW_HEIGHT", "540")),
            fps=int(os.getenv("RENDER_FPS", "30")),
            segment_crf=int(os.getenv("RENDER_SEGMENT_CRF", "18")),
            final_crf=int(os.getenv("RENDER_FINAL_CRF", "20")),
            preview_crf=int(os.getenv("RENDER_PREVIEW_CRF", "25")),
            preset=os.getenv("RENDER_PRESET", "veryfast").strip(),
            separator_audio_gain_db=float(
                os.getenv("SEPARATOR_AUDIO_GAIN_DB", "-9")
            ),
            final_target_lufs=float(os.getenv("FINAL_TARGET_LUFS", "-14")),
            final_true_peak_db=float(os.getenv("FINAL_TRUE_PEAK_DB", "-1.5")),
            caption_margin_vertical=int(
                os.getenv("CAPTION_MARGIN_VERTICAL", "40")
            ),
            title_font_size=int(os.getenv("TITLE_FONT_SIZE", "30")),
            title_margin_left=int(os.getenv("TITLE_MARGIN_LEFT", "110")),
            title_margin_top=int(os.getenv("TITLE_MARGIN_TOP", "70")),
            attribution_font_size=int(
                os.getenv("ATTRIBUTION_FONT_SIZE", "20")
            ),
            attribution_margin_right=int(
                os.getenv("ATTRIBUTION_MARGIN_RIGHT", "115")
            ),
            attribution_margin_bottom=int(
                os.getenv("ATTRIBUTION_MARGIN_BOTTOM", "135")
            ),
            page_flip_enabled=_env_bool("PAGE_FLIP_ENABLED", True),
            page_flip_duration_seconds=float(
                os.getenv("PAGE_FLIP_DURATION_SECONDS", "0.7")
            ),
        )

    def validate(self) -> None:
        for width, height, label in (
            (self.width, self.height, "final"),
            (self.preview_width, self.preview_height, "preview"),
        ):
            if width < 320 or height < 180 or width % 2 or height % 2:
                raise ValueError(f"Invalid {label} render dimensions: {width}x{height}")
        if not 1 <= self.fps <= 120:
            raise ValueError("RENDER_FPS must be between 1 and 120.")
        if not 0 <= self.segment_crf <= 35 or not 0 <= self.final_crf <= 35:
            raise ValueError("Render CRF values must be between 0 and 35.")
        if not self.preset:
            raise ValueError("RENDER_PRESET cannot be empty.")
        if self.caption_margin_vertical < 0:
            raise ValueError("CAPTION_MARGIN_VERTICAL cannot be negative.")
        if not 12 <= self.title_font_size <= 96:
            raise ValueError("TITLE_FONT_SIZE must be between 12 and 96.")
        if self.title_margin_left < 0 or self.title_margin_top < 0:
            raise ValueError("Title margins cannot be negative.")
        if not 12 <= self.attribution_font_size <= 48:
            raise ValueError("ATTRIBUTION_FONT_SIZE must be between 12 and 48.")
        if self.attribution_margin_right < 0 or self.attribution_margin_bottom < 0:
            raise ValueError("Attribution margins cannot be negative.")
        if not 0.25 <= self.page_flip_duration_seconds <= 1.5:
            raise ValueError(
                "PAGE_FLIP_DURATION_SECONDS must be between 0.25 and 1.5."
            )


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise RuntimeError(f"{label} failed: {detail}")
    return result


def _ffmpeg() -> Path:
    path = find_executable("ffmpeg")
    if path is None:
        raise RuntimeError("FFmpeg is required for Milestone 8.")
    return path


def _ffprobe() -> Path:
    path = find_executable("ffprobe")
    if path is None:
        raise RuntimeError("FFprobe is required for Milestone 8.")
    return path


def probe_media(path: Path) -> dict[str, Any]:
    result = _run(
        [
            str(_ffprobe()),
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels,duration",
            "-of",
            "json",
            str(path),
        ],
        f"Probe {path.name}",
    )
    payload = json.loads(result.stdout)
    payload["path"] = str(path.resolve())
    return payload


def _filter_path(path: Path) -> str:
    value = path.resolve().as_posix().replace("'", r"\'")
    if len(value) >= 2 and value[1] == ":":
        value = value[0] + r"\:" + value[2:]
    return value


def _compact_story_title(title: str, order: int) -> str:
    normalized = re.sub(r"\s+", " ", title).strip()
    if not normalized:
        normalized = f"Tech News {order}"
    lines = textwrap.wrap(
        normalized,
        width=54,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(lines) <= 2:
        return "\n".join(lines)
    second = textwrap.shorten(
        " ".join(lines[1:]),
        width=54,
        placeholder="...",
    )
    return f"{lines[0]}\n{second}"


def _compact_source_attribution(attribution: dict) -> str:
    creator = re.sub(r"\s+", " ", str(attribution.get("creator", ""))).strip()
    source_url = str(attribution.get("source_url", "")).strip()
    license_name = re.sub(
        r"\s+",
        " ",
        str(attribution.get("license_name", "")),
    ).strip()
    host = urlparse(source_url).netloc.casefold()
    provider = "Wikimedia Commons" if "wikimedia.org" in host else host.removeprefix("www.")
    if not creator:
        provided = re.sub(
            r"\s+",
            " ",
            str(attribution.get("provided_text", "")),
        ).strip()
        creator = provided.split(";")[0].split("—")[-1].strip()
    parts = []
    for value in (creator, provider, license_name):
        if value and value not in parts:
            parts.append(value)
    text = "Source: " + " · ".join(parts)
    return text if len(text) <= 125 else text[:122].rstrip() + "..."


def _db_factor(value: float) -> float:
    return 10 ** (value / 20)


def _required_inputs(video_dir: Path) -> tuple[dict, dict, dict, dict]:
    state = load_json(video_dir / "state.json", "Stage state")
    if state.get("stages", {}).get("effects", {}).get("status") != "EFFECTS_READY":
        raise RuntimeError("Milestone 6 must be EFFECTS_READY before rendering.")
    timing = load_json(video_dir / "story_timing.json", "Story timing")
    require_separator_checkpoint(state, timing)
    if timing.get("separator_timing_mode") != "overlay_at_narration_start":
        raise RuntimeError(
            "Story timing is stale: separators must overlay narration from story start."
        )
    effects = load_json(video_dir / "07_effects" / "effects_plan.json", "Effects plan")
    captions = load_json(video_dir / "06_captions" / "captions.json", "Captions")
    if effects.get("quality", {}).get("status") != "passed":
        raise RuntimeError("Effects plan quality must pass before rendering.")
    if captions.get("quality", {}).get("status") != "passed":
        raise RuntimeError("Caption quality must pass before rendering.")
    return state, timing, effects, captions


def build_render_plan(
    video_number: int,
    settings: RenderSettings | None = None,
) -> dict:
    settings = settings or RenderSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    state, timing, effects, captions = _required_inputs(video_dir)
    duration = float(timing["duration_seconds"])
    visuals = effects.get("visual_events", [])
    if not visuals:
        raise RuntimeError("Effects plan has no visual events.")
    referenced = []
    for event in visuals:
        path = Path(event["asset"]["file"])
        if not path.is_file():
            raise FileNotFoundError(f"Visual asset is missing: {path}")
        referenced.append({"file": str(path), "sha256": sha256_file(path)})
    separators = []
    if not timing.get("quality", {}).get("manual_separators_skipped", False):
        for story in timing.get("stories", []):
            item = story["separator"]
            path = Path(item["file"])
            if not path.is_file():
                raise FileNotFoundError(f"Separator is missing: {path}")
            separators.append(
                {
                    "story_id": story["story_id"],
                    "order": story["order"],
                    "start": item["start"],
                    "end": item["end"],
                    "duration": item["duration"],
                    "file": str(path),
                    "sha256": sha256_file(path),
                    "has_audio": bool(item.get("has_audio")),
                }
            )
    narration = []
    for story in timing.get("stories", []):
        path = Path(story["audio_file"])
        if not path.is_file():
            raise FileNotFoundError(f"Narration is missing: {path}")
        narration.append(
            {
                "story_id": story["story_id"],
                "order": story["order"],
                "start": story["narration"]["start"],
                "end": story["narration"]["end"],
                "duration": story["narration"]["duration"],
                "file": str(path),
                "sha256": sha256_file(path),
            }
        )
    caption_file = Path(captions["combined_srt_file"])
    output_dir = video_dir / "08_render"
    title_dir = output_dir / "titles"
    title_dir.mkdir(parents=True, exist_ok=True)
    attribution_dir = output_dir / "attributions"
    attribution_dir.mkdir(parents=True, exist_ok=True)
    titles = []
    for story in timing.get("stories", []):
        order = int(story["order"])
        title_file = title_dir / f"{order:02d}_story_title.txt"
        display_title = _compact_story_title(str(story.get("title", "")), order)
        atomic_write_text(title_file, display_title)
        titles.append(
            {
                "story_id": story["story_id"],
                "order": order,
                "start": story["narration"]["start"],
                "end": story["narration"]["end"],
                "text": display_title,
                "text_file": str(title_file),
            }
        )
    attributions = []
    for event in visuals:
        attribution = event.get("attribution")
        if not attribution or attribution.get("required") is not True:
            continue
        text_file = attribution_dir / f"{event['event_id']}_source.txt"
        display_text = _compact_source_attribution(attribution)
        atomic_write_text(text_file, display_text)
        attributions.append(
            {
                "event_id": event["event_id"],
                "story_id": event["story_id"],
                "start": event["timeline_start"],
                "end": event["timeline_end"],
                "text": display_text,
                "text_file": str(text_file),
                "source_url": attribution.get("source_url", ""),
                "license_name": attribution.get("license_name", ""),
            }
        )
    frame = effects.get("tv_frame") or {}
    frame_file = Path(frame["file"]) if frame.get("file") else None
    image_tv_effect = effects.get("image_tv_effect") or {"enabled": False}
    if image_tv_effect.get("enabled"):
        effect_file = Path(image_tv_effect.get("file", ""))
        if not effect_file.is_file():
            raise FileNotFoundError(f"Animated TV effect is missing: {effect_file}")
        if sha256_file(effect_file) != image_tv_effect.get("sha256"):
            raise RuntimeError("Animated TV effect changed after effects planning.")
        if image_tv_effect.get("native_alpha") is not True:
            raise RuntimeError("Animated TV effect must preserve native alpha.")
    music = effects.get("audio", {}).get("background_music")
    sfx = effects.get("audio", {}).get("glitch_sfx", {})
    timeline_sfx = effects.get("audio", {}).get("timeline_sfx", {})
    timeline_sfx_assets = timeline_sfx.get("assets", {})
    for asset_id, asset in timeline_sfx_assets.items():
        path = Path(asset.get("file", ""))
        if not path.is_file():
            raise FileNotFoundError(f"Timeline SFX asset is missing ({asset_id}): {path}")
        if sha256_file(path) != asset.get("sha256"):
            raise RuntimeError(f"Timeline SFX asset changed after planning: {path}")
    for event in timeline_sfx.get("events", []):
        if event.get("sound") not in timeline_sfx_assets:
            raise RuntimeError(
                f"Timeline SFX event references an unknown asset: {event.get('sound')}"
            )
        if not 0 <= float(event.get("start", -1)) < duration:
            raise RuntimeError(
                f"Timeline SFX event is outside the program: {event.get('event_id')}"
            )
    separators_skipped = timing.get("quality", {}).get(
        "manual_separators_skipped", False
    )
    page_flip_events = []
    if settings.page_flip_enabled and separators_skipped:
        for story in timing.get("stories", []):
            start = float(story["narration"]["start"])
            event_duration = min(
                settings.page_flip_duration_seconds,
                max(0.0, duration - start),
            )
            if event_duration >= 0.05:
                page_flip_events.append(
                    {
                        "story_id": story["story_id"],
                        "order": int(story["order"]),
                        "start": round(start, 6),
                        "end": round(start + event_duration, 6),
                        "duration": round(event_duration, 6),
                    }
                )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "created_at_utc": utc_now(),
        "duration_seconds": duration,
        "settings": asdict(settings),
        "timeline_mode": timing["separator_timing_mode"],
        "visual_events": visuals,
        "separators": separators,
        "narration": narration,
        "glitch_events": effects.get("glitch_events", []),
        "image_tv_effect": image_tv_effect,
        "tv_frame": frame,
        "story_titles": titles,
        "source_attributions": attributions,
        "captions": {
            "file": str(caption_file),
            "sha256": sha256_file(caption_file),
            "cue_count": captions.get("quality", {}).get("cue_count"),
        },
        "background_music": music,
        "glitch_sfx": sfx,
        "timeline_sfx": timeline_sfx,
        "page_flip": {
            "enabled": bool(page_flip_events),
            "mode": "chapter_open" if page_flip_events else "disabled",
            "only_when_separators_skipped": True,
            "events": page_flip_events,
        },
        "referenced_visuals": referenced,
        "quality": {
            "status": "passed",
            "separator_and_narration_start_together": (
                not separators
                or (
                    len(separators) == len(narration)
                    and all(
                        math.isclose(
                            float(s["start"]),
                            float(n["start"]),
                            abs_tol=0.001,
                        )
                        for s, n in zip(separators, narration)
                    )
                )
            ),
            "images_start_at_chapter_start": (
                not timing.get("quality", {}).get("manual_separators_skipped", False)
                or not separators
            ),
            "first_caption_starts_at_program_start": math.isclose(
                float(captions.get("quality", {}).get("first_caption_start", -1)),
                0.0,
                abs_tol=0.01,
            ),
            "no_directional_pans": effects.get("quality", {}).get(
                "no_directional_pans", False
            ),
            "persistent_titles_cover_all_stories": len(titles) == len(narration),
            "image_tv_effect_excludes_separators": (
                not image_tv_effect.get("enabled")
                or image_tv_effect.get("coverage")
                == "story_images_except_manual_separator_intervals"
            ),
            "visual_count": len(visuals),
            "persistent_title_count": len(titles),
            "source_attribution_count": len(attributions),
            "timeline_sfx_event_count": len(timeline_sfx.get("events", [])),
            "page_flip_matches_separator_mode": (
                not page_flip_events or (separators_skipped and not separators)
            ),
        },
    }
    if not all(
        value
        for key, value in payload["quality"].items()
        if key not in {
            "visual_count",
            "persistent_title_count",
            "source_attribution_count",
            "timeline_sfx_event_count",
        }
    ):
        raise RuntimeError(f"Milestone 8 plan quality failed: {payload['quality']}")
    fingerprint_payload = {
        "timing": sha256_file(video_dir / "story_timing.json"),
        "effects": sha256_file(video_dir / "07_effects" / "effects_plan.json"),
        "captions": payload["captions"]["sha256"],
        "settings": asdict(settings),
    }
    payload["input_fingerprint"] = json_fingerprint(fingerprint_payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "render_plan.json", payload)
    state.setdefault("stages", {})[STAGE_NAME] = {
        "status": "RENDER_PLANNED",
        "render_plan": str(output_dir / "render_plan.json"),
        "input_fingerprint": payload["input_fingerprint"],
        "updated_at_utc": utc_now(),
    }
    state["updated_at_utc"] = state["stages"][STAGE_NAME]["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)
    return payload


def _segment_fingerprint(event: dict, settings: RenderSettings) -> str:
    return json_fingerprint(
        {
            "asset": event["asset"],
            "duration": event["duration"],
            "motion": event["motion"],
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "crf": settings.segment_crf,
            "visual_motion_renderer_version": MOTION_RENDERER_VERSION,
        }
    )


def _render_visual_segments(
    video_dir: Path,
    plan: dict,
    settings: RenderSettings,
) -> list[Path]:
    directory = video_dir / "08_render" / "intermediates" / "visuals_1080p"
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    total = len(plan["visual_events"])
    for index, event in enumerate(plan["visual_events"], start=1):
        output = directory / f"{index:04d}_{event['event_id']}.mp4"
        sidecar = output.with_suffix(".json")
        fingerprint = _segment_fingerprint(event, settings)
        if output.is_file() and sidecar.is_file():
            cached = load_json(sidecar, "Visual segment cache")
            if cached.get("input_fingerprint") == fingerprint:
                outputs.append(output)
                continue
        duration = float(event["duration"])
        frames = max(1, round(duration * settings.fps))
        motion = event.get("motion", {})
        maximum_scale = float(motion.get("maximum_scale", 1.0))
        if motion.get("type") in {"smooth_push_in", "slow_push_in"} and maximum_scale > 1.0:
            vf = smooth_push_in_filter(
                width=settings.width,
                height=settings.height,
                fps=settings.fps,
                duration=duration,
                frames=frames,
                maximum_scale=maximum_scale,
            )
        else:
            vf = (
                f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={settings.width}:{settings.height},"
                f"tpad=stop_mode=clone:stop_duration={duration},"
                f"fps={settings.fps},format=yuv420p"
            )
        _run(
            [
                str(_ffmpeg()),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                event["asset"]["file"],
                "-vf",
                vf,
                "-frames:v",
                str(frames),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                str(settings.segment_crf),
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(settings.fps),
                "-y",
                str(output),
            ],
            f"Render visual segment {index}/{total}",
        )
        atomic_write_json(
            sidecar,
            {
                "input_fingerprint": fingerprint,
                "event_id": event["event_id"],
                "frames": frames,
                "duration_seconds": frames / settings.fps,
                "output_sha256": sha256_file(output),
            },
        )
        outputs.append(output)
        if index == 1 or index % 10 == 0 or index == total:
            print(f"Visual intermediates: {index}/{total}", flush=True)
    return outputs


def _concat_visuals(
    video_dir: Path,
    segments: list[Path],
    settings: RenderSettings,
) -> Path:
    output_dir = video_dir / "08_render" / "intermediates"
    concat_file = output_dir / "visuals.ffconcat"
    lines = ["ffconcat version 1.0"]
    for path in segments:
        lines.append(f"file '{path.resolve().as_posix()}'")
    atomic_write_text(concat_file, "\n".join(lines) + "\n")
    output = output_dir / "base_visual_1080p.mp4"
    _run(
        [
            str(_ffmpeg()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-vf",
            "setparams=range=limited,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            str(settings.segment_crf),
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-r",
            str(settings.fps),
            "-y",
            str(output),
        ],
        "Normalize and concatenate visual intermediates",
    )
    return output


def _normalize_separators(
    video_dir: Path,
    plan: dict,
    settings: RenderSettings,
) -> list[Path]:
    directory = video_dir / "08_render" / "intermediates" / "separators"
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    for item in plan["separators"]:
        source = Path(item["file"])
        output = directory / f"{int(item['order']):02d}_separator_normalized.mp4"
        fingerprint = json_fingerprint(
            {
                "sha256": item["sha256"],
                "width": settings.width,
                "height": settings.height,
                "fps": settings.fps,
            }
        )
        sidecar = output.with_suffix(".json")
        if output.is_file() and sidecar.is_file():
            cached = load_json(sidecar, "Separator cache")
            if cached.get("input_fingerprint") == fingerprint:
                outputs.append(output)
                continue
        command = [
            str(_ffmpeg()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vf",
            (
                f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease,"
                f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={settings.fps},format=yuv420p"
            ),
            "-c:v",
            "libx264",
            "-preset",
            settings.preset,
            "-crf",
            str(settings.final_crf),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-y",
            str(output),
        ]
        _run(command, f"Normalize separator {item['order']}")
        atomic_write_json(
            sidecar,
            {
                "input_fingerprint": fingerprint,
                "source_file": str(source),
                "output_sha256": sha256_file(output),
            },
        )
        outputs.append(output)
    return outputs


def _ensure_page_flip_overlay(
    video_dir: Path,
    settings: RenderSettings,
) -> Path:
    directory = video_dir / "08_render" / "intermediates" / "transitions"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "page_flip_alpha.mov"
    sidecar = output.with_suffix(".json")
    fingerprint = json_fingerprint(
        {
            "renderer_version": 1,
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "duration": settings.page_flip_duration_seconds,
            "page_color": "0xF2EBDD",
            "fold_width": 48,
        }
    )
    if output.is_file() and sidecar.is_file():
        cached = load_json(sidecar, "Page-flip cache")
        if cached.get("input_fingerprint") == fingerprint:
            return output

    duration = settings.page_flip_duration_seconds
    edge = f"W*(1-T/{duration:.6f})"
    distance = f"({edge}-X)"
    red = (
        f"if(lte(X,{edge}),"
        f"if(lt({distance},48),160+82*{distance}/48,242),0)"
    )
    green = (
        f"if(lte(X,{edge}),"
        f"if(lt({distance},48),150+85*{distance}/48,235),0)"
    )
    blue = (
        f"if(lte(X,{edge}),"
        f"if(lt({distance},48),145+76*{distance}/48,221),0)"
    )
    alpha = f"if(lte(X,{edge}),255,0)"
    _run(
        [
            str(_ffmpeg()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            (
                f"color=c=black@0:s={settings.width}x{settings.height}:"
                f"r={settings.fps}:d={duration:.6f}"
            ),
            "-vf",
            f"format=rgba,geq=r='{red}':g='{green}':b='{blue}':a='{alpha}'",
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            "-y",
            str(output),
        ],
        "Generate transparent page-flip transition",
    )
    atomic_write_json(
        sidecar,
        {
            "input_fingerprint": fingerprint,
            "output_sha256": sha256_file(output),
            "duration_seconds": duration,
        },
    )
    return output


def _compose_final(
    video_dir: Path,
    plan: dict,
    base_visual: Path,
    separators: list[Path],
    settings: RenderSettings,
) -> Path:
    output = video_dir / "08_render" / f"{project_name(plan['video_number'])}_final.mp4"
    command = [
        str(_ffmpeg()),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-filter_complex_threads",
        "1",
        "-i",
        str(base_visual),
    ]
    for path in separators:
        command.extend(["-i", str(path)])
    frame_index = 1 + len(separators)
    frame = plan.get("tv_frame") or {}
    if frame.get("file"):
        command.extend(
            [
                "-loop", "1", "-framerate", str(settings.fps),
                "-t", str(plan["duration_seconds"]), "-i", frame["file"],
            ]
        )
    else:
        frame_index = -1
    narration_start_index = 1 + len(separators) + (1 if frame_index >= 0 else 0)
    for narration in plan["narration"]:
        command.extend(["-i", narration["file"]])
    music_index = narration_start_index + len(plan["narration"])
    music = plan.get("background_music")
    if music and music.get("track_file"):
        command.extend(["-stream_loop", "-1", "-i", music["track_file"]])
    else:
        music_index = -1
    sfx_index = (
        narration_start_index
        + len(plan["narration"])
        + (1 if music_index >= 0 else 0)
    )
    sfx = plan.get("glitch_sfx") or {}
    if sfx.get("file") and plan.get("glitch_events"):
        command.extend(["-i", sfx["file"]])
    else:
        sfx_index = -1

    filters = ["[0:v]setpts=PTS-STARTPTS[vbase]"]
    current = "vbase"
    for index, (item, _) in enumerate(zip(plan["separators"], separators), start=1):
        start = float(item["start"])
        duration = float(item["duration"])
        filters.append(
            f"[{index}:v]trim=duration={duration},setpts=PTS-STARTPTS+{start}/TB[sep{index}]"
        )
        next_label = f"vsep{index}"
        filters.append(
            f"[{current}][sep{index}]overlay=0:0:eof_action=pass:"
            f"enable='between(t,{start:.6f},{start + duration:.6f})'[{next_label}]"
        )
        current = next_label
    glitches = plan.get("glitch_events", [])
    if glitches:
        enable = "+".join(
            f"between(t\\,{float(item['start']):.6f}\\,{float(item['end']):.6f})"
            for item in glitches
        )
        filters.append(
            f"[{current}]noise=alls=6:allf=t+u:enable='{enable}',"
            f"drawbox=y=ih*0.43:w=iw:h=6:color=white@0.12:t=fill:enable='{enable}'[vglitch]"
        )
        current = "vglitch"
    if frame_index >= 0:
        opacity = float(frame.get("opacity", 1.0))
        filters.append(
            f"[{frame_index}:v]scale={settings.width}:{settings.height},format=rgba,"
            r"lut=a='if(gt(val\,0)\,255\,0)',"
            f"colorchannelmixer=aa={opacity}[frame]"
        )
        filters.append(f"[{current}][frame]overlay=0:0:shortest=1[vframed]")
        current = "vframed"
    filters.append(
        f"[{current}]subtitles=filename='{_filter_path(Path(plan['captions']['file']))}',"
        "setparams=range=limited,format=yuv420p[vout]"
    )

    audio_labels = []
    for offset, narration in enumerate(plan["narration"]):
        index = narration_start_index + offset
        delay = round(float(narration["start"]) * 1000)
        duration = float(narration["duration"])
        label = f"voice{offset}"
        filters.append(
            f"[{index}:a]atrim=start=0:end={duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"adelay={delay}|{delay}[{label}]"
        )
        audio_labels.append(f"[{label}]")
    separator_gain = _db_factor(settings.separator_audio_gain_db)
    for offset, item in enumerate(plan["separators"]):
        if not item.get("has_audio"):
            continue
        index = 1 + offset
        delay = round(float(item["start"]) * 1000)
        duration = float(item["duration"])
        label = f"sepaudio{offset}"
        filters.append(
            f"[{index}:a]atrim=start=0:end={duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={separator_gain:.6f},adelay={delay}|{delay}[{label}]"
        )
        audio_labels.append(f"[{label}]")
    if music_index >= 0:
        duration = float(plan["duration_seconds"])
        speech_factor = float(music["ducking"]["speech_gain_factor"])
        filters.append(
            f"[{music_index}:a]atrim=duration={duration},asetpts=PTS-STARTPTS,"
            "aresample=48000,aformat=channel_layouts=stereo,"
            f"loudnorm=I={music['normalization']['target_lufs']}:TP=-2:LRA=7,"
            f"volume={speech_factor:.6f},afade=t=in:st=0:d={music['fade_in_seconds']},"
            f"afade=t=out:st={max(0.0, duration - float(music['fade_out_seconds']))}:"
            f"d={music['fade_out_seconds']}[music]"
        )
        audio_labels.append("[music]")
    if sfx_index >= 0:
        glitches = plan["glitch_events"]
        filters.append(
            f"[{sfx_index}:a]asplit={len(glitches)}"
            + "".join(f"[sfx{index}]" for index in range(len(glitches)))
        )
        sfx_factor = _db_factor(float(sfx.get("gain_db", -15)))
        for index, event in enumerate(glitches):
            delay = round(float(event["start"]) * 1000)
            label = f"sfxdelayed{index}"
            filters.append(
                f"[sfx{index}]aresample=48000,aformat=channel_layouts=stereo,"
                f"volume={sfx_factor:.6f},adelay={delay}|{delay}[{label}]"
            )
            audio_labels.append(f"[{label}]")
    filters.append(
        f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:"
        "dropout_transition=0:normalize=0,"
        f"loudnorm=I={settings.final_target_lufs}:TP={settings.final_true_peak_db}:LRA=11,"
        "alimiter=limit=0.794:level=false[mix]"
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
            str(plan["duration_seconds"]),
            "-r",
            str(settings.fps),
            "-c:v",
            "libx264",
            "-preset",
            settings.preset,
            "-crf",
            str(settings.final_crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
    )
    _run(command, "Compose final master")
    return output


def _render_preview(final: Path, video_dir: Path, settings: RenderSettings) -> Path:
    output = video_dir / "08_render" / f"{video_dir.name}_preview_540p.mp4"
    _run(
        [
            str(_ffmpeg()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(final),
            "-vf",
            f"scale={settings.preview_width}:{settings.preview_height}",
            "-c:v",
            "libx264",
            "-preset",
            settings.preset,
            "-crf",
            str(settings.preview_crf),
            "-pix_fmt",
            "yuv420p",
            "-af",
            "volume=-1dB,alimiter=limit=0.794:level=false",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ],
        "Render low-resolution preview",
    )
    return output


def _compose_video_track(
    video_dir: Path,
    plan: dict,
    base_visual: Path,
    separators: list[Path],
    settings: RenderSettings,
) -> Path:
    output = video_dir / "08_render" / "intermediates" / "composited_video_1080p.mp4"
    command = [
        str(_ffmpeg()), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-filter_complex_threads", "1", "-i", str(base_visual),
    ]
    for path in separators:
        command.extend(["-i", str(path)])
    next_input_index = 1 + len(separators)
    effect_index = -1
    image_tv_effect = plan.get("image_tv_effect") or {}
    if image_tv_effect.get("enabled") and image_tv_effect.get("file"):
        effect_index = next_input_index
        command.extend(
            ["-stream_loop", "-1", "-i", str(image_tv_effect["file"])]
        )
        next_input_index += 1
    page_flip_events = plan.get("page_flip", {}).get("events", [])
    page_flip_index = -1
    if page_flip_events:
        page_flip_index = next_input_index
        page_flip_file = _ensure_page_flip_overlay(video_dir, settings)
        command.extend(["-i", str(page_flip_file)])
        next_input_index += 1
    frame_index = next_input_index
    frame = plan.get("tv_frame") or {}
    if frame.get("file"):
        command.extend(["-i", frame["file"]])
    else:
        frame_index = -1
    filters = ["[0:v]setpts=PTS-STARTPTS[vbase]"]
    current = "vbase"
    glitches = plan.get("glitch_events", [])
    if glitches:
        enable = "+".join(
            f"between(t\\,{float(item['start']):.6f}\\,{float(item['end']):.6f})"
            for item in glitches
        )
        filters.append(
            f"[{current}]noise=alls=6:allf=t+u:enable='{enable}',"
            f"drawbox=y=ih*0.43:w=iw:h=6:color=white@0.12:t=fill:enable='{enable}'[vglitch]"
        )
        current = "vglitch"
    if effect_index >= 0:
        separator_ranges = "+".join(
            f"between(t\\,{float(item['start']):.6f}\\,{float(item['end']):.6f})"
            for item in plan["separators"]
        )
        enable = f"not({separator_ranges})" if separator_ranges else "1"
        opacity = float(image_tv_effect.get("opacity", 1.0))
        filters.append(
            f"[{effect_index}:v]scale={settings.width}:{settings.height}:flags=lanczos,"
            f"format=rgba,colorchannelmixer=aa={opacity},"
            "setpts=PTS-STARTPTS[imageeffect]"
        )
        filters.append(
            f"[{current}][imageeffect]overlay=0:0:eof_action=repeat:repeatlast=1:"
            f"shortest=0:format=auto:enable='{enable}'[veffect]"
        )
        current = "veffect"
    for index, item in enumerate(plan["separators"], start=1):
        start = float(item["start"])
        duration = float(item["duration"])
        filters.append(
            f"[{index}:v]trim=duration={duration},setpts=PTS-STARTPTS+{start}/TB[sep{index}]"
        )
        next_label = f"vsep{index}"
        filters.append(
            f"[{current}][sep{index}]overlay=0:0:eof_action=pass:"
            f"enable='between(t,{start:.6f},{start + duration:.6f})'[{next_label}]"
        )
        current = next_label
    if page_flip_index >= 0:
        if len(page_flip_events) == 1:
            page_sources = [f"{page_flip_index}:v"]
        else:
            page_sources = [f"pageflipsource{index}" for index in range(1, len(page_flip_events) + 1)]
            filters.append(
                f"[{page_flip_index}:v]split={len(page_flip_events)}"
                + "".join(f"[{label}]" for label in page_sources)
            )
        for index, (event, source) in enumerate(
            zip(page_flip_events, page_sources),
            start=1,
        ):
            start = float(event["start"])
            duration = float(event["duration"])
            filters.append(
                f"[{source}]trim=start=0:end={duration:.6f},"
                f"setpts=PTS-STARTPTS+{start:.6f}/TB[pageflip{index}]"
            )
            next_label = f"vpageflip{index}"
            filters.append(
                f"[{current}][pageflip{index}]overlay=0:0:eof_action=pass:"
                f"repeatlast=0:shortest=0:format=auto[{next_label}]"
            )
            current = next_label
    if frame_index >= 0:
        opacity = float(frame.get("opacity", 1.0))
        filters.append(
            f"[{frame_index}:v]scale={settings.width}:{settings.height},format=rgba,"
            r"lut=a='if(gt(val\,0)\,255\,0)',"
            f"colorchannelmixer=aa={opacity}[frame]"
        )
        filters.append(
            f"[{current}][frame]overlay=0:0:eof_action=repeat:repeatlast=1:shortest=0[vframed]"
        )
        current = "vframed"
    for index, title in enumerate(plan.get("story_titles", []), start=1):
        start = float(title["start"])
        end = float(title["end"])
        next_label = f"vtitle{index}"
        filters.append(
            f"[{current}]drawtext=textfile='{_filter_path(Path(title['text_file']))}':"
            f"reload=0:fontcolor=white:fontsize={settings.title_font_size}:"
            "line_spacing=7:box=1:boxcolor=black@0.72:boxborderw=13:"
            f"x={settings.title_margin_left}:y={settings.title_margin_top}:"
            f"enable='between(t\\,{start:.6f}\\,{end:.6f})'[{next_label}]"
        )
        current = next_label
    for index, attribution in enumerate(
        plan.get("source_attributions", []),
        start=1,
    ):
        start = float(attribution["start"])
        end = float(attribution["end"])
        next_label = f"vattribution{index}"
        filters.append(
            f"[{current}]drawtext=textfile='"
            f"{_filter_path(Path(attribution['text_file']))}':reload=0:"
            f"fontcolor=white@0.94:fontsize={settings.attribution_font_size}:"
            "box=1:boxcolor=black@0.68:boxborderw=8:"
            f"x=w-text_w-{settings.attribution_margin_right}:"
            f"y=h-text_h-{settings.attribution_margin_bottom}:"
            f"enable='between(t\\,{start:.6f}\\,{end:.6f})'[{next_label}]"
        )
        current = next_label
    filters.append(
        f"[{current}]subtitles=filename='{_filter_path(Path(plan['captions']['file']))}':"
        "force_style='Alignment=2,"
        f"MarginV={settings.caption_margin_vertical},Outline=2,Shadow=0'[vout]"
    )
    command.extend(
        [
            "-filter_complex", ";".join(filters), "-map", "[vout]",
            "-t", str(plan["duration_seconds"]), "-an", "-r", str(settings.fps),
            "-c:v", "libx264", "-preset", settings.preset,
            "-crf", str(settings.final_crf), "-pix_fmt", "yuv420p",
            "-color_range", "tv",
            "-y", str(output),
        ]
    )
    _run(command, "Compose finite video track")
    return output


def _mix_audio_track(
    video_dir: Path,
    plan: dict,
    separators: list[Path],
    settings: RenderSettings,
) -> Path:
    output = video_dir / "08_render" / "intermediates" / "final_mix.wav"
    command = [
        str(_ffmpeg()), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-filter_complex_threads", "1",
    ]
    for narration in plan["narration"]:
        command.extend(["-i", narration["file"]])
    separator_start_index = len(plan["narration"])
    for path in separators:
        command.extend(["-i", str(path)])
    next_input_index = separator_start_index + len(separators)
    music = plan.get("background_music")
    music_index = -1
    if music and music.get("track_file"):
        music_index = next_input_index
        command.extend(["-stream_loop", "-1", "-i", music["track_file"]])
        next_input_index += 1
    sfx = plan.get("glitch_sfx") or {}
    sfx_index = -1
    if sfx.get("file") and plan.get("glitch_events"):
        sfx_index = next_input_index
        command.extend(["-i", sfx["file"]])
        next_input_index += 1
    timeline_sfx = plan.get("timeline_sfx") or {}
    timeline_sfx_indices: dict[str, int] = {}
    for asset_id, asset in sorted(timeline_sfx.get("assets", {}).items()):
        if not any(
            event.get("sound") == asset_id
            for event in timeline_sfx.get("events", [])
        ):
            continue
        timeline_sfx_indices[asset_id] = next_input_index
        command.extend(["-i", asset["file"]])
        next_input_index += 1
    filters = []
    labels = []
    for index, narration in enumerate(plan["narration"]):
        delay = round(float(narration["start"]) * 1000)
        duration = float(narration["duration"])
        label = f"voice{index}"
        filters.append(
            f"[{index}:a]atrim=start=0:end={duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"adelay={delay}|{delay}[{label}]"
        )
        labels.append(f"[{label}]")
    separator_gain = _db_factor(settings.separator_audio_gain_db)
    for offset, item in enumerate(plan["separators"]):
        if not item.get("has_audio"):
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
    if music_index >= 0:
        duration = float(plan["duration_seconds"])
        speech_factor = float(music["ducking"]["speech_gain_factor"])
        filters.append(
            f"[{music_index}:a]atrim=duration={duration},asetpts=PTS-STARTPTS,"
            "aresample=48000,aformat=channel_layouts=stereo,"
            f"loudnorm=I={music['normalization']['target_lufs']}:TP=-2:LRA=7,"
            f"volume={speech_factor:.6f},afade=t=in:st=0:d={music['fade_in_seconds']},"
            f"afade=t=out:st={max(0.0, duration - float(music['fade_out_seconds']))}:"
            f"d={music['fade_out_seconds']}[music]"
        )
        labels.append("[music]")
    if sfx_index >= 0:
        glitches = plan["glitch_events"]
        filters.append(
            f"[{sfx_index}:a]asplit={len(glitches)}"
            + "".join(f"[sfx{index}]" for index in range(len(glitches)))
        )
        sfx_factor = _db_factor(float(sfx.get("gain_db", -15)))
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
        source_labels: list[str] = []
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
        gain_factor = _db_factor(float(asset.get("gain_db", -12)))
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
            "-filter_complex", ";".join(filters), "-map", "[mix]",
            "-t", str(plan["duration_seconds"]), "-c:a", "pcm_s16le",
            "-ar", "48000", "-ac", "2", "-y", str(output),
        ]
    )
    _run(command, "Mix finite audio track")
    return output


def _mux_master(
    video_dir: Path,
    plan: dict,
    video_track: Path,
    audio_track: Path,
) -> Path:
    output = video_dir / "08_render" / f"{project_name(plan['video_number'])}_final.mp4"
    _run(
        [
            str(_ffmpeg()), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(video_track), "-i", str(audio_track),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", str(plan["duration_seconds"]), "-movflags", "+faststart",
            "-y", str(output),
        ],
        "Mux final master",
    )
    return output


def _qa_output(
    path: Path,
    expected_width: int,
    expected_height: int,
    expected_duration: float,
    settings: RenderSettings,
) -> dict:
    probe = probe_media(path)
    streams = probe.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration = float(probe.get("format", {}).get("duration") or 0)
    video_duration = float(video.get("duration") or 0)
    audio_duration = float(audio.get("duration") or 0)
    volume = _run(
        [
            str(_ffmpeg()), "-hide_banner", "-nostats", "-i", str(path),
            "-map", "0:a:0", "-af", "volumedetect", "-f", "null", os.devnull,
        ],
        f"Measure audio peak for {path.name}",
    )
    peak_match = re.search(
        r"max_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB",
        volume.stderr,
    )
    peak_db = float(peak_match.group(1)) if peak_match else 0.0
    checks = {
        "duration_matches": abs(duration - expected_duration) <= 0.15,
        "video_duration_matches": abs(video_duration - expected_duration) <= 0.15,
        "audio_duration_matches": abs(audio_duration - expected_duration) <= 0.15,
        "resolution_matches": (
            video.get("width") == expected_width and video.get("height") == expected_height
        ),
        "video_codec_h264": video.get("codec_name") == "h264",
        "pixel_format_yuv420p": video.get("pix_fmt") == "yuv420p",
        "audio_codec_aac": audio.get("codec_name") == "aac",
        "audio_sample_rate_48000": str(audio.get("sample_rate")) == "48000",
        "stereo_audio": int(audio.get("channels") or 0) == 2,
        "audio_peak_safe": peak_db <= -1.0,
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "duration_seconds": duration,
        "bytes": path.stat().st_size,
        "audio_sample_peak_db": peak_db,
        "sha256": sha256_file(path),
        "probe": probe,
    }


def render_project(
    video_number: int,
    settings: RenderSettings | None = None,
) -> dict:
    settings = settings or RenderSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    plan = build_render_plan(video_number, settings)
    segments = _render_visual_segments(video_dir, plan, settings)
    base = _concat_visuals(video_dir, segments, settings)
    separators = _normalize_separators(video_dir, plan, settings)
    video_track = _compose_video_track(video_dir, plan, base, separators, settings)
    audio_track = _mix_audio_track(video_dir, plan, separators, settings)
    final = _mux_master(video_dir, plan, video_track, audio_track)
    preview = _render_preview(final, video_dir, settings)
    report = {
        "schema_version": SCHEMA_VERSION,
        "video_number": video_number,
        "created_at_utc": utc_now(),
        "duration_seconds": plan["duration_seconds"],
        "final": {
            "file": str(final),
            "qa": _qa_output(
                final,
                settings.width,
                settings.height,
                float(plan["duration_seconds"]),
                settings,
            ),
        },
        "preview": {
            "file": str(preview),
            "qa": _qa_output(
                preview,
                settings.preview_width,
                settings.preview_height,
                float(plan["duration_seconds"]),
                settings,
            ),
        },
        "intermediates": {
            "visual_segments": len(segments),
            "normalized_separators": len(separators),
            "base_visual": str(base),
            "composited_video": str(video_track),
            "final_mix": str(audio_track),
        },
    }
    report["quality"] = {
        "status": (
            "passed"
            if report["final"]["qa"]["status"] == "passed"
            and report["preview"]["qa"]["status"] == "passed"
            else "failed"
        )
    }
    report_path = video_dir / "08_render" / "render_report.json"
    atomic_write_json(report_path, report)
    state = load_json(video_dir / "state.json", "Stage state")
    state.setdefault("stages", {})[STAGE_NAME] = {
        "status": "FINALIZING_ARCHIVE" if report["quality"]["status"] == "passed" else "FAILED",
        "render_plan": str(video_dir / "08_render" / "render_plan.json"),
        "render_report": str(report_path),
        "final_file": str(final),
        "preview_file": str(preview),
        "input_fingerprint": plan["input_fingerprint"],
        "updated_at_utc": utc_now(),
    }
    state["updated_at_utc"] = state["stages"][STAGE_NAME]["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)
    if report["quality"]["status"] != "passed":
        raise RuntimeError(f"Milestone 8 QA failed: {report}")
    report["news_archive"] = NewsArchive().finalize_project(video_number)
    atomic_write_json(report_path, report)
    state["stages"][STAGE_NAME]["status"] = "RENDER_READY"
    state["stages"][STAGE_NAME]["updated_at_utc"] = utc_now()
    state["updated_at_utc"] = state["stages"][STAGE_NAME]["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)
    return report


def render_preview_only(
    video_number: int,
    settings: RenderSettings | None = None,
) -> dict:
    """Rebuild and re-QA the preview while preserving a passing current master."""
    settings = settings or RenderSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    plan = build_render_plan(video_number, settings)
    final = video_dir / "08_render" / f"{project_name(video_number)}_final.mp4"
    if not final.is_file():
        raise FileNotFoundError(f"Current final master is missing: {final}")
    final_qa = _qa_output(
        final,
        settings.width,
        settings.height,
        float(plan["duration_seconds"]),
        settings,
    )
    if final_qa["status"] != "passed":
        raise RuntimeError("Preview-only recovery requires a passing current master.")
    preview = _render_preview(final, video_dir, settings)
    preview_qa = _qa_output(
        preview,
        settings.preview_width,
        settings.preview_height,
        float(plan["duration_seconds"]),
        settings,
    )
    report_path = video_dir / "08_render" / "render_report.json"
    existing = load_json(report_path, "Render report") if report_path.is_file() else {}
    report = {
        **existing,
        "schema_version": SCHEMA_VERSION,
        "video_number": video_number,
        "created_at_utc": utc_now(),
        "duration_seconds": plan["duration_seconds"],
        "final": {"file": str(final), "qa": final_qa},
        "preview": {"file": str(preview), "qa": preview_qa},
        "quality": {"status": preview_qa["status"]},
    }
    atomic_write_json(report_path, report)
    state = load_json(video_dir / "state.json", "Stage state")
    state.setdefault("stages", {})[STAGE_NAME] = {
        "status": "RENDER_READY" if preview_qa["status"] == "passed" else "FAILED",
        "render_plan": str(video_dir / "08_render" / "render_plan.json"),
        "render_report": str(report_path),
        "final_file": str(final),
        "preview_file": str(preview),
        "input_fingerprint": plan["input_fingerprint"],
        "updated_at_utc": utc_now(),
    }
    state["updated_at_utc"] = state["stages"][STAGE_NAME]["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)
    if preview_qa["status"] != "passed":
        raise RuntimeError(f"Preview QA failed: {preview_qa}")
    return report


def render_status(video_number: int) -> dict:
    video_dir = project_dir(video_number)
    state = load_json(video_dir / "state.json", "Stage state")
    stage = state.get("stages", {}).get(STAGE_NAME, {})
    return {
        "status": stage.get("status", "NOT_STARTED"),
        "final_file": stage.get("final_file"),
        "preview_file": stage.get("preview_file"),
        "render_report": stage.get("render_report"),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Milestone 8 preview and final FFmpeg renders."
    )
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument(
        "--action",
        choices=("plan", "render", "preview", "status"),
        default="render",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.action == "plan":
            result = build_render_plan(args.video)
            print(f"Render plan: {project_dir(args.video) / '08_render' / 'render_plan.json'}")
            print(f"Duration: {result['duration_seconds']:.3f}s")
        elif args.action == "status":
            result = render_status(args.video)
        elif args.action == "preview":
            result = render_preview_only(args.video)
            print(f"Preview: {result['preview']['file']}")
            print("Milestone 8: RENDER_READY")
        else:
            result = render_project(args.video)
            print(f"Final: {result['final']['file']}")
            print(f"Preview: {result['preview']['file']}")
            print("Milestone 8: RENDER_READY")
        if args.json or args.action == "status":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        report_cli_error("RENDER", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
