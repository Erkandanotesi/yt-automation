from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
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

from file_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    json_fingerprint,
    sha256_file,
    sha256_text,
)
from file_locations import project_dir, project_name  # noqa: E402
from separator_policy import require_separator_checkpoint  # noqa: E402


CAPTION_SCHEMA_VERSION = 1
STAGE_NAME = "captions"
TIMESTAMP_PATTERN = re.compile(
    r"^(?P<hours>\d{2,}):(?P<minutes>\d{2}):(?P<seconds>\d{2}),"
    r"(?P<milliseconds>\d{3})$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CaptionSettings:
    max_characters_per_line: int = 42
    max_lines: int = 2
    max_words_per_caption: int = 9
    minimum_duration_seconds: float = 0.35

    @classmethod
    def from_environment(cls) -> "CaptionSettings":
        return cls(
            max_characters_per_line=int(
                os.getenv("CAPTION_MAX_CHARACTERS_PER_LINE", "42")
            ),
            max_lines=int(os.getenv("CAPTION_MAX_LINES", "2")),
            max_words_per_caption=int(
                os.getenv("CAPTION_MAX_WORDS", "9")
            ),
            minimum_duration_seconds=float(
                os.getenv("CAPTION_MIN_DURATION_SECONDS", "0.35")
            ),
        )

    def validate(self) -> None:
        if self.max_characters_per_line < 12:
            raise ValueError(
                "CAPTION_MAX_CHARACTERS_PER_LINE must be at least 12."
            )
        if self.max_lines not in {1, 2}:
            raise ValueError("CAPTION_MAX_LINES must be 1 or 2.")
        if self.max_words_per_caption < 2:
            raise ValueError("CAPTION_MAX_WORDS must be at least 2.")
        if self.minimum_duration_seconds <= 0:
            raise ValueError("CAPTION_MIN_DURATION_SECONDS must be positive.")


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


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def format_srt_timestamp(seconds: float) -> str:
    if seconds < -0.0005:
        raise ValueError("SRT timestamps cannot be negative.")
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return (
        f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},"
        f"{milliseconds:03d}"
    )


def parse_srt_timestamp(value: str) -> float:
    match = TIMESTAMP_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    milliseconds = int(match.group("milliseconds"))
    if minutes > 59 or seconds > 59:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def wrap_caption(text: str, settings: CaptionSettings) -> str:
    clean = normalize_text(text)
    if not clean:
        raise ValueError("Caption text cannot be empty.")
    if len(clean) <= settings.max_characters_per_line:
        return clean
    if settings.max_lines == 1:
        raise ValueError(
            f"Caption exceeds {settings.max_characters_per_line} characters."
        )
    spaces = [index for index, character in enumerate(clean) if character == " "]
    valid = [
        index
        for index in spaces
        if index <= settings.max_characters_per_line
        and len(clean) - index - 1 <= settings.max_characters_per_line
    ]
    if not valid:
        longest_word = max(clean.split(), key=len)
        if len(longest_word) > settings.max_characters_per_line:
            raise ValueError(
                "A caption word is longer than the configured line limit: "
                f"{longest_word}"
            )
        raise ValueError(
            "Caption cannot fit within the configured two-line limit: "
            f"{clean}"
        )
    midpoint = len(clean) / 2
    split = min(valid, key=lambda value: abs(value - midpoint))
    return clean[:split] + "\n" + clean[split + 1 :]


def _fits_caption(text: str, settings: CaptionSettings) -> bool:
    try:
        wrap_caption(text, settings)
    except ValueError:
        return False
    return True


