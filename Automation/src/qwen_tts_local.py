"""Local Qwen3-TTS provider for YouTube Tech News pipeline.

This module wraps Qwen3-TTS 1.7B-CustomVoice running locally on GPU via CUDA.
It produces story voiceovers and metadata compatible with alignment_stage.py
and the rest of the news automation pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
AUTOMATION_ROOT = SOURCE_ROOT.parent
WORKSPACE_ROOT = AUTOMATION_ROOT.parent
PROJECTS_ROOT = WORKSPACE_ROOT / "Projects"
MODELS_ROOT = WORKSPACE_ROOT / "models"
QWEN_VENV_PYTHON = AUTOMATION_ROOT / ".venv_qwen" / "Scripts" / "python.exe"
LOCAL_MODEL_DIR = MODELS_ROOT / "Qwen3-TTS-12Hz-1.7B-CustomVoice"
HF_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"


def bootstrap_qwen_venv_if_needed() -> None:
    """Re-exec inside .venv_qwen if invoked from another Python interpreter."""
    if not QWEN_VENV_PYTHON.is_file():
        return
    current_exe = Path(sys.executable).resolve()
    target_exe = QWEN_VENV_PYTHON.resolve()
    if current_exe != target_exe:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [str(target_exe), str(Path(__file__).resolve())] + sys.argv[1:]
        proc = subprocess.run(cmd, env=env)
        sys.exit(proc.returncode)


if __name__ == "__main__" and "--no-bootstrap" not in sys.argv:
    bootstrap_qwen_venv_if_needed()

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, ConfigDict, Field
import requests

load_dotenv(AUTOMATION_ROOT / ".env")

try:
    from file_locations import project_dir, project_name
    from file_utils import sha256_file, sha256_text
    from tooling import find_executable
except ImportError:
    def project_dir(video_num: int) -> Path:
        return PROJECTS_ROOT / f"Video {video_num}"

    def project_name(video_num: int) -> str:
        return f"Video {video_num}"

    def sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def find_executable(name: str) -> Path | None:
        import shutil
        found = shutil.which(name)
        if found:
            return Path(found)
        ffmpeg_bin = (
            AUTOMATION_ROOT
            / "tools"
            / "ffmpeg"
            / "ffmpeg-8.1.2-essentials_build"
            / "bin"
            / f"{name}.exe"
        )
        return ffmpeg_bin if ffmpeg_bin.is_file() else None


def info(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [Qwen-TTS] {msg}")


def error(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [Qwen-TTS ERROR] {msg}", file=sys.stderr)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    norm = raw.strip().lower()
    if norm in {"1", "true", "yes", "on"}:
        return True
    if norm in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def tech_summary_skeptical_enabled() -> bool:
    """Read live skeptical preference without reloading unrelated env vars."""
    saved = dotenv_values(AUTOMATION_ROOT / ".env").get("NEWS_STUDIO_BE_SKEPTICAL")
    raw = saved if saved is not None else os.getenv("NEWS_STUDIO_BE_SKEPTICAL", "false")
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


LEGACY_HYPE_INSTRUCT = (
    "Speak with clear confidence and an energetic, engaging tech podcast tone."
)
BASELINE_NATURAL_INSTRUCT = (
    "Speak in a natural, warm, conversational tech-journalist voice with "
    "steady pacing, subtle inflection, and clear articulation."
)


def _resolve_default_instruct() -> str:
    configured = os.getenv("QWEN_TTS_INSTRUCTION", "").strip()
    if not configured or configured == LEGACY_HYPE_INSTRUCT:
        return BASELINE_NATURAL_INSTRUCT
    return configured


DEFAULT_SPEAKER = os.getenv("QWEN_TTS_SPEAKER", "aiden").strip() or "aiden"
DEFAULT_INSTRUCT = _resolve_default_instruct()
DEFAULT_LANGUAGE = os.getenv("QWEN_TTS_LANGUAGE", "English").strip() or "English"
MAX_CHUNK_CHARS = _env_int("QWEN_TTS_MAX_CHUNK_CHARS", 340)
DYNAMIC_INSTRUCT_ENABLED = _env_bool("QWEN_TTS_DYNAMIC_INSTRUCT", True)
QWEN_EXPRESSION_PROMPT_VERSION = "qwen-beat-v1"
QWEN_TEMPERATURE = _env_float("QWEN_TTS_TEMPERATURE", 0.65)
QWEN_TOP_P = _env_float("QWEN_TTS_TOP_P", 0.88)
QWEN_TOP_K = _env_int("QWEN_TTS_TOP_K", 40)
QWEN_REPETITION_PENALTY = _env_float("QWEN_TTS_REPETITION_PENALTY", 1.08)
QWEN_SUBTALKER_TEMPERATURE = _env_float("QWEN_TTS_SUBTALKER_TEMPERATURE", 0.70)
QWEN_SUBTALKER_TOP_P = _env_float("QWEN_TTS_SUBTALKER_TOP_P", 0.90)
QWEN_SUBTALKER_TOP_K = _env_int("QWEN_TTS_SUBTALKER_TOP_K", 40)
QWEN_SEED = _env_int("QWEN_TTS_SEED", 42)
PAUSE_NORMAL_SECONDS = _env_float("QWEN_TTS_PAUSE_NORMAL_SEC", 0.26)
PAUSE_EMPHASIS_SECONDS = _env_float("QWEN_TTS_PAUSE_EMPHASIS_SEC", 0.38)
PAUSE_BEAT_SECONDS = _env_float("QWEN_TTS_PAUSE_BEAT_SEC", 0.48)
LEAD_SILENCE_SECONDS = 0.08
TAIL_SILENCE_SECONDS = 0.18
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini").strip()
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "medium").strip().lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview").strip()

_cached_model = None


def get_model():
    global _cached_model
    if _cached_model is not None:
        return _cached_model

    import torch
    from qwen_tts import Qwen3TTSModel

    model_path = str(LOCAL_MODEL_DIR) if LOCAL_MODEL_DIR.is_dir() else HF_MODEL_ID
    info(f"Loading Qwen3-TTS 1.7B-CustomVoice from: {model_path} ...")

    use_cuda = torch.cuda.is_available()
    device = "cuda:0" if use_cuda else "cpu"
    dtype = torch.bfloat16 if (use_cuda and torch.cuda.is_bf16_supported()) else torch.float16

    load_kwargs: dict[str, Any] = {
        "device_map": device,
        "torch_dtype": dtype,
    }
    if use_cuda:
        load_kwargs["attn_implementation"] = "sdpa"

    start = time.time()
    model = Qwen3TTSModel.from_pretrained(model_path, **load_kwargs)
    info(f"Model loaded in {time.time() - start:.2f}s on {device} ({dtype})")
    _cached_model = model
    return model


def summary_files(video_dir: Path) -> list[Path]:
    """Match elevenlabs_tts.summary_files discovery logic."""
    news_dir = video_dir / "Haberler"
    if not news_dir.exists():
        raise FileNotFoundError(f"News directory not found: {news_dir}")

    project_info_path = video_dir / "project_info.json"
    if project_info_path.exists():
        project_info = json.loads(project_info_path.read_text(encoding="utf-8"))
        if project_info.get("status") != "complete":
            raise RuntimeError(
                "News project is incomplete; TTS will not run until project_info.json status is complete."
            )
        story_records = project_info.get("stories", [])
        files = [Path(item["text"]) for item in story_records]
        expected = int(project_info.get("expected_count", len(files)))
        if len(files) != expected:
            raise RuntimeError(
                f"Project manifest contains {len(files)} stories; expected {expected}."
            )
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            raise FileNotFoundError("Missing narration files: " + ", ".join(missing))
        return files

    files = sorted(
        path
        for path in news_dir.glob("*.txt")
        if not path.stem.endswith("-orijinal")
    )
    if not files:
        raise RuntimeError(f"No summary TXT files found in {news_dir}")
    return files


@dataclass(frozen=True)
class StoryChunk:
    index: int
    text: str
    paragraph_index: int
    paragraph_break_after: bool


def _split_long_sentence(sentence: str, max_chars: int) -> list[str]:
    """Split an unusually long single sentence at clause punctuation if needed."""
    clean = sentence.strip()
    if len(clean) <= max_chars:
        return [clean] if clean else []

    clauses = re.split(r"(?<=[,;:\u2014])\s+", clean)
    if len(clauses) == 1:
        words = clean.split()
        parts: list[str] = []
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= max_chars:
                current = f"{current} {word}"
            else:
                parts.append(current)
                current = word
        if current:
            parts.append(current)
        return parts

    parts = []
    current = ""
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        if not current:
            current = clause
        elif len(current) + 1 + len(clause) <= max_chars:
            current = f"{current} {clause}"
        else:
            parts.append(current)
            current = clause
    if current:
        parts.append(current)
    return parts


def split_prosodic_chunks(
    text: str,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[StoryChunk]:
    """Split narration into 1-3 sentence prosodic beats, preserving paragraph breaks."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paragraphs:
        clean = text.strip()
        return [StoryChunk(index=1, text=clean, paragraph_index=0, paragraph_break_after=False)]

    raw_chunks: list[tuple[str, int, bool]] = []
    for para_idx, para in enumerate(paragraphs):
        sentences = [
            part
            for sent in re.split(r"(?<=[.!?])\s+", para)
            if sent.strip()
            for part in _split_long_sentence(sent, max_chars)
        ]
        para_chunks: list[str] = []
        current = ""
        for sent in sentences:
            if not current:
                current = sent
            elif len(current) + 1 + len(sent) <= max_chars:
                current = f"{current} {sent}"
            else:
                para_chunks.append(current)
                current = sent
        if current:
            para_chunks.append(current)

        for chunk_pos, chunk_body in enumerate(para_chunks):
            is_last_in_para = chunk_pos == len(para_chunks) - 1
            has_next_para = para_idx < len(paragraphs) - 1
            raw_chunks.append((chunk_body, para_idx, is_last_in_para and has_next_para))

    return [
        StoryChunk(
            index=idx,
            text=chunk_body,
            paragraph_index=para_idx,
            paragraph_break_after=break_after,
        )
        for idx, (chunk_body, para_idx, break_after) in enumerate(raw_chunks, start=1)
    ]


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split long narration text into natural paragraph/sentence chunks."""
    return [chunk.text for chunk in split_prosodic_chunks(text, max_chars=max_chars)]


ToneLabel = Literal["hook", "factual", "serious", "skeptical", "emphatic", "closing"]
PauseStyle = Literal["normal", "beat", "emphasis"]


class BeatDirection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_index: int = Field(
        description="1-based index matching the input chunk number."
    )
    tone_label: ToneLabel = Field(
        description="Editorial delivery mode for this beat."
    )
    instruct: str = Field(
        description=(
            "Concise 8-18 word acoustic voice direction for Qwen3-TTS CustomVoice "
            "(pace, warmth, pitch, and subtle inflection; no bracket tags or hype)."
        )
    )
    pause_after: PauseStyle = Field(
        description=(
            "Pause feel after this beat: 'normal' for smooth continuation, "
            "'beat' for a topic/paragraph shift, 'emphasis' before a sharp turn or punchline."
        )
    )


class StoryBeatPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beats: list[BeatDirection] = Field(
        description="Ordered list of voice directions covering every input beat_index 1..N."
    )


TONE_PRESET_INSTRUCTS: dict[str, str] = {
    "hook": "Warm, clear, engaging newsroom opening, steady medium pace, natural curiosity.",
    "factual": "Clear, steady, matter-of-fact journalistic delivery, natural conversational cadence.",
    "serious": "Measured, serious, grounded tone, slightly slower pace, calm authority.",
    "skeptical": "Dry, understated, subtly skeptical tone, calm inflection, slight emphasis on contrast.",
    "emphatic": "Focused, quietly emphatic delivery, crisp articulation on key numbers, steady rhythm.",
    "closing": "Calm, firm, reflective concluding tone, slightly unhurried final cadence.",
}

_SERIOUS_BEAT_RE = re.compile(
    r"\b(?:breach|attack|hack|spy|espionage|resign|outage|lawsuit|flaw|"
    r"vulnerab|security|warning|risk|investigation|layoff|fail|critics)\w*\b",
    re.IGNORECASE,
)
_SKEPTICAL_BEAT_RE = re.compile(
    r"\b(?:still|yet|however|meanwhile|alleged|reportedly|declined|no surprise|"
    r"again|basic question|whose interests|uncomfortable|at least)\b|\?",
    re.IGNORECASE,
)
_EMPHATIC_BEAT_RE = re.compile(
    r"(?:\$\d|\b\d+(?:\.\d+)?\s*(?:billion|million|thousand|percent|%)\b)",
    re.IGNORECASE,
)


def sanitize_qwen_instruct(raw_instruct: str, fallback: str) -> str:
    """Normalize an AI-generated Qwen3-TTS instruction into a clean acoustic directive."""
    cleaned = re.sub(r"\[[^\]]{1,40}\]", "", raw_instruct or "")
    cleaned = re.sub(r'^[\s"\'-]+|[\s"\']+$', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 12:
        return fallback
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return cleaned


def build_beat_direction_prompt(
    chunks: list[StoryChunk],
    *,
    skeptical: bool,
) -> str:
    """Build the AI voice-direction prompt for Qwen3-TTS per-beat instruct control."""
    angle_guidance = (
        "Editorial mode is SKEPTICAL: where executives make PR claims, contradictions appear, "
        "or a closing question exposes accountability, use dry, understated, subtly wry delivery "
        "(tone_label='skeptical')—never theatrical sarcasm or shouting."
        if skeptical
        else "Editorial mode is STANDARD: deliver authoritative, warm, approachable tech news—calm "
        "and steady on facts, grounded on risks, and measured on takeaways."
    )
    numbered_chunks = "\n".join(
        f"[{chunk.index}] (para_break_after={str(chunk.paragraph_break_after).lower()}) {chunk.text}"
        for chunk in chunks
    )
    return f"""You are a voice director configuring per-beat delivery instructions for Qwen3-TTS CustomVoice.
