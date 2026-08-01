from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from alignment_utils import (
    alignment_input_fingerprint,
    build_alignment_artifact,
    merge_timestamp_alignments,
)
from file_utils import (
    atomic_write_bytes,
    atomic_write_json,
    json_fingerprint,
    report_cli_error,
    sha256_file,
    sha256_text,
    stable_story_id,
)
from tooling import find_executable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_locations import (  # noqa: E402 - .env must be loaded first
    AUTOMATION_ROOT,
    project_dir,
    project_name,
)

def env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be true/false, yes/no, on/off, or 1/0."
    )


def env_text(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


API_BASE = "https://api.elevenlabs.io"
VOICE_ID = env_text(
    "ELEVENLABS_VOICE_ID",
    "pNInz6obpgDQGcFmaJgB",
)
MODEL_ID = env_text("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
OUTPUT_FORMAT = env_text("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
SPEED = env_float("ELEVENLABS_SPEED", 1.0)
STABILITY = env_float("ELEVENLABS_STABILITY", 0.5)
SIMILARITY_BOOST = env_float("ELEVENLABS_SIMILARITY_BOOST", 0.75)
STYLE = env_float("ELEVENLABS_STYLE", 0.5)
SPEAKER_BOOST = env_bool("ELEVENLABS_SPEAKER_BOOST", True)
LANGUAGE_OVERRIDE = env_bool(
    "ELEVENLABS_IS_OVERRIDE",
    env_bool("ELEVENLABS_LANGUAGE_OVERRIDE", False),
)
LANGUAGE_CODE = env_text("ELEVENLABS_LANGUAGE_CODE", "en").lower()
REQUEST_TIMEOUT = int(env_text("ELEVENLABS_TIMEOUT", "120"))
RETRIES = int(env_text("ELEVENLABS_RETRIES", "3"))
ALLOW_CREDIT_POOLING = env_bool("ELEVENLABS_ALLOW_CREDIT_POOLING", True)

INVALID_VALUES = {
    "",
    "BURAYA_ELEVENLABS_API_ANAHTARINI_YAZ",
    "BURAYA_ELEVENLABS_VOICE_ID_YAZ",
}

KEY_SWITCH_CODES = {
    "invalid_api_key",
    "quota_exceeded",
    "insufficient_credits",
    "paid_plan_required",
    "authorization_error",
    "model_access_denied",
    "voice_not_found",
}


def load_api_keys() -> list[tuple[str, str]]:
    # Keep the first non-empty value for every numbered slot in .env. This
    # prevents a later blank template line from hiding a real key above it.
    file_keys: dict[int, str] = {}
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(
                r"^\s*ELEVENLABS_API_KEY_(\d+)\s*=\s*(.*?)\s*$",
                raw_line,
            )
            if not match:
                continue
            number = int(match.group(1))
            value = match.group(2).strip().strip("\"'")
            if value not in INVALID_VALUES and number not in file_keys:
                file_keys[number] = value

    numbered: dict[int, str] = dict(file_keys)
    for name, value in os.environ.items():
        match = re.fullmatch(r"ELEVENLABS_API_KEY_(\d+)", name)
        value = value.strip()
        if match and value not in INVALID_VALUES:
            numbered[int(match.group(1))] = value

    keys: list[tuple[str, str]] = [
        (str(number), value)
        for number, value in sorted(numbered.items())
    ]

    legacy = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if legacy not in INVALID_VALUES and all(value != legacy for _, value in keys):
        keys.append(("legacy", legacy))
    return keys


API_KEYS = load_api_keys()
ACTIVE_KEY_INDEX = 0
KNOWN_REMAINING_CREDITS: dict[str, int] = {}
REQUEST_ATTEMPT_EVENTS: list[dict] = []


class TeeStream:
    """Write console output to both the terminal and a UTF-8 log file."""

    def __init__(self, terminal, log_file, compact: bool = False) -> None:
        self.terminal = terminal
        self.log_file = log_file
        self.compact = compact
        self.suppress_next_newline = False

    def write(self, value: str) -> int:
        self.log_file.write(value)
        self.log_file.flush()

        # Full DEBUG output stays in elevenlabs.log. Compact mode keeps the
        # interactive window focused on progress, warnings and actual errors.
        if self.compact and (
            " | DEBUG | " in value
            or " | WARNING | " in value
        ):
            self.suppress_next_newline = True
            return len(value)
        if self.compact and self.suppress_next_newline and value == "\n":
            self.suppress_next_newline = False
            return len(value)

        self.terminal.write(value)
        return len(value)

    def flush(self) -> None:
        self.terminal.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def debug(message: str) -> None:
    print(f"{utc_now()} | DEBUG | {message}")


def info(message: str) -> None:
    print(f"{utc_now()} | INFO | {message}")


def warning(message: str) -> None:
    print(f"{utc_now()} | WARNING | {message}")


def safe_configuration_summary() -> str:
    """Return useful settings without exposing any API key value."""
    key_slots = ", ".join(label for label, _ in API_KEYS) or "none"
    return (
        f"root={AUTOMATION_ROOT}; voice_id={VOICE_ID}; model={MODEL_ID}; "
        f"format={OUTPUT_FORMAT}; speed={SPEED}; stability={STABILITY}; "
        f"similarity={SIMILARITY_BOOST}; style={STYLE}; "
        f"speaker_boost={SPEAKER_BOOST}; language_override={LANGUAGE_OVERRIDE}; "
        f"language={LANGUAGE_CODE if LANGUAGE_OVERRIDE else 'automatic'}; "
        f"timeout={REQUEST_TIMEOUT}s; retries={RETRIES}; "
        f"configured_key_slots=[{key_slots}]"
    )


class ElevenLabsAPIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        super().__init__(
            f"ElevenLabs request rejected ({status_code}, {code or 'unknown'}): "
            f"{message}"
        )
        self.status_code = status_code
        self.code = code
        self.message = message


class AllKeysQuotaError(RuntimeError):
    def __init__(self, remaining_by_slot: dict[str, int]) -> None:
        self.remaining_by_slot = remaining_by_slot
        total = sum(remaining_by_slot.values())
        super().__init__(
            f"No single API key has enough credits. Combined remaining "
            f"credits across usable slots: {total}."
        )


class InsufficientCombinedQuotaError(RuntimeError):
    def __init__(self, required: int, available: int) -> None:
        self.required = required
        self.available = available
        super().__init__(
            f"All API keys were tried, but their combined remaining quota is "
            f"{available} characters; this news text requires {required}. "
            f"Add or top up an API key, then run 0 o again."
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate one ElevenLabs audio file per news summary."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--video", type=int, help="Video number to synthesize.")
    action.add_argument(
        "--check",
        action="store_true",
        help="Validate the API key and configured voice.",
    )
    action.add_argument(
        "--list-voices",
        action="store_true",
        help="List voices available to the configured API key.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate audio files that already exist.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Show concise console progress while preserving full log details.",
    )
    return parser.parse_args()


def require_api_key() -> None:
    if not API_KEYS:
        raise RuntimeError(
            "No ElevenLabs API key is configured. Add ELEVENLABS_API_KEY_1, "
            "ELEVENLABS_API_KEY_2, ... to "
            f"{AUTOMATION_ROOT / '.env'}."
        )


def require_voice_id() -> None:
    if VOICE_ID in INVALID_VALUES:
        raise RuntimeError(
            "ELEVENLABS_VOICE_ID is missing. Run "
            r".venv\Scripts\python.exe elevenlabs_tts.py --list-voices, "
            "then add the selected voice ID to .env."
        )


def validate_voice_settings() -> None:
    ranged_settings = {
        "ELEVENLABS_STABILITY": (STABILITY, 0.0, 1.0),
        "ELEVENLABS_SIMILARITY_BOOST": (SIMILARITY_BOOST, 0.0, 1.0),
        "ELEVENLABS_STYLE": (STYLE, 0.0, 1.0),
        "ELEVENLABS_SPEED": (SPEED, 0.7, 1.2),
    }
    for name, (value, minimum, maximum) in ranged_settings.items():
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{name} must be between {minimum} and {maximum}; got {value}."
            )
    if LANGUAGE_OVERRIDE and not re.fullmatch(r"[a-z]{2,3}", LANGUAGE_CODE):
        raise ValueError(
            "ELEVENLABS_LANGUAGE_CODE must be a 2-3 letter language code "
            "when ELEVENLABS_IS_OVERRIDE=true."
        )


def headers(api_key: str) -> dict[str, str]:
    return {
        "xi-api-key": api_key,
        "Accept": "application/json",
    }


def error_details(response: requests.Response) -> tuple[str, str]:
    try:
        payload = response.json()
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            code = str(
                detail.get("code")
                or detail.get("status")
                or detail.get("type")
                or ""
            )
            message = str(
                detail.get("message")
                or json.dumps(detail, ensure_ascii=False)
            )
            return code, message
        return "", str(detail)
    except (ValueError, AttributeError):
        return "", response.text[:1000] or response.reason


def request_with_retry(
    method: str,
    url: str,
    **kwargs,
) -> requests.Response:
    last_error: Exception | None = None

    for attempt in range(1, RETRIES + 1):
        started = time.monotonic()
        event = {
            "method": method.upper(),
            "endpoint": url.removeprefix(API_BASE),
            "attempt": attempt,
            "max_attempts": RETRIES,
        }
        debug(
            f"HTTP {method} {url} | attempt={attempt}/{RETRIES} | "
            f"timeout={REQUEST_TIMEOUT}s"
        )
        try:
            response = requests.request(
                method,
                url,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
            elapsed = time.monotonic() - started
            debug(
                f"HTTP response | status={response.status_code} | "
                f"elapsed={elapsed:.2f}s | "
                f"content_type={response.headers.get('content-type', 'unknown')} | "
                f"content_length={response.headers.get('content-length', 'unknown')} | "
                f"request_id={response.headers.get('request-id', 'unknown')}"
            )
            if response.status_code < 400:
                event.update(
                    {
                        "result": "success",
                        "status_code": response.status_code,
                        "elapsed_seconds": round(elapsed, 3),
                        "request_id": response.headers.get("request-id", ""),
                    }
                )
                REQUEST_ATTEMPT_EVENTS.append(event)
                return response

            code, message = error_details(response)
            event.update(
                {
                    "result": "http_error",
                    "status_code": response.status_code,
                    "error_code": code,
                    "elapsed_seconds": round(elapsed, 3),
                    "request_id": response.headers.get("request-id", ""),
                }
            )
            REQUEST_ATTEMPT_EVENTS.append(event)
            warning(
                f"ElevenLabs rejected request | status={response.status_code} | "
                f"code={code or 'unknown'} | message={message}"
            )
            if response.status_code not in {408, 409, 429} and response.status_code < 500:
                raise ElevenLabsAPIError(
                    response.status_code,
                    code,
                    message,
                )

            last_error = ElevenLabsAPIError(
                response.status_code,
                code,
                message,
            )
        except requests.RequestException as exc:
            last_error = exc
            event.update(
                {
                    "result": "network_error",
                    "error_type": type(exc).__name__,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            REQUEST_ATTEMPT_EVENTS.append(event)
            warning(
                f"Network error | type={type(exc).__name__} | message={exc}"
            )

        if attempt < RETRIES:
            wait_seconds = min(2 ** (attempt - 1), 8)
            REQUEST_ATTEMPT_EVENTS[-1]["retry_wait_seconds"] = wait_seconds
            warning(
                f"Request failed ({attempt}/{RETRIES}); retrying in "
                f"{wait_seconds}s."
            )
            time.sleep(wait_seconds)

    if last_error is None:
        raise RuntimeError("ElevenLabs request failed.")
    raise last_error


def should_switch_key(error: ElevenLabsAPIError) -> bool:
    return (
        error.code in KEY_SWITCH_CODES
        or error.status_code in {401, 402, 403}
    )


def with_key_fallback(operation):
    global ACTIVE_KEY_INDEX

    require_api_key()
    last_error: Exception | None = None
    quota_errors = 0

    for index in range(ACTIVE_KEY_INDEX, len(API_KEYS)):
        label, api_key = API_KEYS[index]
        debug(
            f"Trying API key slot {label} "
            f"({index + 1}/{len(API_KEYS)}). Key value is hidden."
        )
        try:
            result = operation(api_key)
            ACTIVE_KEY_INDEX = index
            debug(f"API key slot {label} succeeded.")
            return result, label
        except ElevenLabsAPIError as exc:
            last_error = exc
            if exc.code == "quota_exceeded":
                quota_errors += 1
                match = re.search(
                    r"You have\s+([\d,]+)\s+credits remaining",
                    exc.message,
                    flags=re.IGNORECASE,
                )
                if match:
                    KNOWN_REMAINING_CREDITS[label] = int(
                        match.group(1).replace(",", "")
                    )
            warning(
                f"API key slot {label} failed | status={exc.status_code} | "
                f"code={exc.code or 'unknown'} | switch_key={should_switch_key(exc)}"
            )
            if not should_switch_key(exc):
                raise
            if index + 1 >= len(API_KEYS):
                break
            print(
                f"ElevenLabs API key slot {label} is unavailable "
                f"({exc.code or exc.status_code}); trying the next key."
            )
        except requests.RequestException as exc:
            # A key-specific connection can occasionally stall after all retries.
            # Continue with the next configured slot instead of aborting the batch.
            last_error = exc
            warning(
                f"API key slot {label} ended with a network error after all "
                f"retries | type={type(exc).__name__} | message={exc}"
            )
            if index + 1 >= len(API_KEYS):
                break
            warning(f"Trying the next configured API key after slot {label}.")

    if last_error is None:
        raise RuntimeError("No usable ElevenLabs API key was found.")
    if quota_errors and KNOWN_REMAINING_CREDITS:
        raise AllKeysQuotaError(dict(KNOWN_REMAINING_CREDITS))
    raise last_error


def list_voices() -> int:
    response, key_label = with_key_fallback(
        lambda api_key: request_with_retry(
            "GET",
            f"{API_BASE}/v2/voices",
            headers=headers(api_key),
            params={
                "page_size": 100,
                "sort": "name",
                "sort_direction": "asc",
                "include_total_count": "false",
            },
        )
    )
    voices = response.json().get("voices", [])

    if not voices:
        print("No voices were returned for this account.")
        return 1

    print(f"Available ElevenLabs voices (API key slot {key_label}):\n")
    for voice in voices:
        name = voice.get("name", "Unnamed")
        voice_id = voice.get("voice_id", "")
        category = voice.get("category", "")
        print(f"{name} | {category} | {voice_id}")

    print(
        "\nCopy the desired voice ID into "
        f"{AUTOMATION_ROOT / '.env'} as ELEVENLABS_VOICE_ID."
    )
    return 0


def validate_voice() -> dict:
    require_voice_id()
    validate_voice_settings()
    info("Validating ElevenLabs voice and account access.")
    debug(f"Configuration: {safe_configuration_summary()}")
    response, key_label = with_key_fallback(
        lambda api_key: request_with_retry(
            "GET",
            f"{API_BASE}/v1/voices/{VOICE_ID}",
            headers=headers(api_key),
        )
    )
    voice = response.json()
    print(
        "ElevenLabs configuration OK: "
        f"{voice.get('name', VOICE_ID)} | {MODEL_ID} | {OUTPUT_FORMAT} | "
        f"speed={SPEED}, stability={STABILITY}, "
        f"similarity={SIMILARITY_BOOST}, style={STYLE}, "
        f"speaker_boost={SPEAKER_BOOST}, "
        f"language_override={LANGUAGE_OVERRIDE} | "
        f"API key slot {key_label}"
    )
    return voice


def tts_settings_payload() -> dict:
    return {
        "voice_id": VOICE_ID,
        "model_id": MODEL_ID,
        "output_format": OUTPUT_FORMAT,
        "voice_settings": {
            "speed": SPEED,
            "stability": STABILITY,
            "similarity_boost": SIMILARITY_BOOST,
            "style": STYLE,
            "use_speaker_boost": SPEAKER_BOOST,
        },
        "language_override": LANGUAGE_OVERRIDE,
        "language_code": LANGUAGE_CODE if LANGUAGE_OVERRIDE else None,
    }


def tts_input_fingerprint(text: str) -> str:
    return json_fingerprint(
        {
            "text_sha256": sha256_text(text),
            "settings": tts_settings_payload(),
        }
    )


def probe_audio(path: Path) -> dict:
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        raise RuntimeError(
            "FFprobe is required to validate generated or cached audio. "
            "Run install_ffmpeg.ps1 or preflight.bat."
        )

    completed = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels:format=duration",
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
        streams = payload.get("streams", [])
        duration = float(payload.get("format", {}).get("duration", 0))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"FFprobe returned invalid metadata for {path.name}.") from exc

    if not streams or duration <= 0:
        raise RuntimeError(f"No valid audio stream was found in {path.name}.")

    stream = streams[0]
    return {
        "duration_seconds": round(duration, 3),
        "codec": stream.get("codec_name", ""),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }


def load_previous_tts_info(audio_dir: Path) -> dict:
    path = audio_dir / "tts_info.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warning(f"Ignoring unreadable TTS metadata: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def cached_audio_metadata(
    *,
    previous_entry: dict | None,
    output_path: Path,
    input_fingerprint: str,
) -> dict | None:
    if not previous_entry or not output_path.is_file():
        return None
    if previous_entry.get("input_fingerprint") != input_fingerprint:
        return None

    expected_checksum = previous_entry.get("audio_sha256", "")
    if not expected_checksum or sha256_file(output_path) != expected_checksum:
        return None

    media = probe_audio(output_path)
    return {
        **previous_entry,
        **media,
        "status": "cached",
        "audio_sha256": expected_checksum,
    }


def summary_files(video_dir: Path) -> list[Path]:
    news_dir = video_dir / "Haberler"
    if not news_dir.exists():
        raise FileNotFoundError(f"News directory not found: {news_dir}")

    project_info_path = video_dir / "project_info.json"
    if project_info_path.exists():
        try:
            project_info = json.loads(project_info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Project manifest is unreadable: {project_info_path}"
            ) from exc
        if project_info.get("status") != "complete":
            raise RuntimeError(
                "News project is incomplete; TTS will not run until its "
                "project_info.json status is complete."
            )
        story_records = project_info.get("stories", [])
        files = [Path(item["text"]) for item in story_records]
        expected = int(project_info.get("expected_count", len(files)))
        if len(files) != expected:
            raise RuntimeError(
                f"Project manifest contains {len(files)} stories; expected {expected}."
            )
        if any(path.parent.resolve() != news_dir.resolve() for path in files):
            raise RuntimeError("Project manifest references a text outside Haberler.")
        missing = [str(path) for path in files if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Project manifest references missing narration files: "
                + ", ".join(missing)
            )
        return files

    files = sorted(
        path
        for path in news_dir.glob("*.txt")
        if not path.stem.endswith("-orijinal")
    )
    if not files:
        raise RuntimeError(f"No summary TXT files found in {news_dir}")
    return files


def story_ids_by_text_file(video_dir: Path, files: list[Path]) -> dict[str, str]:
    project_info_path = video_dir / "project_info.json"
    if project_info_path.exists():
        try:
            payload = json.loads(project_info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        mapped = {
            Path(item.get("text", "")).name: item.get("story_id", "")
            for item in payload.get("stories", [])
            if isinstance(item, dict)
        }
    else:
        mapped = {}
    return {
        path.name: mapped.get(path.name)
        or stable_story_id(f"legacy:{path.stem}")
        for path in files
    }


def speech_request(text: str, api_key: str) -> requests.Response:
    payload = {
        "text": text,
        "model_id": MODEL_ID,
        "voice_settings": {
            "speed": SPEED,
            "stability": STABILITY,
            "similarity_boost": SIMILARITY_BOOST,
            "style": STYLE,
            "use_speaker_boost": SPEAKER_BOOST,
        },
    }
    if LANGUAGE_OVERRIDE:
        payload["language_code"] = LANGUAGE_CODE

    return request_with_retry(
        "POST",
        f"{API_BASE}/v1/text-to-speech/{VOICE_ID}/with-timestamps",
        headers={
            "xi-api-key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        params={"output_format": OUTPUT_FORMAT},
        json=payload,
    )


def decode_timestamp_response(response: requests.Response) -> tuple[bytes, dict]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("ElevenLabs timestamped TTS returned invalid JSON.") from exc

    encoded_audio = payload.get("audio_base64", "")
    if not encoded_audio:
        raise RuntimeError("ElevenLabs timestamped TTS returned no audio_base64.")
    try:
        audio = base64.b64decode(encoded_audio, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "ElevenLabs timestamped TTS returned invalid base64 audio."
        ) from exc

    alignment = payload.get("alignment")
    if not isinstance(alignment, dict):
        raise RuntimeError(
            "ElevenLabs timestamped TTS returned no original-text alignment."
        )
    return audio, alignment


def split_text_for_capacities(
    text: str,
    slots_and_capacities: list[tuple[str, str, int]],
) -> list[tuple[str, str, str]]:
    """Split at sentence/word boundaries while respecting each key's quota."""
    remaining = text.strip()
    chunks: list[tuple[str, str, str]] = []

    for position, (label, api_key, capacity) in enumerate(slots_and_capacities):
        if not remaining:
            break

        future_capacity = sum(
            item[2] for item in slots_and_capacities[position + 1 :]
        )
        minimum_take = max(1, len(remaining) - future_capacity)
        maximum_take = min(capacity, len(remaining))
        if minimum_take > maximum_take:
            continue

        if len(remaining) <= capacity:
            cut = len(remaining)
        else:
            window = remaining[:maximum_take]
            candidates = [
                match.end()
                for match in re.finditer(r"(?<=[.!?])\s+", window)
                if match.end() >= minimum_take
            ]
            if not candidates:
                candidates = [
                    match.start()
                    for match in re.finditer(r"\s+", window)
                    if match.start() >= minimum_take
                ]
            cut = candidates[-1] if candidates else maximum_take

        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append((label, api_key, chunk))
        remaining = remaining[cut:].strip()

    if remaining:
        available = sum(item[2] for item in slots_and_capacities)
        raise InsufficientCombinedQuotaError(len(text), available)
    return chunks


def strip_id3v2(data: bytes) -> bytes:
    """Remove an ID3v2 header before appending a later MP3 segment."""
    if len(data) < 10 or data[:3] != b"ID3":
        return data
    size = (
        (data[6] & 0x7F) << 21
        | (data[7] & 0x7F) << 14
        | (data[8] & 0x7F) << 7
        | (data[9] & 0x7F)
    )
    footer_size = 10 if data[5] & 0x10 else 0
    return data[10 + size + footer_size :]


def generate_speech_with_pooled_credits(
    text: str,
    remaining_by_slot: dict[str, int],
) -> tuple[bytes, dict[str, Any], dict]:
    global ACTIVE_KEY_INDEX

    # Keep a tiny safety margin because the service may count a boundary
    # character differently from Python for unusual Unicode input.
    usable: list[tuple[str, str, int]] = []
    for label, api_key in API_KEYS:
        remaining = remaining_by_slot.get(label, 0)
        safe_capacity = max(0, remaining - 5)
        if safe_capacity:
            usable.append((label, api_key, safe_capacity))

    available = sum(item[2] for item in usable)
    if available < len(text):
        raise InsufficientCombinedQuotaError(len(text), available)

    chunks = split_text_for_capacities(text, usable)
    info(
        f"No single key can cover this story. Using {len(chunks)} sequential "
        f"key chunk(s) and joining them into one MP3."
    )

    audio_parts: list[bytes] = []
    used_slots: list[str] = []
    request_ids: list[str] = []
    timestamp_chunks: list[tuple[str, dict]] = []
    total_cost = 0

    for part_number, (label, api_key, chunk) in enumerate(chunks, start=1):
        info(
            f"  Part {part_number}/{len(chunks)} -> key slot {label} "
            f"({len(chunk)} characters)"
        )
        response = speech_request(chunk, api_key)
        audio, alignment = decode_timestamp_response(response)
        audio_parts.append(
            audio if part_number == 1 else strip_id3v2(audio)
        )
        timestamp_chunks.append((chunk, alignment))
        used_slots.append(label)
        request_ids.append(response.headers.get("request-id", ""))
        cost = response.headers.get("character-cost", "")
        if str(cost).isdigit():
            total_cost += int(cost)
        KNOWN_REMAINING_CREDITS[label] = max(
            0,
            remaining_by_slot.get(label, 0) - len(chunk),
        )
        ACTIVE_KEY_INDEX = next(
            index
            for index, (slot, _) in enumerate(API_KEYS)
            if slot == label
        )

    combined = b"".join(audio_parts)
    merged_alignment = merge_timestamp_alignments(text, timestamp_chunks)
    return (
        combined,
        {
            "api_key_slot": "+".join(used_slots),
            "character_cost": str(total_cost),
            "request_id": ",".join(item for item in request_ids if item),
            "content_type": "audio/mpeg",
            "response_bytes": len(combined),
            "parts": len(chunks),
        },
        merged_alignment,
    )


def generate_speech(text: str) -> tuple[bytes, dict[str, Any], dict]:
    def operation(api_key: str) -> requests.Response:
        return speech_request(text, api_key)

    try:
        response, key_label = with_key_fallback(operation)
    except AllKeysQuotaError as exc:
        if not ALLOW_CREDIT_POOLING:
            raise InsufficientCombinedQuotaError(
                len(text),
                sum(exc.remaining_by_slot.values()),
            ) from exc
        return generate_speech_with_pooled_credits(
            text,
            exc.remaining_by_slot,
        )
    response_content_type = response.headers.get("content-type", "")
    if "json" not in response_content_type.lower():
        warning(
            "Unexpected timestamped response type: "
            f"{response_content_type or 'missing'}"
        )
    audio, alignment = decode_timestamp_response(response)
    return (
        audio,
        {
            "api_key_slot": key_label,
            "character_cost": response.headers.get("character-cost", ""),
            "request_id": response.headers.get("request-id", ""),
            "content_type": "audio/mpeg",
            "response_content_type": response_content_type,
            "response_bytes": len(audio),
        },
        alignment,
    )


def synthesize_video(video_number: int, overwrite: bool) -> int:
    if video_number < 1:
        raise ValueError("--video must be 1 or greater.")

    video_dir = project_dir(video_number)
    info(f"Starting audio stage for {project_name(video_number)}.")
    debug(f"Video directory: {video_dir}")
    files = summary_files(video_dir)
    story_ids = story_ids_by_text_file(video_dir, files)
    audio_dir = video_dir / "Sesler"
    alignment_dir = audio_dir / "alignments"
    audio_dir.mkdir(parents=True, exist_ok=True)
    previous_info = load_previous_tts_info(audio_dir)
    previous_by_name = {
        Path(item.get("text_file", "")).name: item
        for item in previous_info.get("files", [])
        if isinstance(item, dict) and item.get("text_file")
    }
    info(f"Found {len(files)} summary file(s). Output: {audio_dir}")

    results: list[dict] = []
    voice: dict | None = None
    for index, text_path in enumerate(files, start=1):
        output_path = audio_dir / f"{text_path.stem}.mp3"
        text = text_path.read_text(encoding="utf-8").strip()
        text_checksum = sha256_text(text)
        input_fingerprint = tts_input_fingerprint(text)
        previous_entry = previous_by_name.get(text_path.name)
        existed_before = output_path.exists()
        cache_miss_reason = (
            "forced_overwrite"
            if overwrite
            else "missing_audio"
            if not existed_before
            else "missing_metadata"
            if previous_entry is None
            else "text_or_settings_changed"
            if previous_entry.get("input_fingerprint") != input_fingerprint
            else "missing_audio_checksum"
            if not previous_entry.get("audio_sha256")
            else "audio_checksum_changed"
        )
        debug(
            f"Input {index}/{len(files)} | file={text_path.name} | "
            f"characters={len(text)} | output={output_path.name}"
        )

        if not text:
            raise RuntimeError(f"Summary file is empty: {text_path}")

        if output_path.exists() and not overwrite:
            try:
                cached = cached_audio_metadata(
                    previous_entry=previous_entry,
                    output_path=output_path,
                    input_fingerprint=input_fingerprint,
                )
            except RuntimeError as exc:
                warning(f"Cached audio failed validation and will be rebuilt: {exc}")
                cached = None
                cache_miss_reason = "invalid_cached_audio"
            if cached is not None:
                print(f"[{index}/{len(files)}] Cached audio kept: {output_path.name}")
                cached.update(
                    {
                        "text_file": str(text_path),
                        "audio_file": str(output_path),
                        "characters": len(text),
                        "text_sha256": text_checksum,
                        "input_fingerprint": input_fingerprint,
                    }
                )
                results.append(cached)
                continue

        if voice is None:
            voice = validate_voice()

        info(f"[{index}/{len(files)}] Generating: {text_path.name}")
        attempt_start = len(REQUEST_ATTEMPT_EVENTS)
        try:
            audio, metadata, raw_alignment = generate_speech(text)
        except Exception:
            warning(
                f"Generation failed for {text_path.name}. No output file was committed."
            )
            raise
        if not audio:
            raise RuntimeError(f"ElevenLabs returned empty audio for {text_path.name}")

        alignment_path = alignment_dir / f"{text_path.stem}.alignment.json"
        validated: dict[str, Any] = {}

        def validate_generated_audio(temporary_path: Path) -> None:
            temporary_media = probe_audio(temporary_path)
            temporary_checksum = sha256_file(temporary_path)
            temporary_alignment = build_alignment_artifact(
                story_id=story_ids[text_path.name],
                canonical_text=text,
                raw_alignment=raw_alignment,
                method="tts_with_timestamps",
                audio_sha256=temporary_checksum,
                audio_duration_seconds=temporary_media["duration_seconds"],
                request_id=str(metadata.get("request_id", "")),
            )
            temporary_alignment["input_fingerprint"] = (
                alignment_input_fingerprint(
                    text=text,
                    audio_sha256=temporary_checksum,
                )
            )
            validated.update(
                {
                    "media": temporary_media,
                    "audio_sha256": temporary_checksum,
                    "alignment": temporary_alignment,
                }
            )

        atomic_write_bytes(
            output_path,
            audio,
            validate_temporary=validate_generated_audio,
        )
        media = validated["media"]
        audio_checksum = validated["audio_sha256"]
        alignment_artifact = validated["alignment"]
        atomic_write_json(alignment_path, alignment_artifact)
        cost = metadata.get("character_cost", "")
        normalized_cost: int | str = int(cost) if str(cost).isdigit() else str(cost)

        results.append(
            {
                "text_file": str(text_path),
                "audio_file": str(output_path),
                "status": (
                    "regenerated"
                    if existed_before
                    else "generated"
                ),
                "cache_miss_reason": cache_miss_reason,
                "characters": len(text),
                "text_sha256": text_checksum,
                "input_fingerprint": input_fingerprint,
                "audio_sha256": audio_checksum,
                **media,
                **metadata,
                "character_cost": normalized_cost,
                "retry_history": REQUEST_ATTEMPT_EVENTS[attempt_start:],
                "alignment_file": str(alignment_path),
                "alignment_sha256": sha256_file(alignment_path),
                "alignment_method": "tts_with_timestamps",
                "alignment_status": "generated",
            }
        )
        info(
            f"Saved: {output_path} | bytes={len(audio)} | "
            f"key_slot={metadata['api_key_slot']} | "
            f"request_id={metadata['request_id'] or 'unknown'}"
        )

    tts_info = {
        "schema_version": 2,
        "video": project_name(video_number),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "voice_id": VOICE_ID,
        "voice_name": (
            voice.get("name", "")
            if voice is not None
            else previous_info.get("voice_name", "")
        ),
        "model_id": MODEL_ID,
        "output_format": OUTPUT_FORMAT,
        "settings": tts_settings_payload(),
        "files": results,
    }
    atomic_write_json(audio_dir / "tts_info.json", tts_info)
    info(f"Audio stage complete: {audio_dir}")
    return 0


def main() -> int:
    args = parse_arguments()
    log_file = None

    if args.video is not None:
        video_dir = project_dir(args.video)
        if not video_dir.is_dir():
            print(
                f"[TTS ERROR] Project directory not found: {video_dir}. "
                "Run the news stage first.",
                file=sys.stderr,
            )
            return 1
        log_path = video_dir / "elevenlabs.log"
        log_file = log_path.open("a", encoding="utf-8")
        sys.stdout = TeeStream(sys.__stdout__, log_file, compact=args.compact)
        sys.stderr = TeeStream(sys.__stderr__, log_file, compact=args.compact)
        print()
        info("=" * 68)
        info(f"ElevenLabs run started. Log file: {log_path}")

    try:
        if args.list_voices:
            return list_voices()
        if args.check:
            validate_voice()
            return 0
        return synthesize_video(args.video, args.overwrite)
    except Exception as exc:  # noqa: BLE001
        if args.compact and log_file is not None:
            print(file=sys.stderr)
            report_cli_error("TTS", exc)
            log_file.write(
                f"{utc_now()} | ERROR | {type(exc).__name__}: {exc}\n"
            )
            log_file.write(
                f"{utc_now()} | ERROR | Full traceback follows:\n"
            )
            log_file.write(traceback.format_exc())
            log_file.flush()
        else:
            print(
                f"{utc_now()} | ERROR | {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            print(
                f"{utc_now()} | ERROR | Full traceback follows:",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
        if args.video is not None:
            error_log = project_dir(args.video) / "elevenlabs.log"
            if args.compact:
                print(f"Technical details: {error_log}", file=sys.stderr)
            else:
                print(
                    f"{utc_now()} | ERROR | Audio generation stopped. "
                    f"Review {error_log}",
                    file=sys.stderr,
                )
        return 1
    finally:
        if log_file is not None:
            info("ElevenLabs run finished.")
            sys.stdout.flush()
            sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