def render_srt(cues: list[dict]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    (
                        f"{format_srt_timestamp(float(cue['start']))} --> "
                        f"{format_srt_timestamp(float(cue['end']))}"
                    ),
                    str(cue["display_text"]),
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def parse_srt(value: str) -> list[dict]:
    normalized = value.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    cues = []
    for expected_index, block in enumerate(
        re.split(r"\n{2,}", normalized),
        start=1,
    ):
        lines = block.splitlines()
        if len(lines) < 3:
            raise ValueError(f"SRT block {expected_index} is incomplete.")
        try:
            actual_index = int(lines[0])
        except ValueError as exc:
            raise ValueError(
                f"SRT block {expected_index} has a non-numeric index."
            ) from exc
        if actual_index != expected_index:
            raise ValueError(
                f"SRT index sequence expected {expected_index}, got {actual_index}."
            )
        if " --> " not in lines[1]:
            raise ValueError(f"SRT block {expected_index} has no timestamp arrow.")
        start_text, end_text = lines[1].split(" --> ", 1)
        text = "\n".join(lines[2:]).strip()
        if not text:
            raise ValueError(f"SRT block {expected_index} has no text.")
        cues.append(
            {
                "index": actual_index,
                "start": parse_srt_timestamp(start_text),
                "end": parse_srt_timestamp(end_text),
                "display_text": text,
                "plain_text": normalize_text(text),
            }
        )
    return cues


def _caption_chunks(
    canonical_text: str,
    words: list[dict],
    phrases: list[dict],
    settings: CaptionSettings,
) -> list[dict]:
    chunks: list[dict] = []
    for phrase in phrases:
        word_start = int(phrase.get("word_start", -1))
        word_end = int(phrase.get("word_end", -1))
        if (
            word_start < 0
            or word_end <= word_start
            or word_end > len(words)
        ):
            raise RuntimeError("Alignment phrase has invalid word boundaries.")
        current: list[dict] = []
        for word in words[word_start:word_end]:
            proposed = [*current, word]
            proposed_text = canonical_text[
                int(proposed[0]["character_start"]) :
                int(proposed[-1]["character_end"])
            ]
            if current and (
                len(proposed) > settings.max_words_per_caption
                or not _fits_caption(proposed_text, settings)
            ):
                chunks.append(_word_chunk(canonical_text, current))
                current = [word]
            else:
                current = proposed
        if current:
            chunks.append(_word_chunk(canonical_text, current))
    return chunks


def _word_chunk(canonical_text: str, words: list[dict]) -> dict:
    first = words[0]
    last = words[-1]
    character_start = int(first["character_start"])
    character_end = int(last["character_end"])
    return {
        "text": canonical_text[character_start:character_end],
        "start": float(first["start"]),
        "end": float(last["end"]),
        "character_start": character_start,
        "character_end": character_end,
        "word_start": int(first["word_index"]),
        "word_end": int(last["word_index"]) + 1,
    }


def _merge_short_chunks(
    chunks: list[dict],
    canonical_text: str,
    settings: CaptionSettings,
) -> list[dict]:
    result: list[dict] = []
    index = 0
    while index < len(chunks):
        current = dict(chunks[index])
        duration = current["end"] - current["start"]
        if duration < settings.minimum_duration_seconds and index + 1 < len(chunks):
            following = chunks[index + 1]
            merged_text = canonical_text[
                current["character_start"] : following["character_end"]
            ]
            if (
                _fits_caption(merged_text, settings)
                and following["word_end"] - current["word_start"]
                <= settings.max_words_per_caption
            ):
                current.update(
                    {
                        "text": merged_text,
                        "end": following["end"],
                        "character_end": following["character_end"],
                        "word_end": following["word_end"],
                    }
                )
                index += 1
        if (
            current["end"] - current["start"]
            < settings.minimum_duration_seconds
            and result
        ):
            previous = result[-1]
            merged_text = canonical_text[
                previous["character_start"] : current["character_end"]
            ]
            if (
                _fits_caption(merged_text, settings)
                and current["word_end"] - previous["word_start"]
                <= settings.max_words_per_caption
            ):
                previous.update(
                    {
                        "text": merged_text,
                        "end": current["end"],
                        "character_end": current["character_end"],
                        "word_end": current["word_end"],
                    }
                )
                index += 1
                continue
        result.append(current)
        index += 1
    return result


def build_story_cues(
    story: dict,
    alignment: dict,
    settings: CaptionSettings,
) -> list[dict]:
    settings.validate()
    if alignment.get("story_id") != story.get("story_id"):
        raise RuntimeError("Alignment story ID does not match story_timing.json.")
    if alignment.get("quality", {}).get("status") != "passed":
        raise RuntimeError(f"Alignment quality failed for {story.get('story_id')}.")
    canonical_text = str(alignment.get("canonical_text", ""))
    text_path = Path(story.get("text_file", ""))
    if not text_path.is_file():
        raise FileNotFoundError(f"Canonical narration not found: {text_path}")
    approved_text = text_path.read_text(encoding="utf-8").strip()
    if (
        normalize_text(canonical_text) != normalize_text(approved_text)
        or alignment.get("text_sha256") != sha256_text(canonical_text)
    ):
        raise RuntimeError(
            f"Alignment text is stale for {story.get('story_id')}."
        )
    chunks = _caption_chunks(
        canonical_text,
        alignment.get("words", []),
        alignment.get("phrases", []),
        settings,
    )
    chunks = _merge_short_chunks(chunks, canonical_text, settings)
    narration_start = float(story["narration"]["start"])
    cues = []
    for chunk in chunks:
        plain_text = normalize_text(chunk["text"])
        cues.append(
            {
                "story_id": story["story_id"],
                "story_order": int(story["order"]),
                "start": round(narration_start + chunk["start"], 6),
                "end": round(narration_start + chunk["end"], 6),
                "local_start": round(chunk["start"], 6),
                "local_end": round(chunk["end"], 6),
                "plain_text": plain_text,
                "display_text": wrap_caption(plain_text, settings),
                "word_start": chunk["word_start"],
                "word_end": chunk["word_end"],
            }
        )
    if not cues:
        raise RuntimeError(f"No caption cues were produced for {story.get('story_id')}.")
    reconstructed = normalize_text(
        canonical_text[
            int(alignment["words"][0]["character_start"]) :
            int(alignment["words"][-1]["character_end"])
        ]
    )
    if normalize_text(" ".join(cue["plain_text"] for cue in cues)) != reconstructed:
        raise RuntimeError(
            f"Caption text reconstruction failed for {story.get('story_id')}."
        )
    return cues


def validate_cues(
    cues: list[dict],
    timing: dict,
    settings: CaptionSettings,
) -> dict:
    if not cues:
        raise RuntimeError("Caption output contains no cues.")
    separators = [
        (
            float(story["separator"]["start"]),
            float(story["separator"]["end"]),
        )
        for story in timing.get("stories", [])
    ]
    separator_overlay_mode = (
        timing.get("separator_timing_mode") == "overlay_at_narration_start"
    )
    errors = []
    previous_end = -1.0
    short_count = 0
    for index, cue in enumerate(cues, start=1):
        start = float(cue["start"])
        end = float(cue["end"])
        if end <= start:
            errors.append(f"Cue {index} has non-positive duration.")
        if start < previous_end - 0.002:
            errors.append(f"Cue {index} overlaps the previous cue.")
        if not separator_overlay_mode and any(
            start < separator_end - 0.002
            and end > separator_start + 0.002
            for separator_start, separator_end in separators
        ):
            errors.append(f"Cue {index} overlaps a separator.")
        lines = str(cue["display_text"]).splitlines()
        if len(lines) > settings.max_lines:
            errors.append(f"Cue {index} has too many lines.")
        for line in lines:
            if len(line) > settings.max_characters_per_line:
                errors.append(f"Cue {index} exceeds the line-length limit.")
        if end - start < settings.minimum_duration_seconds - 0.001:
            short_count += 1
        previous_end = max(previous_end, end)
    timeline_duration = float(timing.get("duration_seconds", 0))
    if cues[-1]["end"] > timeline_duration + 0.01:
        errors.append("The final caption extends beyond the project timeline.")
    return {
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "cue_count": len(cues),
        "first_caption_start": round(float(cues[0]["start"]), 6),
        "last_caption_end": round(float(cues[-1]["end"]), 6),
        "short_caption_count": short_count,
        "no_overlaps": not any("overlap" in error for error in errors),
        "no_separator_overlap": not any(
            "separator" in error for error in errors
        ),
        "separator_overlay_mode": separator_overlay_mode,
        "separator_overlap_allowed": separator_overlay_mode,
    }


def _require_inputs(video_dir: Path) -> tuple[dict, dict, list[dict]]:
    state = load_json(video_dir / "state.json", "Stage state")
    timing = load_json(video_dir / "story_timing.json", "Story timing manifest")
    require_separator_checkpoint(state, timing)
    inputs = []
    for story in timing.get("stories", []):
        alignment_path = Path(story.get("alignment_file", ""))
        text_path = Path(story.get("text_file", ""))
        if not alignment_path.is_file() or not text_path.is_file():
            raise FileNotFoundError(
                f"Caption input is missing for {story.get('story_id')}."
            )
        inputs.append(
            {
                "story": story,
                "alignment": load_json(
                    alignment_path,
                    f"Alignment for {story.get('story_id')}",
                ),
                "alignment_sha256": sha256_file(alignment_path),
                "text_sha256": sha256_file(text_path),
            }
        )
    if not inputs:
        raise RuntimeError("story_timing.json contains no stories.")
    return state, timing, inputs


def _input_fingerprint(
    timing: dict,
    inputs: list[dict],
    settings: CaptionSettings,
) -> str:
    return json_fingerprint(
        {
            "timing": {
                "duration_seconds": timing.get("duration_seconds"),
                "manual_separators_validated_at_utc": timing.get(
                    "manual_separators_validated_at_utc"
                ),
                "stories": [
                    {
                        "story_id": item["story"]["story_id"],
                        "order": item["story"]["order"],
                        "separator": item["story"]["separator"],
                        "narration": item["story"]["narration"],
                        "alignment_sha256": item["alignment_sha256"],
                        "text_sha256": item["text_sha256"],
                    }
                    for item in inputs
                ],
            },
            "settings": asdict(settings),
        }
    )


def _cached_package_current(
    manifest: dict,
    input_fingerprint: str,
) -> bool:
    if (
        manifest.get("input_fingerprint") != input_fingerprint
        or manifest.get("quality", {}).get("status") != "passed"
    ):
        return False
    files = [
        {
            "path": manifest.get("combined_srt_file", ""),
            "sha256": manifest.get("combined_srt_sha256", ""),
        },
        *[
            {
                "path": item.get("srt_file", ""),
                "sha256": item.get("srt_sha256", ""),
            }
            for item in manifest.get("stories", [])
        ],
    ]
    return bool(files) and all(
        Path(item["path"]).is_file()
        and item["sha256"]
        and sha256_file(Path(item["path"])) == item["sha256"]
        for item in files
    )


def _write_state(
    video_dir: Path,
    state: dict,
    manifest_path: Path,
    manifest: dict,
) -> None:
    updated_at = utc_now()
    state.setdefault("stages", {})[STAGE_NAME] = {
        "status": "CAPTIONS_READY",
        "reason": (
            "Plain SRT captions match the approved narration and finalized "
            "timeline offsets."
        ),
        "caption_manifest_file": str(manifest_path),
        "combined_srt_file": manifest["combined_srt_file"],
        "cue_count": manifest["cue_count"],
        "input_fingerprint": manifest["input_fingerprint"],
        "updated_at_utc": updated_at,
    }
    state["updated_at_utc"] = updated_at
    atomic_write_json(video_dir / "state.json", state)


def generate_captions(
    *,
    video_number: int,
    settings: CaptionSettings | None = None,
    force: bool = False,
) -> dict:
    settings = settings or CaptionSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    if not video_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {video_dir}")
    state, timing, inputs = _require_inputs(video_dir)
    output_dir = video_dir / "06_captions"
    manifest_path = output_dir / "captions.json"
    fingerprint = _input_fingerprint(timing, inputs, settings)
    existing = {}
    if manifest_path.is_file():
        try:
            existing = load_json(manifest_path, "Caption manifest")
        except RuntimeError:
            existing = {}
    if not force and _cached_package_current(existing, fingerprint):
        cached = dict(existing)
        cached["run_status"] = "cached"
        _write_state(video_dir, state, manifest_path, cached)
        return cached

    all_cues = []
    story_entries = []
    cue_offset = 0
    for item in inputs:
        story = item["story"]
        story_cues = build_story_cues(
            story,
            item["alignment"],
            settings,
        )
        story_quality = validate_cues(story_cues, timing, settings)
        if story_quality["status"] != "passed":
            raise RuntimeError(
                f"Caption validation failed for {story['story_id']}: "
                f"{story_quality['errors']}"
            )
        story_path = output_dir / f"story_{int(story['order']):02d}.srt"
        story_srt = render_srt(story_cues)
        atomic_write_text(story_path, story_srt)
        story_entries.append(
            {
                "story_id": story["story_id"],
                "order": int(story["order"]),
                "title": story.get("title", ""),
                "cue_start_index": cue_offset + 1,
                "cue_end_index": cue_offset + len(story_cues),
                "cue_count": len(story_cues),
                "first_caption_start": story_quality["first_caption_start"],
                "last_caption_end": story_quality["last_caption_end"],
                "srt_file": str(story_path),
                "srt_sha256": sha256_file(story_path),
                "alignment_file": story["alignment_file"],
                "alignment_sha256": item["alignment_sha256"],
                "text_file": story["text_file"],
                "text_sha256": item["text_sha256"],
                "quality": story_quality,
            }
        )
        all_cues.extend(story_cues)
        cue_offset += len(story_cues)

    quality = validate_cues(all_cues, timing, settings)
    if quality["status"] != "passed":
        raise RuntimeError(f"Combined caption validation failed: {quality['errors']}")
    combined_path = output_dir / "captions.srt"
    atomic_write_text(combined_path, render_srt(all_cues))
    parsed = parse_srt(combined_path.read_text(encoding="utf-8"))
    if len(parsed) != len(all_cues):
        raise RuntimeError("Written SRT cue count does not match the caption plan.")
    if any(
        normalize_text(actual["plain_text"]) != expected["plain_text"]
        for actual, expected in zip(parsed, all_cues, strict=True)
    ):
        raise RuntimeError("Written SRT text differs from the caption plan.")

    manifest = {
        "schema_version": CAPTION_SCHEMA_VERSION,
        "video_number": video_number,
        "video_name": project_name(video_number),
        "created_at_utc": existing.get("created_at_utc", utc_now()),
        "updated_at_utc": utc_now(),
        "run_status": "generated",
        "format": "SubRip",
        "encoding": "UTF-8",
        "input_fingerprint": fingerprint,
        "configuration": asdict(settings),
        "combined_srt_file": str(combined_path),
        "combined_srt_sha256": sha256_file(combined_path),
        "cue_count": len(all_cues),
        "stories": story_entries,
        "quality": {
            **quality,
            "canonical_text_matches": True,
            "timeline_offsets_applied": True,
            "first_and_last_caption_checked_per_story": True,
        },
    }
    atomic_write_json(manifest_path, manifest)
    _write_state(video_dir, state, manifest_path, manifest)
    return manifest


def caption_status(
    *,
    video_number: int,
    settings: CaptionSettings | None = None,
) -> dict:
    settings = settings or CaptionSettings.from_environment()
    settings.validate()
    video_dir = project_dir(video_number)
    manifest_path = video_dir / "06_captions" / "captions.json"
    try:
        _, timing, inputs = _require_inputs(video_dir)
        fingerprint = _input_fingerprint(timing, inputs, settings)
        manifest = load_json(manifest_path, "Caption manifest")
        current = _cached_package_current(manifest, fingerprint)
        return {
            "video_number": video_number,
            "status": "CAPTIONS_READY" if current else "STALE",
            "current": current,
            "cue_count": manifest.get("cue_count", 0),
            "combined_srt_file": manifest.get("combined_srt_file", ""),
            "caption_manifest_file": str(manifest_path),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "video_number": video_number,
            "status": "NOT_READY",
            "current": False,
            "reason": str(exc),
            "caption_manifest_file": str(manifest_path),
        }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate plain SRT captions from approved alignment data."
    )
    parser.add_argument("--video", type=int, required=True)
    parser.add_argument(
        "--action",
        choices=("generate", "status"),
        default="generate",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.video < 1:
            raise ValueError("--video must be 1 or greater.")
        if args.action == "status":
            result = caption_status(video_number=args.video)
            success = result["current"]
        else:
            result = generate_captions(
                video_number=args.video,
                force=args.force,
            )
            result = {
                "video_number": args.video,
                "status": "CAPTIONS_READY",
                "run_status": result["run_status"],
                "cue_count": result["cue_count"],
                "combined_srt_file": result["combined_srt_file"],
                "caption_manifest_file": str(
                    project_dir(args.video)
                    / "06_captions"
                    / "captions.json"
                ),
            }
            success = True
        print(
            json.dumps(result, ensure_ascii=False, indent=2)
            if args.json
            else "\n".join(f"{key}: {value}" for key, value in result.items())
        )
        return 0 if success else 1
    except Exception as exc:  # noqa: BLE001
        print(f"[CAPTION ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