Assign a natural, cohesive voice direction to each numbered narration chunk below.

RULES FOR QWEN3-TTS NATURALNESS:
- {angle_guidance}
- Write each `instruct` as a concise 8-18 word acoustic cue describing pace, warmth, pitch, and subtle inflection (e.g. "Warm, clear, engaging newsroom opening, steady medium pace, natural curiosity.").
- NEVER use hype or theatrical acting words like "excited", "dramatic", "shouting", "whispering", or "high-energy podcast". Natural conversational journalism sounds best.
- NEVER insert bracketed tags like [serious] or [pause].
- Keep tone transitions smooth across consecutive chunks—only shift tone when the story beat genuinely shifts (hook -> factual details -> risk/contrast/skeptical turn -> closing takeaway).
- Beat 1 should typically be 'hook' (or 'serious' for urgent breaking crises); the final beat should be 'closing'.
- Use `pause_after='beat'` when `para_break_after=true` or when transitioning to a new story angle; use `'emphasis'` only right before a sharp skeptical turn or key reveal; otherwise `'normal'`.
- Return exactly one BeatDirection entry for every chunk 1..{len(chunks)}.

NARRATION CHUNKS:
{numbered_chunks}
"""


def _extract_openai_response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                return part["text"].strip()
    return ""


def request_ai_beat_plan(
    chunks: list[StoryChunk],
    *,
    skeptical: bool,
) -> StoryBeatPlan:
    """Call OpenAI or Gemini REST APIs for structured per-beat voice directions."""
    prompt = build_beat_direction_prompt(chunks, skeptical=skeptical)
    schema = StoryBeatPlan.model_json_schema()
    system_msg = (
        "You are an expert voice director for conversational tech-news narration. "
        "Produce subtle, natural acoustic cues for Qwen3-TTS."
    )

    if AI_PROVIDER == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured in .env.")
        body: dict[str, Any] = {
            "model": OPENAI_MODEL,
            "input": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "StoryBeatPlan",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        if OPENAI_REASONING_EFFORT and OPENAI_REASONING_EFFORT != "none":
            body["reasoning"] = {"effort": OPENAI_REASONING_EFFORT}
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenAI beat planner failed ({resp.status_code}): {resp.text[:300]}")
        output_text = _extract_openai_response_text(resp.json())
        if not output_text:
            raise RuntimeError("OpenAI beat planner returned empty output.")
        return StoryBeatPlan.model_validate_json(output_text)

    if AI_PROVIDER == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured in .env.")
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "systemInstruction": {"parts": [{"text": system_msg}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini beat planner failed ({resp.status_code}): {resp.text[:300]}")
        candidates = resp.json().get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
        output_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if not output_text:
            raise RuntimeError("Gemini beat planner returned empty output.")
        return StoryBeatPlan.model_validate_json(output_text)

    raise ValueError("AI_PROVIDER must be 'gemini' or 'openai'.")


def fallback_beat_directions(
    chunks: list[StoryChunk],
    *,
    skeptical: bool,
    base_instruct: str = DEFAULT_INSTRUCT,
) -> list[dict[str, Any]]:
    """Deterministic beat tone & pause classifier when AI direction is unavailable."""
    total = len(chunks)
    planned: list[dict[str, Any]] = []
    for chunk in chunks:
        if chunk.index == 1:
            tone: ToneLabel = "hook"
        elif chunk.index == total:
            tone = "closing"
        elif skeptical and _SKEPTICAL_BEAT_RE.search(chunk.text):
            tone = "skeptical"
        elif _SERIOUS_BEAT_RE.search(chunk.text):
            tone = "serious"
        elif _EMPHATIC_BEAT_RE.search(chunk.text):
            tone = "emphatic"
        else:
            tone = "factual"

        instruct = TONE_PRESET_INSTRUCTS.get(tone, base_instruct)
        if chunk.paragraph_break_after:
            pause_after: PauseStyle = "beat"
        elif tone in {"skeptical", "emphatic"}:
            pause_after = "emphasis"
        else:
            pause_after = "normal"

        planned.append(
            {
                "index": chunk.index,
                "text": chunk.text,
                "tone_label": tone,
                "instruct": instruct,
                "pause_after": pause_after,
                "paragraph_break_after": chunk.paragraph_break_after,
            }
        )
    return planned


def plan_story_beats(
    text: str,
    *,
    base_instruct: str = DEFAULT_INSTRUCT,
    dynamic_instruct: bool = DYNAMIC_INSTRUCT_ENABLED,
    max_chars: int = MAX_CHUNK_CHARS,
) -> tuple[list[dict[str, Any]], str]:
    """Plan per-beat voice instructions while keeping every chunk's text verbatim."""
    chunks = split_prosodic_chunks(text, max_chars=max_chars)
    if not dynamic_instruct:
        return (
            [
                {
                    "index": chunk.index,
                    "text": chunk.text,
                    "tone_label": "static",
                    "instruct": base_instruct,
                    "pause_after": "beat" if chunk.paragraph_break_after else "normal",
                    "paragraph_break_after": chunk.paragraph_break_after,
                }
                for chunk in chunks
            ],
            "static",
        )

    skeptical = tech_summary_skeptical_enabled()
    heuristic = fallback_beat_directions(
        chunks,
        skeptical=skeptical,
        base_instruct=base_instruct,
    )

    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            plan = request_ai_beat_plan(chunks, skeptical=skeptical)
            by_index = {beat.beat_index: beat for beat in plan.beats}
            merged: list[dict[str, Any]] = []
            for item, chunk in zip(heuristic, chunks, strict=True):
                ai_beat = by_index.get(chunk.index)
                if ai_beat is None:
                    merged.append(item)
                    continue
                pause_after: PauseStyle = (
                    "beat"
                    if chunk.paragraph_break_after and ai_beat.pause_after == "normal"
                    else ai_beat.pause_after
                )
                merged.append(
                    {
                        "index": chunk.index,
                        "text": chunk.text,
                        "tone_label": ai_beat.tone_label,
                        "instruct": sanitize_qwen_instruct(ai_beat.instruct, item["instruct"]),
                        "pause_after": pause_after,
                        "paragraph_break_after": chunk.paragraph_break_after,
                    }
                )
            return merged, "ai"
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(1.0)

    info(f"AI beat direction skipped; using heuristic beat cues ({last_error})")
    return heuristic, "heuristic"


