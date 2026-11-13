from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
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
from file_locations import project_dir, project_name, resolve_portable_path  # noqa: E402
from news_archive import NewsArchive  # noqa: E402
from motion_filters import (  # noqa: E402
    MOTION_RENDERER_VERSION,
    smooth_push_in_filter,
)
from separator_policy import require_separator_checkpoint  # noqa: E402
from tooling import find_executable  # noqa: E402


STAGE_NAME = "render"
SCHEMA_VERSION = 1

TITLE_ASS_PLAY_RES_X = 1600
TITLE_ASS_PLAY_RES_Y = 900
TITLE_ASS_FONT_SIZE = 29
TITLE_ASS_MIN_PAPER_WIDTH = 400
TITLE_ASS_MAX_PAPER_WIDTH = 920
TITLE_ASS_PAPER_HEIGHT = 50
TITLE_ASS_DOUBLE_PAPER_HEIGHT = 80
TITLE_ASS_MAX_TITLE_CHARS = 164
TITLE_ASS_HORIZONTAL_TEXT_PADDING = 50
TITLE_ASS_ENTRANCE_SECONDS = 0.90
TITLE_ASS_EXIT_SECONDS = 1.05


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
    voiceover_gain_db: float = 0.0
    separator_audio_gain_db: float = -9.0
    final_target_lufs: float = -14.0
    final_true_peak_db: float = -1.5
    caption_margin_vertical: int = 24
    title_font_size: int = 30
    title_margin_left: int = 0
    title_margin_top: int = 70
    attribution_font_size: int = 20
    attribution_margin_right: int = 0
    attribution_margin_bottom: int = 8
    page_flip_enabled: bool = True
    page_flip_duration_seconds: float = 0.7
    cleanup_intermediates: bool = True
    segment_workers: int = min(4, max(1, (os.cpu_count() or 2) // 2))
    video_filter_threads: int = min(4, max(1, (os.cpu_count() or 2) // 2))

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
            voiceover_gain_db=float(os.getenv("VOICEOVER_GAIN_DB", "0")),
            separator_audio_gain_db=float(
                os.getenv("SEPARATOR_AUDIO_GAIN_DB", "-9")
            ),
            final_target_lufs=float(os.getenv("FINAL_TARGET_LUFS", "-14")),
            final_true_peak_db=float(os.getenv("FINAL_TRUE_PEAK_DB", "-1.5")),
            caption_margin_vertical=int(
                os.getenv("CAPTION_MARGIN_VERTICAL", "24")
            ),
            title_font_size=int(os.getenv("TITLE_FONT_SIZE", "30")),
            title_margin_left=int(os.getenv("TITLE_MARGIN_LEFT", "0")),
            title_margin_top=int(os.getenv("TITLE_MARGIN_TOP", "70")),
            attribution_font_size=int(
                os.getenv("ATTRIBUTION_FONT_SIZE", "20")
            ),
            attribution_margin_right=int(
                os.getenv("ATTRIBUTION_MARGIN_RIGHT", "0")
            ),
            attribution_margin_bottom=int(
                os.getenv("ATTRIBUTION_MARGIN_BOTTOM", "8")
            ),
            page_flip_enabled=_env_bool("PAGE_FLIP_ENABLED", True),
            page_flip_duration_seconds=float(
                os.getenv("PAGE_FLIP_DURATION_SECONDS", "0.7")
            ),
            cleanup_intermediates=_env_bool("RENDER_CLEANUP_INTERMEDIATES", True),
            segment_workers=int(
                os.getenv(
                    "RENDER_SEGMENT_WORKERS",
                    str(min(4, max(1, (os.cpu_count() or 2) // 2))),
                )
            ),
            video_filter_threads=int(
                os.getenv(
                    "RENDER_VIDEO_FILTER_THREADS",
                    str(min(4, max(1, (os.cpu_count() or 2) // 2))),
                )
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
        if not -24 <= self.voiceover_gain_db <= 12:
            raise ValueError("VOICEOVER_GAIN_DB must be between -24 and 12 dB.")
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
        if not 1 <= self.segment_workers <= 16:
            raise ValueError("RENDER_SEGMENT_WORKERS must be between 1 and 16.")
        if not 1 <= self.video_filter_threads <= 16:
            raise ValueError("RENDER_VIDEO_FILTER_THREADS must be between 1 and 16.")


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise RuntimeError(f"{label} failed: {detail}")
    return result


def _report_render_progress(
    phase: str,
    detail: str,
    *,
    completed: int | None = None,
    total: int | None = None,
) -> None:
    """Emit a machine-readable heartbeat without exposing FFmpeg's noisy output."""
    payload: dict[str, Any] = {"phase": phase, "detail": detail}
    if completed is not None:
        payload["completed"] = completed
    if total is not None:
        payload["total"] = total
    print(
        "NEWS_STUDIO_RENDER_PROGRESS "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


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
    return textwrap.shorten(
        normalized,
        width=TITLE_ASS_MAX_TITLE_CHARS,
        placeholder="...",
    )


def _ass_timestamp(seconds: float) -> str:
    total_centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _ass_escape_text(value: str) -> str:
    return (
        re.sub(r"\s+", " ", value).strip()
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def _story_title_text(item: dict) -> str:
    text = str(item.get("text", "")).strip()
    if not text and item.get("text_file"):
        path = resolve_portable_path(str(item["text_file"]))
        if path.is_file():
            text = path.read_text(encoding="utf-8-sig").strip()
    return _compact_story_title(text or "Tech News", int(item.get("order", 1)))


def _story_title_text_width(text: str) -> float:
    units = 0.0
    for character in text:
        if character.isspace():
            units += 0.32
        elif character in "ilI|!.,'`:;":
            units += 0.27
        elif character in "MW@%&#":
            units += 0.86
        elif character in "mw":
            units += 0.75
        elif character.isupper():
            units += 0.62
        else:
            units += 0.52
    return units * TITLE_ASS_FONT_SIZE * 0.94


def _story_title_lines(text: str) -> list[str]:
    maximum_line_width = TITLE_ASS_MAX_PAPER_WIDTH - TITLE_ASS_HORIZONTAL_TEXT_PADDING
    if _story_title_text_width(text) <= maximum_line_width:
        return [text]

    words = text.split()
    candidates = []
    for split_at in range(1, len(words)):
        first = " ".join(words[:split_at])
        second = " ".join(words[split_at:])
        first_width = _story_title_text_width(first)
        second_width = _story_title_text_width(second)
        if max(first_width, second_width) <= maximum_line_width:
            candidates.append(
                (abs(first_width - second_width), -min(first_width, second_width), first, second)
            )
    if candidates:
        _, _, first, second = min(candidates)
        return [first, second]

    # Extremely long titles are capped to the available two-line canvas without
    # changing the approved font size.
    visible_words: list[str] = []
    for word in words:
        trial = " ".join([*visible_words, word])
        trial_words = trial.split()
        fits = any(
            max(
                _story_title_text_width(" ".join(trial_words[:split_at])),
                _story_title_text_width(" ".join(trial_words[split_at:])),
            )
            <= maximum_line_width
            for split_at in range(1, len(trial_words))
        )
        if len(trial_words) > 1 and not fits:
            break
        visible_words.append(word)
    capped = " ".join(visible_words).rstrip(" .") + "..."
    return _story_title_lines(capped)


def _story_title_paper_width(lines: list[str]) -> int:
    estimated = math.ceil(
        max(_story_title_text_width(line) for line in lines)
        + TITLE_ASS_HORIZONTAL_TEXT_PADDING
    )
    return max(
        TITLE_ASS_MIN_PAPER_WIDTH,
        min(TITLE_ASS_MAX_PAPER_WIDTH, estimated),
    )


def _story_title_font_size(text: str, paper_width: int) -> int:
    return TITLE_ASS_FONT_SIZE


def _torn_paper_ass_path(width: int, height: int = TITLE_ASS_PAPER_HEIGHT) -> str:
    top_y = (6, 10, 5, 9)
    bottom_y = (
        height - 4,
        height + 1,
        height - 3,
        height + 2,
    )
    points: list[tuple[int, int]] = [(5, 9)]
    x = 38
    index = 0
    while x < width - 30:
        points.append((x, top_y[index % len(top_y)]))
        x += 42 + (index % 2) * 3
        index += 1
    points.extend(
        [
            (width - 12, 6),
            (width, 9),
            (width - 2, height - 1),
        ]
    )
    x = width - 34
    index = 0
    while x > 22:
        points.append((x, bottom_y[index % len(bottom_y)]))
        x -= 41 + (index % 2) * 3
        index += 1
    points.extend(
        [
            (11, height - 2),
            (3, height),
        ]
    )
    command = [f"m {points[0][0]} {points[0][1]}"]
    command.extend(f"l {x_value} {y_value}" for x_value, y_value in points[1:])
    return " ".join(command)


def _write_story_title_ass(story_titles: list[dict], output_path: Path) -> dict:
    header = [
        "[Script Info]",
        "Title: Persistent upper-left torn-paper story titles",
        "ScriptType: v4.00+",
        f"PlayResX: {TITLE_ASS_PLAY_RES_X}",
        f"PlayResY: {TITLE_ASS_PLAY_RES_Y}",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            "Style: Headline,Arial,29,&H00141414,&H00141414,&H00000000,"
            "&H00000000,-1,0,0,0,94,100,0,0,1,0,0,7,0,0,0,1"
        ),
        (
            "Style: Quote,Arial,86,&H00FFFFFF,&H00FFFFFF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []
    widths: list[int] = []
    font_sizes: list[int] = []
    paper_heights: list[int] = []

    def add_event(
        layer: int,
        start: float,
        end: float,
        style: str,
        override: str,
        payload: str,
    ) -> None:
        if end <= start:
            return
        events.append(
            "Dialogue: "
            f"{layer},{_ass_timestamp(start)},{_ass_timestamp(end)},{style},,0,0,0,,"
            f"{{{override}}}{payload}"
        )

    for item in story_titles:
        start = max(0.0, float(item.get("start", 0.0)))
        end = max(start, float(item.get("end", start)))
        duration = end - start
        if duration <= 0.01:
            continue
        title = _story_title_text(item)
        lines = _story_title_lines(title)
        text = r"\N".join(_ass_escape_text(line) for line in lines)
        paper_width = _story_title_paper_width(lines)
        font_size = TITLE_ASS_FONT_SIZE
        paper_height = (
            TITLE_ASS_DOUBLE_PAPER_HEIGHT if len(lines) == 2 else TITLE_ASS_PAPER_HEIGHT
        )
        text_y = 75
        widths.append(paper_width)
        font_sizes.append(font_size)
        paper_heights.append(paper_height)
        paper_path = _torn_paper_ass_path(paper_width, paper_height)
        entrance = min(TITLE_ASS_ENTRANCE_SECONDS, duration * 0.25)
        exit_duration = min(TITLE_ASS_EXIT_SECONDS, duration * 0.25)
        entrance_end = start + entrance
        exit_start = max(entrance_end, end - exit_duration)
        entrance_ms = max(1, round(entrance * 1000) - 40)
        exit_ms = max(41, round((end - exit_start) * 1000) - 30)
        travel = paper_width + 60
        positions = {
            "shadow": (42, 68),
            "paper": (36, 62),
            "text": (53, text_y),
            "quote_shadow": (24, 24),
            "quote": (20, 20),
        }

        def entrance_move(name: str) -> str:
            x_value, y_value = positions[name]
            return f"\\an7\\move({x_value - travel},{y_value},{x_value},{y_value},0,{entrance_ms})"

        def hold_position(name: str) -> str:
            x_value, y_value = positions[name]
            return f"\\an7\\pos({x_value},{y_value})"

        def exit_move(name: str) -> str:
            x_value, y_value = positions[name]
            return (
                f"\\an7\\move({x_value},{y_value},{x_value - travel},{y_value},"
                f"40,{exit_ms})"
            )

        add_event(
            0, start, entrance_end, "Headline",
            entrance_move("shadow")
            + r"\c&H00000000&\alpha&H70&\bord0\shad0\fad(160,0)\p1",
            paper_path + r"{\p0}",
        )
        add_event(
            1, start, entrance_end, "Headline",
            entrance_move("paper")
            + r"\c&H00FCFCFC&\bord0\shad0\fad(160,0)\p1",
            paper_path + r"{\p0}",
        )
        add_event(
            2, start, entrance_end, "Headline",
            entrance_move("text") + f"\\fs{font_size}" + r"\fad(180,0)", text,
        )
        add_event(
            2, start, entrance_end, "Quote",
            entrance_move("quote_shadow")
            + r"\c&H00000000&\alpha&H72&\bord0\shad0\fad(180,0)",
            "“",
        )
        add_event(
            3, start, entrance_end, "Quote",
            entrance_move("quote") + r"\fad(180,0)", "“",
        )

        if exit_start > entrance_end:
            add_event(
                0, entrance_end, exit_start, "Headline",
                hold_position("shadow")
                + r"\c&H00000000&\alpha&H70&\bord0\shad0\p1",
                paper_path + r"{\p0}",
            )
            add_event(
                1, entrance_end, exit_start, "Headline",
                hold_position("paper") + r"\c&H00FCFCFC&\bord0\shad0\p1",
                paper_path + r"{\p0}",
            )
            add_event(
                2, entrance_end, exit_start, "Headline",
                hold_position("text") + f"\\fs{font_size}", text,
            )
            add_event(
                2, entrance_end, exit_start, "Quote",
                hold_position("quote_shadow")
                + r"\c&H00000000&\alpha&H72&\bord0\shad0",
                "“",
            )
            add_event(3, entrance_end, exit_start, "Quote", hold_position("quote"), "“")

        add_event(
            0, exit_start, end, "Headline",
            exit_move("shadow")
            + r"\c&H00000000&\alpha&H70&\bord0\shad0\fad(0,190)\p1",
            paper_path + r"{\p0}",
        )
        add_event(
            1, exit_start, end, "Headline",
            exit_move("paper")
            + r"\c&H00FCFCFC&\bord0\shad0\fad(0,190)\p1",
            paper_path + r"{\p0}",
        )
        add_event(
            2, exit_start, end, "Headline",
            exit_move("text") + f"\\fs{font_size}" + r"\fad(0,210)", text,
        )
        add_event(
            2, exit_start, end, "Quote",
            exit_move("quote_shadow")
            + r"\c&H00000000&\alpha&H72&\bord0\shad0\fad(0,210)",
            "“",
        )
        add_event(
            3, exit_start, end, "Quote",
            exit_move("quote") + r"\fad(0,210)", "“",
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output_path, "\n".join([*header, *events]) + "\n")
    return {
        "enabled": bool(events),
        "file": str(output_path),
        "sha256": sha256_file(output_path),
        "style": "upper_left_torn_paper_reference_v1",
        "play_resolution": f"{TITLE_ASS_PLAY_RES_X}x{TITLE_ASS_PLAY_RES_Y}",
        "entrance_seconds": TITLE_ASS_ENTRANCE_SECONDS,
        "exit_seconds": TITLE_ASS_EXIT_SECONDS,
        "minimum_paper_width": min(widths, default=0),
        "maximum_paper_width": max(widths, default=0),
        "minimum_font_size": min(font_sizes, default=0),
        "maximum_font_size": max(font_sizes, default=0),
        "paper_height": max(paper_heights, default=0),
        "minimum_paper_height": min(paper_heights, default=0),
        "maximum_paper_height": max(paper_heights, default=0),
        "two_line_title_count": sum(height > TITLE_ASS_PAPER_HEIGHT for height in paper_heights),
        "title_count": len(widths),
        "coverage": "complete_story_narration_intervals",
    }


def _compact_source_attribution(attribution: dict) -> str:
    creator = re.sub(r"\s+", " ", str(attribution.get("creator", ""))).strip()
    source_url = str(attribution.get("source_url", "")).strip()
    license_name = re.sub(
        r"\s+",
        " ",
        str(attribution.get("license_name", "")),
    ).strip()
    host = urlparse(source_url).netloc.casefold()
    provider = (
        "Unsplash"
        if "unsplash.com" in host
        else "Pexels"
        if "pexels.com" in host
        else host.removeprefix("www.")
    )
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


def _first_caption_starts_at_program_start(timing: dict, captions: dict) -> bool:
    first_caption_start = float(
        captions.get("quality", {}).get("first_caption_start", -1)
    )
    if first_caption_start < 0.0:
        return False
    stories = timing.get("stories") or []
    first_story = stories[0] if stories else {}
    narration_start = float(
        first_story.get("narration", {}).get("start", 0.0)
    )
    # Allow synthetic timestamps at 0.0s as well as acoustic forced-alignment
    # onsets after brief leading breath/silence at the start of the audio.
    if math.isclose(first_caption_start, narration_start, abs_tol=0.01):
        return True
    spans = first_story.get("phrases") or first_story.get("sentences") or []
    if spans:
        first_span = spans[0]
        expected_start = float(
            first_span.get(
                "timeline_start",
                narration_start + float(first_span.get("start", 0.0)),
            )
        )
        if (
            narration_start - 0.01 <= first_caption_start <= narration_start + 1.0
            and math.isclose(first_caption_start, expected_start, abs_tol=0.05)
        ):
            return True
    return narration_start - 0.01 <= first_caption_start <= narration_start + 0.5


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
        asset_dict = event["asset"]
        path = resolve_portable_path(asset_dict["file"], video_dir=video_dir)
        if not path.is_file():
            raise FileNotFoundError(f"Visual asset is missing: {path}")
        asset_dict["file"] = str(path)
        referenced.append({"file": str(path), "sha256": sha256_file(path)})
    separators = []
    if not timing.get("quality", {}).get("manual_separators_skipped", False):
        for story in timing.get("stories", []):
            item = story["separator"]
            path = resolve_portable_path(item["file"], video_dir=video_dir)
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
        path = resolve_portable_path(story["audio_file"], video_dir=video_dir)
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
    caption_file = resolve_portable_path(
        captions.get("combined_ass_file") or captions["combined_srt_file"],
        video_dir=video_dir,
    )
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
    story_title_overlay = _write_story_title_ass(
        titles,
        title_dir / "story_titles_paper.ass",
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
    frame = dict(effects.get("tv_frame") or {})
    if frame.get("enabled") is not True:
        frame = {"enabled": False}
    if frame.get("file"):
        frame["coverage"] = "still_story_images_only"
        frame["excludes"] = [
            "manual_separators",
            "native_video_assets",
            "generated_separator_summary_images",
        ]
    frame_file = (
        resolve_portable_path(frame["file"], video_dir=video_dir)
        if frame.get("file")
        else None
    )
    if frame_file is not None:
        frame["file"] = str(frame_file)
    image_tv_effect = dict(effects.get("image_tv_effect") or {"enabled": False})
    if image_tv_effect.get("enabled"):
        effect_file = resolve_portable_path(
            image_tv_effect.get("file", ""),
            video_dir=video_dir,
        )
        if not effect_file.is_file():
            raise FileNotFoundError(f"Animated TV effect is missing: {effect_file}")
        if sha256_file(effect_file) != image_tv_effect.get("sha256"):
            raise RuntimeError("Animated TV effect changed after effects planning.")
        if image_tv_effect.get("native_alpha") is not True:
            raise RuntimeError("Animated TV effect must preserve native alpha.")
        image_tv_effect["file"] = str(effect_file)
    music = effects.get("audio", {}).get("background_music")
    if isinstance(music, dict):
        music = dict(music)
        if music.get("track_file"):
            music["track_file"] = str(
                resolve_portable_path(music["track_file"], video_dir=video_dir)
            )
        if music.get("tracks"):
            resolved_tracks = []
            for track in music["tracks"]:
                item = dict(track)
                if item.get("track_file"):
                    item["track_file"] = str(
                        resolve_portable_path(item["track_file"], video_dir=video_dir)
                    )
                resolved_tracks.append(item)
            music["tracks"] = resolved_tracks
    sfx = dict(effects.get("audio", {}).get("glitch_sfx", {}))
    if sfx.get("file"):
        sfx["file"] = str(resolve_portable_path(sfx["file"], video_dir=video_dir))
    timeline_sfx = effects.get("audio", {}).get("timeline_sfx", {})
    timeline_sfx_assets = timeline_sfx.get("assets", {})
    for asset_id, asset in timeline_sfx_assets.items():
        path = resolve_portable_path(asset.get("file", ""), video_dir=video_dir)
        if not path.is_file():
            raise FileNotFoundError(f"Timeline SFX asset is missing ({asset_id}): {path}")
        if sha256_file(path) != asset.get("sha256"):
            raise RuntimeError(f"Timeline SFX asset changed after planning: {path}")
        asset["file"] = str(path)
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
        "story_title_overlay": story_title_overlay,
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
            "first_caption_starts_at_program_start": _first_caption_starts_at_program_start(
                timing,
                captions,
            ),
            "no_directional_pans": effects.get("quality", {}).get(
                "no_directional_pans", False
            ),
            "persistent_titles_cover_all_stories": len(titles) == len(narration),
            "paper_title_overlay_covers_all_stories": (
                story_title_overlay["title_count"] == len(narration)
                and story_title_overlay["coverage"]
                == "complete_story_narration_intervals"
            ),
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
    quality_passed = all(
        value
        for key, value in payload["quality"].items()
        if key not in {
            "status",
            "visual_count",
            "persistent_title_count",
            "source_attribution_count",
            "timeline_sfx_event_count",
        }
    )
    payload["quality"]["status"] = "passed" if quality_passed else "failed"
    if not quality_passed:
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


def _render_visual_segment(
    *,
    index: int,
    total: int,
    event: dict,
    output: Path,
    sidecar: Path,
    fingerprint: str,
    settings: RenderSettings,
) -> Path:
    duration = float(event["duration"])
    frames = max(1, round(duration * settings.fps))
    motion = event.get("motion", {})
    maximum_scale = float(motion.get("maximum_scale", 1.0))
    command = [
        str(_ffmpeg()), "-hide_banner", "-loglevel", "error",
        "-i", str(resolve_portable_path(event["asset"]["file"], video_dir=output.parents[2])),
    ]
    if event.get("asset", {}).get("kind") == "video":
        # User clips are visual-only inputs. Trim deterministically to the
        # storyboard interval and pad only a sub-frame/probe shortfall.
        source_start = max(
            0.0,
            float(event.get("asset", {}).get("source_start_seconds", 0) or 0),
        )
        vf = (
            f"trim=start={source_start}:duration={duration},setpts=PTS-STARTPTS,"
            f"scale={settings.width}:{settings.height}:"
            "force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={settings.width}:{settings.height},"
            "tpad=stop_mode=clone:stop_duration=0.2,"
            f"fps={settings.fps},format=yuv420p"
        )
        command.extend(["-vf", vf])
    elif event.get("brand_logo", False):
        logo_width = max(1, round(settings.width * 0.72))
        logo_height = max(1, round(settings.height * 0.52))
        command.extend(
            [
                "-f", "lavfi", "-i",
                (
                    f"color=c=0xe9edf2:s={settings.width}x{settings.height}:"
                    f"r={settings.fps}"
                ),
                "-filter_complex",
                (
                    f"[0:v]scale={logo_width}:{logo_height}:"
                    "force_original_aspect_ratio=decrease:flags=lanczos,"
                    "format=rgba[logo];"
                    "[1:v][logo]overlay=(W-w)/2:(H-h)/2:format=auto,"
                    f"fps={settings.fps},format=yuv420p[vout]"
                ),
                "-map", "[vout]",
            ]
        )
    elif motion.get("type") in {"smooth_push_in", "slow_push_in"} and maximum_scale > 1.0:
        command.extend(
            [
                "-vf",
                smooth_push_in_filter(
                    width=settings.width,
                    height=settings.height,
                    fps=settings.fps,
                    duration=duration,
                    frames=frames,
                    maximum_scale=maximum_scale,
                ),
            ]
        )
    else:
        command.extend(
            [
                "-vf",
                (
                    f"scale={settings.width}:{settings.height}:"
                    "force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={settings.width}:{settings.height},"
                    f"tpad=stop_mode=clone:stop_duration={duration},"
                    f"fps={settings.fps},format=yuv420p"
                ),
            ]
        )
    command.extend(
        [
            "-frames:v", str(frames), "-an", "-c:v", "libx264",
            "-preset", "ultrafast", "-crf", str(settings.segment_crf),
            "-pix_fmt", "yuv420p", "-r", str(settings.fps), "-y", str(output),
        ]
    )
    _run(command, f"Render visual segment {index}/{total}")
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
    return output


def _render_visual_segments(
    video_dir: Path,
    plan: dict,
    settings: RenderSettings,
) -> list[Path]:
    directory = video_dir / "08_render" / "intermediates" / "visuals_1080p"
    directory.mkdir(parents=True, exist_ok=True)
    total = len(plan["visual_events"])
    outputs: list[Path | None] = [None] * total
    jobs: list[tuple[int, dict, Path, Path, str]] = []
    for index, event in enumerate(plan["visual_events"], start=1):
        output = directory / f"{index:04d}_{event['event_id']}.mp4"
        sidecar = output.with_suffix(".json")
        fingerprint = _segment_fingerprint(event, settings)
        if output.is_file() and sidecar.is_file():
            cached = load_json(sidecar, "Visual segment cache")
            if cached.get("input_fingerprint") == fingerprint:
                outputs[index - 1] = output
                continue
        jobs.append((index, event, output, sidecar, fingerprint))

    completed = total - len(jobs)
    _report_render_progress(
        "visuals",
        (
            f"Rendering visual segments ({completed}/{total} cached or complete)."
            if jobs
            else f"All {total} visual segments were reused from cache."
        ),
        completed=completed,
        total=total,
    )
    worker_count = min(settings.segment_workers, max(1, len(jobs)))
    if jobs:
        print(
            f"Rendering {len(jobs)} visual intermediates with "
            f"{worker_count} parallel worker(s).",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    _render_visual_segment,
                    index=index,
                    total=total,
                    event=event,
                    output=output,
                    sidecar=sidecar,
                    fingerprint=fingerprint,
                    settings=settings,
                ): index
                for index, event, output, sidecar, fingerprint in jobs
            }
            for future in as_completed(futures):
                index = futures[future]
                outputs[index - 1] = future.result()
                completed += 1
                if completed == 1 or completed % 5 == 0 or completed == total:
                    print(f"Visual intermediates: {completed}/{total}", flush=True)
                    _report_render_progress(
                        "visuals",
                        f"Rendered visual segments: {completed}/{total}.",
                        completed=completed,
                        total=total,
                    )
    if any(output is None for output in outputs):
        raise RuntimeError("One or more visual segments did not produce an output file.")
    return [output for output in outputs if output is not None]


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


def _cleanup_paths(
    video_dir: Path,
    targets: list[Path],
    *,
    label: str,
) -> dict:
    """Best-effort deletion limited to this project's intermediate root.

    Antivirus, Explorer, or a recently exited FFmpeg process can temporarily retain a
    Windows directory handle. Cleanup happens only after the media has passed QA, so a
    persistent cleanup lock must retain the intermediates for a later run instead of
    turning a valid final render into a failed production.
    """
    intermediate_root = (video_dir / "08_render" / "intermediates").resolve()
    removed_files = 0
    removed_bytes = 0
    deferred_paths: list[str] = []
    errors: list[str] = []
    for target in targets:
        resolved = target.resolve()
        if not resolved.is_relative_to(intermediate_root):
            raise RuntimeError(
                f"Refusing render cleanup outside intermediate root: {resolved}"
            )
        if resolved.is_dir():
            files = [item for item in resolved.rglob("*") if item.is_file()]
            removed_files += len(files)
            for item in files:
                try:
                    removed_bytes += item.stat().st_size
                except OSError:
                    pass
            remove = lambda: shutil.rmtree(resolved)
        elif resolved.is_file():
            removed_files += 1
            try:
                removed_bytes += resolved.stat().st_size
            except OSError:
                pass
            remove = lambda: resolved.unlink()
        else:
            continue
        for attempt in range(4):
            try:
                remove()
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                if attempt < 3:
                    time.sleep((0.10, 0.25, 0.50)[attempt])
                    continue
                deferred_paths.append(str(resolved))
                errors.append(f"{type(exc).__name__}: {exc}")
    print(
        f"Render cleanup ({label}): removed {removed_files} file(s), "
        f"{removed_bytes / (1024 * 1024):.1f} MB"
        + (
            f"; deferred {len(deferred_paths)} locked path(s)"
            if deferred_paths
            else ""
        ),
        flush=True,
    )
    return {
        "label": label,
        "status": "deferred" if deferred_paths else "passed",
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "deferred_paths": deferred_paths,
        "errors": errors,
    }


def _normalize_separators(
    video_dir: Path,
    plan: dict,
    settings: RenderSettings,
) -> list[Path]:
    directory = video_dir / "08_render" / "intermediates" / "separators"
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    for item in plan["separators"]:
        source = resolve_portable_path(item["file"], video_dir=video_dir)
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
        str(settings.video_filter_threads),
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
                "-t", str(plan["duration_seconds"]),
                "-i", str(resolve_portable_path(frame["file"], video_dir=video_dir)),
            ]
        )
    else:
        frame_index = -1
    narration_start_index = 1 + len(separators) + (1 if frame_index >= 0 else 0)
    for narration in plan["narration"]:
        command.extend(["-i", str(resolve_portable_path(narration["file"], video_dir=video_dir))])
    music_index = narration_start_index + len(plan["narration"])
    music = plan.get("background_music")
    if music and music.get("track_file"):
        command.extend(["-stream_loop", "-1", "-i", str(resolve_portable_path(music["track_file"], video_dir=video_dir))])
    else:
        music_index = -1
    sfx_index = (
        narration_start_index
        + len(plan["narration"])
        + (1 if music_index >= 0 else 0)
    )
    sfx = plan.get("glitch_sfx") or {}
    if sfx.get("file") and plan.get("glitch_events"):
        command.extend(["-i", str(resolve_portable_path(sfx["file"], video_dir=video_dir))])
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
        f"[{current}]subtitles=filename='{_filter_path(resolve_portable_path(plan['captions']['file'], video_dir=video_dir))}',"
        "setparams=range=limited,format=yuv420p[vout]"
    )

    audio_labels = []
    voiceover_gain = _db_factor(settings.voiceover_gain_db)
    for offset, narration in enumerate(plan["narration"]):
        index = narration_start_index + offset
        delay = round(float(narration["start"]) * 1000)
        duration = float(narration["duration"])
        label = f"voice{offset}"
        filters.append(
            f"[{index}:a]atrim=start=0:end={duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={voiceover_gain:.6f},"
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
            "-c:a",
            # Reuse the master's safe AAC stream. Re-encoding it can introduce
            # a second round of codec peak overshoot (or require needless gain
            # reduction), while stream copy is lossless and deterministic.
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ],
        "Render low-resolution preview",
    )
    return output


def _outside_ranges_expression(ranges: list[tuple[float, float]]) -> str:
    """Return an FFmpeg expression enabled outside the union of time ranges."""
    ordered = sorted(
        (start, end)
        for start, end in ranges
        if end > start
    )
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if not merged:
        return "1"
    disabled = "+".join(
        f"between(t\\,{start:.6f}\\,{end:.6f})"
        for start, end in merged
    )
    return f"not({disabled})"


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
        "-filter_complex_threads", str(settings.video_filter_threads),
        "-i", str(base_visual),
    ]
    for path in separators:
        command.extend(["-i", str(path)])
    next_input_index = 1 + len(separators)
    effect_index = -1
    image_tv_effect = plan.get("image_tv_effect") or {}
    if image_tv_effect.get("enabled") and image_tv_effect.get("file"):
        effect_index = next_input_index
        command.extend(
            ["-stream_loop", "-1", "-i", str(resolve_portable_path(image_tv_effect["file"], video_dir=video_dir))]
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
        command.extend(["-i", str(resolve_portable_path(frame["file"], video_dir=video_dir))])
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
        excluded_ranges = [
            (float(item["start"]), float(item["end"]))
            for item in plan.get("separators", [])
        ]
        excluded_ranges.extend(
            (
                float(event["timeline_start"]),
                float(event["timeline_end"]),
            )
            for event in plan.get("visual_events", [])
            if event.get("asset", {}).get("kind") == "video"
            or event.get("source_type") == "generated_separator_summary"
        )
        frame_enable = _outside_ranges_expression(excluded_ranges)
        filters.append(
            f"[{frame_index}:v]scale={settings.width}:{settings.height},format=rgba,"
            r"lut=a='if(gt(val\,0)\,255\,0)',"
            f"colorchannelmixer=aa={opacity}[frame]"
        )
        filters.append(
            f"[{current}][frame]overlay=0:0:eof_action=repeat:repeatlast=1:"
            f"shortest=0:enable='{frame_enable}'[vframed]"
        )
        current = "vframed"
    story_titles = plan.get("story_titles", [])
    if story_titles:
        title_overlay = plan.get("story_title_overlay") or {}
        raw_title_ass = str(title_overlay.get("file", "")).strip()
        title_ass_path = (
            resolve_portable_path(raw_title_ass, video_dir=video_dir)
            if raw_title_ass
            else Path("")
        )
        if not raw_title_ass or not title_ass_path.is_file():
            title_overlay = _write_story_title_ass(
                story_titles,
                video_dir / "08_render" / "titles" / "story_titles_paper.ass",
            )
            plan["story_title_overlay"] = title_overlay
            title_ass_path = Path(title_overlay["file"])
        filters.append(
            f"[{current}]subtitles=filename='{_filter_path(title_ass_path)}'[vtitles]"
        )
        current = "vtitles"
    for index, attribution in enumerate(
        plan.get("source_attributions", []),
        start=1,
    ):
        start = float(attribution["start"])
        end = float(attribution["end"])
        next_label = f"vattribution{index}"
        filters.append(
            f"[{current}]drawtext=textfile='"
            f"{_filter_path(resolve_portable_path(attribution['text_file'], video_dir=video_dir))}':reload=0:"
            f"fontcolor=white@0.94:fontsize={settings.attribution_font_size}:"
            "text_align=R:box=1:boxcolor=black@0.68:boxborderw=8:"
            f"x=w-text_w-{settings.attribution_margin_right + 8}:"
            f"y=h-text_h-{settings.attribution_margin_bottom}:"
            f"enable='between(t\\,{start:.6f}\\,{end:.6f})'[{next_label}]"
        )
        current = next_label
    filters.append(
        f"[{current}]subtitles=filename='{_filter_path(resolve_portable_path(plan['captions']['file'], video_dir=video_dir))}':"
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
        command.extend(["-i", str(resolve_portable_path(narration["file"], video_dir=video_dir))])
    separator_start_index = len(plan["narration"])
    for path in separators:
        command.extend(["-i", str(path)])
    next_input_index = separator_start_index + len(separators)
    music = plan.get("background_music")
    music_inputs: list[tuple[int, dict]] = []
    if music:
        playlist = music.get("tracks") or []
        if playlist:
            for track in playlist:
                music_inputs.append((next_input_index, track))
                command.extend(["-stream_loop", "-1", "-i", str(resolve_portable_path(track["track_file"], video_dir=video_dir))])
                next_input_index += 1
        elif music.get("track_file"):
            # Backward compatibility for effects plans made before per-story music.
            music_inputs.append((next_input_index, {
                "track_file": music["track_file"],
                "timeline_start": 0.0,
                "segment_duration_seconds": float(plan["duration_seconds"]),
                "fade_in_seconds": music["fade_in_seconds"],
                "fade_out_seconds": music["fade_out_seconds"],
            }))
            command.extend(["-stream_loop", "-1", "-i", str(resolve_portable_path(music["track_file"], video_dir=video_dir))])
            next_input_index += 1
    sfx = plan.get("glitch_sfx") or {}
    sfx_index = -1
    if sfx.get("file") and plan.get("glitch_events"):
        sfx_index = next_input_index
        command.extend(["-i", str(resolve_portable_path(sfx["file"], video_dir=video_dir))])
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
        command.extend(["-i", str(resolve_portable_path(asset["file"], video_dir=video_dir))])
        next_input_index += 1
    filters = []
    labels = []
    voiceover_gain = _db_factor(settings.voiceover_gain_db)
    for index, narration in enumerate(plan["narration"]):
        delay = round(float(narration["start"]) * 1000)
        duration = float(narration["duration"])
        label = f"voice{index}"
        filters.append(
            f"[{index}:a]atrim=start=0:end={duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={voiceover_gain:.6f},"
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
    if music_inputs:
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
            # FFmpeg 8's default two-loop AAC coder can add roughly 2 dB of
            # sample-peak overshoot to an otherwise safe PCM master. The fast
            # coder preserves the mix level while keeping this encode below the
            # -1 dBFS QA ceiling; the preview then copies this validated stream.
            "-c:a", "aac", "-aac_coder", "fast", "-b:a", "192k", "-ar", "48000",
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
    _report_render_progress(
        "planning",
        "Preview approved. Validating assets and preparing the final render.",
    )
    plan = build_render_plan(video_number, settings)
    segments = _render_visual_segments(video_dir, plan, settings)
    _report_render_progress(
        "visual_track",
        "Combining the rendered segments into the base video track.",
    )
    base = _concat_visuals(video_dir, segments, settings)
    segment_cleanup = {
        "label": "visual_segments",
        "removed_files": 0,
        "removed_bytes": 0,
    }
    if settings.cleanup_intermediates:
        segment_cleanup = _cleanup_paths(
            video_dir,
            [
                video_dir / "08_render" / "intermediates" / "visuals_1080p",
                video_dir / "08_render" / "intermediates" / "visuals.ffconcat",
            ],
            label="visual segments after base animation",
        )
    _report_render_progress(
        "separators",
        "Preparing chapter separators and transitions.",
    )
    separators = _normalize_separators(video_dir, plan, settings)
    _report_render_progress(
        "compositing",
        "Compositing titles, captions, transitions, and visual effects.",
    )
    video_track = _compose_video_track(video_dir, plan, base, separators, settings)
    _report_render_progress(
        "audio",
        "Mixing narration, music, separators, and sound effects.",
    )
    audio_track = _mix_audio_track(video_dir, plan, separators, settings)
    _report_render_progress(
        "master",
        "Creating the final video master.",
    )
    final = _mux_master(video_dir, plan, video_track, audio_track)
    _report_render_progress(
        "preview",
        "Creating the lightweight final preview.",
    )
    preview = _render_preview(final, video_dir, settings)
    _report_render_progress(
        "quality",
        "Running final video and audio quality checks.",
    )
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
            "cleanup_enabled": settings.cleanup_intermediates,
            "segment_cleanup": segment_cleanup,
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
    _report_render_progress(
        "archive",
        "Quality checks passed. Finalizing the news archive.",
    )
    report["news_archive"] = NewsArchive().finalize_project(video_number)
    if settings.cleanup_intermediates:
        report["intermediates"]["final_cleanup"] = _cleanup_paths(
            video_dir,
            [video_dir / "08_render" / "intermediates"],
            label="remaining intermediates after final QA",
        )
        report["intermediates"]["retained"] = bool(
            report["intermediates"]["final_cleanup"]["deferred_paths"]
        )
    else:
        report["intermediates"]["retained"] = True
    atomic_write_json(report_path, report)
    state["stages"][STAGE_NAME]["status"] = "RENDER_READY"
    state["stages"][STAGE_NAME]["updated_at_utc"] = utc_now()
    state["updated_at_utc"] = state["stages"][STAGE_NAME]["updated_at_utc"]
    atomic_write_json(video_dir / "state.json", state)
    _report_render_progress("complete", "Final render is complete.")
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
    if preview_qa["status"] == "passed":
        # The archive gate independently reads this report, so persist the passing
        # media QA before asking it to promote the project's news history.
        atomic_write_json(report_path, report)
        report["news_archive"] = NewsArchive().finalize_project(video_number)
        if settings.cleanup_intermediates:
            report.setdefault("intermediates", {})["final_cleanup"] = _cleanup_paths(
                video_dir,
                [video_dir / "08_render" / "intermediates"],
                label="remaining intermediates after preview recovery",
            )
            report["intermediates"]["retained"] = bool(
                report["intermediates"]["final_cleanup"]["deferred_paths"]
            )
        else:
            report.setdefault("intermediates", {})["retained"] = True
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


def finalize_passing_render(
    video_number: int,
    settings: RenderSettings | None = None,
) -> dict:
    """Finish bookkeeping after an already passing render was interrupted.

    This path never re-encodes media. It is deliberately strict: both outputs must
    retain the QA checksums recorded by the interrupted run and the current render
    plan must have the same input fingerprint.
    """
    settings = settings or RenderSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    plan = build_render_plan(video_number, settings)
    report_path = video_dir / "08_render" / "render_report.json"
    report = load_json(report_path, "Render report")
    state = load_json(video_dir / "state.json", "Stage state")
    render_state = state.get("stages", {}).get(STAGE_NAME, {})
    if render_state.get("input_fingerprint") != plan["input_fingerprint"]:
        raise RuntimeError("Passing render recovery was rejected because inputs changed.")
    if report.get("quality", {}).get("status") != "passed":
        raise RuntimeError("Passing render recovery requires a passing render report.")
    for label in ("final", "preview"):
        item = report.get(label, {})
        qa = item.get("qa", {})
        path = resolve_portable_path(str(item.get("file", "")), video_dir=video_dir)
        if (
            qa.get("status") != "passed"
            or not path.is_file()
            or sha256_file(path) != qa.get("sha256")
        ):
            raise RuntimeError(f"Passing render recovery rejected the {label} output.")

    report["news_archive"] = NewsArchive().finalize_project(video_number)
    if settings.cleanup_intermediates:
        report.setdefault("intermediates", {})["final_cleanup"] = _cleanup_paths(
            video_dir,
            [video_dir / "08_render" / "intermediates"],
            label="remaining intermediates after finalize-only recovery",
        )
        report["intermediates"]["retained"] = bool(
            report["intermediates"]["final_cleanup"]["deferred_paths"]
        )
    else:
        report.setdefault("intermediates", {})["retained"] = True
    atomic_write_json(report_path, report)

    updated = utc_now()
    state.setdefault("stages", {})[STAGE_NAME] = {
        **render_state,
        "status": "RENDER_READY",
        "render_plan": str(video_dir / "08_render" / "render_plan.json"),
        "render_report": str(report_path),
        "final_file": report["final"]["file"],
        "preview_file": report["preview"]["file"],
        "input_fingerprint": plan["input_fingerprint"],
        "updated_at_utc": updated,
    }
    state["updated_at_utc"] = updated
    atomic_write_json(video_dir / "state.json", state)
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
        choices=("plan", "render", "preview", "finalize", "status"),
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
        elif args.action == "finalize":
            result = finalize_passing_render(args.video)
            print(f"Final: {result['final']['file']}")
            print(f"Preview: {result['preview']['file']}")
            print("Milestone 8: RENDER_READY (recovered without re-encoding)")
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