def qwen_generation_kwargs() -> dict[str, Any]:
    """Return balanced, low-variance sampling kwargs for Qwen3-TTS CustomVoice."""
    return {
        "do_sample": True,
        "temperature": QWEN_TEMPERATURE,
        "top_p": QWEN_TOP_P,
        "top_k": QWEN_TOP_K,
        "repetition_penalty": QWEN_REPETITION_PENALTY,
        "subtalker_dosample": True,
        "subtalker_temperature": QWEN_SUBTALKER_TEMPERATURE,
        "subtalker_top_p": QWEN_SUBTALKER_TOP_P,
        "subtalker_top_k": QWEN_SUBTALKER_TOP_K,
    }


def seed_qwen_generator(seed: int = QWEN_SEED) -> None:
    """Seed torch RNG before each story so timbre and cadence stay consistent across chunks."""
    if seed <= 0:
        return
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def prepare_chunk_for_qwen_speech(text: str) -> str:
    """Clean typography for natural Qwen3-TTS phrasing without altering spoken words."""
    cleaned = text.replace("\u2019", "'").replace("\u2018", "'")
    cleaned = cleaned.replace("\u201c", '"').replace("\u201d", '"')
    cleaned = re.sub(r"\s*\u2014\s*", " \u2014 ", cleaned)
    return re.sub(r" {2,}", " ", cleaned).strip()


def pause_seconds_for_beat(pause_after: str) -> float:
    """Map beat pause_after label to natural silence duration in seconds."""
    mapping = {
        "normal": PAUSE_NORMAL_SECONDS,
        "emphasis": PAUSE_EMPHASIS_SECONDS,
        "beat": PAUSE_BEAT_SECONDS,
    }
    return max(0.0, mapping.get(pause_after, PAUSE_NORMAL_SECONDS))


def trim_edge_silence(
    wav: Any,
    sample_rate: int,
    *,
    threshold_db: float = -45.0,
    keep_lead_ms: float = 35.0,
    keep_tail_ms: float = 55.0,
) -> Any:
    """Trim excess leading/trailing dead air while preserving consonant onsets and tails."""
    import numpy as np

    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    if arr.size == 0 or sample_rate <= 0:
        return arr

    peak = float(np.max(np.abs(arr)))
    if peak < 1e-6:
        return arr

    threshold = max(peak * (10.0 ** (threshold_db / 20.0)), 1e-4)
    active = np.flatnonzero(np.abs(arr) >= threshold)
    if active.size == 0:
        return arr

    lead_samples = int(round(sample_rate * (keep_lead_ms / 1000.0)))
    tail_samples = int(round(sample_rate * (keep_tail_ms / 1000.0)))
    start = max(0, int(active[0]) - lead_samples)
    end = min(arr.size, int(active[-1]) + 1 + tail_samples)
    return arr[start:end]


def apply_edge_fades(
    wav: Any,
    sample_rate: int,
    *,
    fade_in_ms: float = 12.0,
    fade_out_ms: float = 15.0,
) -> Any:
    """Apply smooth raised-cosine edge fades so chunk joins never click or pop."""
    import numpy as np

    arr = np.asarray(wav, dtype=np.float32).copy()
    if arr.size == 0 or sample_rate <= 0:
        return arr

    fade_in = min(arr.size // 2, int(round(sample_rate * (fade_in_ms / 1000.0))))
    fade_out = min(arr.size // 2, int(round(sample_rate * (fade_out_ms / 1000.0))))

    if fade_in > 0:
        t = np.linspace(0.0, np.pi, fade_in, dtype=np.float32)
        arr[:fade_in] *= 0.5 * (1.0 - np.cos(t))
    if fade_out > 0:
        t = np.linspace(0.0, np.pi, fade_out, dtype=np.float32)
        arr[-fade_out:] *= 0.5 * (1.0 + np.cos(t))
    return arr


def _active_speech_rms(wav: Any) -> float:
    import numpy as np

    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return 0.0
    peak = float(np.max(np.abs(arr)))
    if peak < 1e-6:
        return 0.0
    gate = peak * 0.02  # ~ -34 dB below chunk peak to ignore pause air
    voiced = arr[np.abs(arr) >= gate]
    samples = voiced if voiced.size > 0 else arr
    return float(np.sqrt(np.mean(samples * samples)))


def match_chunk_loudness(
    waveforms: list[Any],
    *,
    max_gain_db: float = 4.5,
    peak_limit: float = 0.95,
) -> list[Any]:
    """Gently align active RMS loudness across chunks to prevent volume jumps."""
    import numpy as np

    if not waveforms:
        return []
    rms_values = [_active_speech_rms(w) for w in waveforms]
    positive = [val for val in rms_values if val > 1e-6]
    if not positive:
        return [np.asarray(w, dtype=np.float32) for w in waveforms]

    target_rms = float(np.median(positive))
    max_gain = 10.0 ** (max_gain_db / 20.0)
    min_gain = 1.0 / max_gain

    balanced: list[Any] = []
    for wav, rms in zip(waveforms, rms_values, strict=True):
        arr = np.asarray(wav, dtype=np.float32)
        if rms <= 1e-6:
            balanced.append(arr)
            continue
        gain = max(min_gain, min(max_gain, target_rms / rms))
        scaled = arr * gain
        peak = float(np.max(np.abs(scaled))) if scaled.size else 0.0
        if peak > peak_limit:
            scaled = scaled * (peak_limit / peak)
        balanced.append(scaled)
    return balanced


def stitch_beat_waveforms(
    raw_waveforms: list[Any],
    pause_styles: list[str],
    sample_rate: int,
) -> Any:
    """Trim dead air, match loudness, apply edge fades, and insert beat-aware pauses."""
    import numpy as np

    if not raw_waveforms:
        return np.zeros(0, dtype=np.float32)

    trimmed = [trim_edge_silence(w, sample_rate) for w in raw_waveforms]
    leveled = match_chunk_loudness(trimmed)
    faded = [apply_edge_fades(w, sample_rate) for w in leveled]

    segments: list[Any] = []
    if LEAD_SILENCE_SECONDS > 0:
        segments.append(np.zeros(int(round(sample_rate * LEAD_SILENCE_SECONDS)), dtype=np.float32))

    for idx, wav in enumerate(faded):
        segments.append(wav)
        if idx < len(faded) - 1:
            pause_sec = pause_seconds_for_beat(pause_styles[idx])
            if pause_sec > 0:
                segments.append(np.zeros(int(round(sample_rate * pause_sec)), dtype=np.float32))

    if TAIL_SILENCE_SECONDS > 0:
        segments.append(np.zeros(int(round(sample_rate * TAIL_SILENCE_SECONDS)), dtype=np.float32))

    return np.concatenate(segments) if len(segments) > 1 else segments[0]


def tts_input_fingerprint(
    text: str,
    speaker: str,
    instruct: str,
    language: str,
    *,
    dynamic_instruct: bool = DYNAMIC_INSTRUCT_ENABLED,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
) -> str:
    payload = {
        "provider": "qwen3-tts",
        "model": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "speaker": speaker,
        "instruct": instruct,
        "language": language,
        "dynamic_instruct": dynamic_instruct,
        "expression_prompt_version": QWEN_EXPRESSION_PROMPT_VERSION,
        "skeptical": tech_summary_skeptical_enabled() if dynamic_instruct else False,
        "max_chunk_chars": max_chunk_chars,
        "generation": qwen_generation_kwargs(),
        "seed": QWEN_SEED,
        "pauses": {
            "normal": PAUSE_NORMAL_SECONDS,
            "emphasis": PAUSE_EMPHASIS_SECONDS,
            "beat": PAUSE_BEAT_SECONDS,
        },
        "text_sha256": sha256_text(text),
    }
    return sha256_text(json.dumps(payload, sort_keys=True))


def probe_audio_file(path: Path) -> dict[str, Any]:
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        import soundfile as sf
        info_sf = sf.info(str(path))
        return {
            "duration_seconds": round(info_sf.duration, 3),
            "codec": info_sf.format.lower(),
            "sample_rate": info_sf.samplerate,
            "channels": info_sf.channels,
        }

    completed = subprocess.run(
        [
            str(ffprobe),
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
            "-of", "json",
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
        raise RuntimeError(f"FFprobe rejected {path.name}: {completed.stderr.strip()}")
    data = json.loads(completed.stdout)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or stream.get("duration") or 0.0)
    return {
        "duration_seconds": round(duration, 3),
        "codec": stream.get("codec_name", ""),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }


def convert_wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to encode MP3 output.")
    cmd = [
        str(ffmpeg),
        "-y",
        "-i", str(wav_path),
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "1",
        str(mp3_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg MP3 conversion failed: {res.stderr}")


def synthesize_text(
    text: str,
    output_path: Path,
    speaker: str = DEFAULT_SPEAKER,
    instruct: str = DEFAULT_INSTRUCT,
    language: str = DEFAULT_LANGUAGE,
    *,
    dynamic_instruct: bool | None = None,
) -> dict[str, Any]:
    import soundfile as sf

    model = get_model()
    use_dynamic = (
        DYNAMIC_INSTRUCT_ENABLED if dynamic_instruct is None else dynamic_instruct
    )
    if instruct != DEFAULT_INSTRUCT and dynamic_instruct is None:
        use_dynamic = False

    beats, instruct_source = plan_story_beats(
        text,
        base_instruct=instruct,
        dynamic_instruct=use_dynamic,
    )
    info(
        f"  Beat plan ({instruct_source}, {len(beats)} beat(s)): "
        + ", ".join(b["tone_label"] for b in beats)
    )

    gen_kwargs = qwen_generation_kwargs()
    seed_qwen_generator(QWEN_SEED)

    start = time.time()
    raw_waveforms: list[Any] = []
    pause_styles: list[str] = []
    sample_rate = 24000

    for i, beat in enumerate(beats):
        spoken_chunk = prepare_chunk_for_qwen_speech(beat["text"])
        info(
            f"  [beat {i + 1}/{len(beats)}] {beat['tone_label']} "
            f"(pause={beat['pause_after']}, {len(spoken_chunk)} chars) -> {beat['instruct']}"
        )
        wavs, sr = model.generate_custom_voice(
            text=spoken_chunk,
            language=language,
            speaker=speaker,
            instruct=beat["instruct"],
            **gen_kwargs,
        )
        sample_rate = sr
        raw_waveforms.append(wavs[0])
        pause_styles.append(beat["pause_after"])

    audio_data = stitch_beat_waveforms(raw_waveforms, pause_styles, sample_rate)
    gen_time = time.time() - start

    duration = len(audio_data) / sample_rate if sample_rate > 0 else 0.0
    rtf = gen_time / duration if duration > 0 else 0.0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_wav = output_path.with_suffix(".temp.wav")
    sf.write(str(temp_wav), audio_data, sample_rate)

    try:
        if output_path.suffix.lower() == ".mp3":
            temp_mp3 = output_path.with_suffix(".temp.mp3")
            convert_wav_to_mp3(temp_wav, temp_mp3)
            temp_mp3.replace(output_path)
        else:
            temp_wav.replace(output_path)
    finally:
        if temp_wav.exists():
            temp_wav.unlink()

    probed = probe_audio_file(output_path)
    probed["synthesis_seconds"] = round(gen_time, 2)
    probed["real_time_factor"] = round(rtf, 3)
    probed["chunks"] = len(beats)
    probed["instruct_source"] = instruct_source
    probed["dynamic_instruct"] = use_dynamic
    probed["beat_directions"] = [
        {
            "index": beat["index"],
            "tone_label": beat["tone_label"],
            "instruct": beat["instruct"],
            "pause_after": beat["pause_after"],
            "characters": len(beat["text"]),
        }
        for beat in beats
    ]
    return probed


def synthesize_video(
    video_number: int,
    overwrite: bool = False,
    speaker: str = DEFAULT_SPEAKER,
    output_dir: Path | None = None,
) -> int:
    video_dir = project_dir(video_number)
    audio_dir = output_dir.resolve() if output_dir is not None else (video_dir / "Sesler")
    audio_dir.mkdir(parents=True, exist_ok=True)

    files = summary_files(video_dir)

    previous_by_name: dict[str, dict[str, Any]] = {}
    prev_info_path = (video_dir / "Sesler") / "tts_info.json"
    if prev_info_path.is_file():
        try:
            prev_payload = json.loads(prev_info_path.read_text(encoding="utf-8"))
            for item in prev_payload.get("files", []):
                if isinstance(item, dict) and item.get("text_file"):
                    previous_by_name[Path(item["text_file"]).name] = item
        except (OSError, json.JSONDecodeError):
            pass

    info(f"Synthesizing {len(files)} story(ies) for Video {video_number} using Qwen3-TTS (speaker: {speaker})")

    results: list[dict[str, Any]] = []
    total_audio_seconds = 0.0

    for idx, text_path in enumerate(files, start=1):
        target_mp3 = audio_dir / f"{text_path.stem}.mp3"
        text = text_path.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"Summary file is empty: {text_path}")

        text_checksum = sha256_text(text)
        fingerprint = tts_input_fingerprint(text, speaker, DEFAULT_INSTRUCT, DEFAULT_LANGUAGE)
        prev_entry = previous_by_name.get(text_path.name)

        use_cached = (
            target_mp3.exists()
            and not overwrite
            and prev_entry is not None
            and prev_entry.get("input_fingerprint") == fingerprint
            and prev_entry.get("audio_sha256") == sha256_file(target_mp3)
        )

        if use_cached:
            info(f"[{idx}/{len(files)}] Cached audio kept: {target_mp3.name}")
            media = probe_audio_file(target_mp3)
            results.append({
                "story_index": idx,
                "text_file": str(text_path),
                "audio_file": str(target_mp3),
                "status": "cached",
                "characters": len(text),
                "text_sha256": text_checksum,
                "input_fingerprint": fingerprint,
                "audio_sha256": prev_entry["audio_sha256"],
                **media,
            })
            total_audio_seconds += media.get("duration_seconds", 0.0)
            continue

        existed_before = target_mp3.exists()
        info(f"[{idx}/{len(files)}] Generating: {text_path.name} ({len(text)} chars)")
        media = synthesize_text(
            text=text,
            output_path=target_mp3,
            speaker=speaker,
        )
        audio_checksum = sha256_file(target_mp3)
        total_audio_seconds += media.get("duration_seconds", 0.0)
        info(
            f"  Done: {media.get('duration_seconds')}s in {media.get('synthesis_seconds')}s "
            f"(RTF: {media.get('real_time_factor')}x, chunks: {media.get('chunks')})"
        )

        results.append({
            "story_index": idx,
            "text_file": str(text_path),
            "audio_file": str(target_mp3),
            "status": "regenerated" if existed_before else "generated",
            "cache_miss_reason": "forced_overwrite" if overwrite else "missing_or_changed",
            "characters": len(text),
            "text_sha256": text_checksum,
            "input_fingerprint": fingerprint,
            "audio_sha256": audio_checksum,
            **media,
        })

    tts_info = {
        "schema_version": 2,
        "video": project_name(video_number),
        "provider": "qwen3-tts",
        "model_id": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice_id": speaker,
        "voice_name": speaker,
        "output_format": "mp3_44100_192",
        "settings": {
            "speaker": speaker,
            "instruct": DEFAULT_INSTRUCT,
            "language": DEFAULT_LANGUAGE,
            "dynamic_instruct": DYNAMIC_INSTRUCT_ENABLED,
            "expression_prompt_version": QWEN_EXPRESSION_PROMPT_VERSION,
            "max_chunk_chars": MAX_CHUNK_CHARS,
            "generation": qwen_generation_kwargs(),
            "seed": QWEN_SEED,
            "pauses": {
                "normal": PAUSE_NORMAL_SECONDS,
                "emphasis": PAUSE_EMPHASIS_SECONDS,
                "beat": PAUSE_BEAT_SECONDS,
            },
        },
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_audio_seconds": round(total_audio_seconds, 2),
        "files": results,
    }

    info_path = audio_dir / "tts_info.json"
    tmp_info = info_path.with_suffix(".json.tmp")
    tmp_info.write_text(json.dumps(tts_info, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_info.replace(info_path)
    info(f"Audio stage complete! tts_info saved to: {info_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen3-TTS Local Voiceover Engine")
    parser.add_argument("--video", type=int, help="Video number to synthesize.")
    parser.add_argument("--output-dir", type=str, default=None, help="Custom output directory.")
    parser.add_argument("--file", type=str, help="Single text file to synthesize.")
    parser.add_argument("--output", type=str, help="Output path for --file.")
    parser.add_argument("--speaker", type=str, default=DEFAULT_SPEAKER, help=f"Speaker name (default: {DEFAULT_SPEAKER}).")
    parser.add_argument("--overwrite", action="store_true", help="Force overwrite existing audio.")
    parser.add_argument("--list-speakers", action="store_true", help="List available speakers.")
    parser.add_argument("--no-bootstrap", action="store_true", help="Internal flag to prevent recursion.")

    args = parser.parse_args()

    if args.list_speakers:
        model = get_model()
        speakers = model.get_supported_speakers()
        print(f"Supported Speakers: {', '.join(speakers or [])}")
        return 0

    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            error(f"Input file not found: {file_path}")
            return 1
        output_path = Path(args.output) if args.output else file_path.with_suffix(".mp3")
        text = file_path.read_text(encoding="utf-8").strip()
        info(f"Synthesizing {file_path} -> {output_path}")
        media = synthesize_text(text, output_path, speaker=args.speaker)
        print(f"Generated: {output_path} ({media['duration_seconds']}s, RTF: {media['real_time_factor']}x)")
        return 0

    if args.video is not None:
        out_dir = Path(args.output_dir) if args.output_dir else None
        return synthesize_video(args.video, overwrite=args.overwrite, speaker=args.speaker, output_dir=out_dir)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
