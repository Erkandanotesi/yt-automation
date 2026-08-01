from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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
    report_cli_error,
    sha256_file,
)
from file_locations import (  # noqa: E402
    PROJECTS_ROOT,
    YOUTUBE_CREDENTIALS_ROOT,
    YOUTUBE_SCHEDULE_STATE_FILE,
    project_dir,
)


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
RETRIABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RETRIABLE_403_REASONS = {"backendError", "rateLimitExceeded", "userRateLimitExceeded"}
MINIMUM_CHAPTERS = 3
MINIMUM_CHAPTER_SECONDS = 10
CHUNK_GRANULARITY = 256 * 1024
TURKEY_TIMEZONE = timezone(timedelta(hours=3), name="Turkey")
PUBLISH_HOURS = (12, 20)
TITLE_SUFFIX = " | Tech Reports"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_youtube_title(value: str) -> str:
    base = value.strip()
    if base.casefold().endswith(TITLE_SUFFIX.casefold()):
        base = base[: -len(TITLE_SUFFIX)].rstrip()
    maximum = 100 - len(TITLE_SUFFIX)
    if len(base) > maximum:
        shortened = base[: maximum + 1].rsplit(" ", 1)[0].strip(" -|:;,. ")
        base = shortened or base[:maximum].strip(" -|:;,. ")
    if not base:
        raise ValueError("YouTube title is empty before the Tech Reports suffix.")
    return f"{base}{TITLE_SUFFIX}"


def remove_sources_section(description: str) -> str:
    lines = description.splitlines()
    if "SOURCES" not in lines:
        return description
    start = lines.index("SOURCES")
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("#"):
        end += 1
    cleaned = lines[:start] + lines[end:]
    return "\n".join(cleaned).rstrip() + "\n"


def _next_slot_after(moment: datetime) -> datetime:
    local = moment.astimezone(TURKEY_TIMEZONE)
    for hour in PUBLISH_HOURS:
        candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > local:
            return candidate
    return (local + timedelta(days=1)).replace(
        hour=PUBLISH_HOURS[0], minute=0, second=0, microsecond=0
    )


def _reserved_publish_times(exclude_manifest: Path | None = None) -> set[datetime]:
    reserved: set[datetime] = set()
    for path in PROJECTS_ROOT.glob("Video */09_publish/youtube_upload_*.json"):
        if exclude_manifest and path.resolve() == exclude_manifest.resolve():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw = payload.get("scheduled_publish_at") or payload.get("metadata", {}).get(
            "status", {}
        ).get("publishAt")
        if not raw:
            continue
        try:
            reserved.add(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            continue
    return reserved


def _last_uploaded_publish_time(state_file: Path) -> datetime | None:
    if not state_file.is_file():
        return None
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        raw = payload.get("last_scheduled_publish_at")
        if not raw:
            return None
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_schedule_state(
    state_file: Path,
    *,
    video_number: int | None,
    video_id: str,
    scheduled_publish_at: str,
) -> None:
    scheduled = datetime.fromisoformat(scheduled_publish_at.replace("Z", "+00:00"))
    if scheduled.tzinfo is None:
        raise ValueError("Scheduled publication time must include a timezone.")
    current = _last_uploaded_publish_time(state_file)
    if current is not None and current > scheduled:
        return
    atomic_write_json(state_file, {
        "schema_version": 1,
        "last_video_number": video_number,
        "last_youtube_video_id": video_id,
        "last_scheduled_publish_at": scheduled.astimezone(TURKEY_TIMEZONE).isoformat(),
        "last_scheduled_publish_at_utc": scheduled.astimezone(timezone.utc).isoformat(),
        "updated_at_utc": utc_now(),
    })


def next_publish_slot(
    now: datetime | None = None,
    *,
    exclude_manifest: Path | None = None,
    state_file: Path = YOUTUBE_SCHEDULE_STATE_FILE,
) -> datetime:
    last_uploaded = _last_uploaded_publish_time(state_file)
    if last_uploaded is None:
        raise RuntimeError(
            "YouTube schedule history is missing. Set last_scheduled_publish_at in "
            f"{state_file}; a saved scheduling baseline is required."
        )
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Current scheduling time must include a timezone.")
    candidate = _next_slot_after(max(last_uploaded, current))
    reserved = _reserved_publish_times(exclude_manifest)
    while any(candidate == item.astimezone(TURKEY_TIMEZONE) for item in reserved):
        candidate = _next_slot_after(candidate)
    return candidate


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _env_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default).strip()).expanduser()
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path.resolve()


@dataclass(frozen=True)
class YouTubeUploadSettings:
    client_secrets_file: Path
    token_file: Path
    schedule_state_file: Path = YOUTUBE_SCHEDULE_STATE_FILE
    privacy_status: str = "private"
    category_id: str = "28"
    default_language: str = "en"
    notify_subscribers: bool = False
    chunk_size_bytes: int = 8 * 1024 * 1024
    maximum_retries: int = 8

    @classmethod
    def from_environment(cls) -> "YouTubeUploadSettings":
        return cls(
            client_secrets_file=_env_path(
                "YOUTUBE_CLIENT_SECRETS_FILE",
                str(YOUTUBE_CREDENTIALS_ROOT / "client_secret.json"),
            ),
            token_file=_env_path(
                "YOUTUBE_TOKEN_FILE",
                str(YOUTUBE_CREDENTIALS_ROOT / "youtube_token.json"),
            ),
            schedule_state_file=_env_path(
                "YOUTUBE_SCHEDULE_STATE_FILE",
                str(YOUTUBE_SCHEDULE_STATE_FILE),
            ),
            privacy_status=os.getenv("YOUTUBE_PRIVACY_STATUS", "private").strip(),
            category_id=os.getenv("YOUTUBE_CATEGORY_ID", "28").strip(),
            default_language=os.getenv("YOUTUBE_DEFAULT_LANGUAGE", "en").strip(),
            notify_subscribers=_env_bool("YOUTUBE_NOTIFY_SUBSCRIBERS", False),
            chunk_size_bytes=int(
                os.getenv("YOUTUBE_UPLOAD_CHUNK_SIZE", str(8 * 1024 * 1024))
            ),
            maximum_retries=int(os.getenv("YOUTUBE_UPLOAD_MAX_RETRIES", "8")),
        )

    def validate(self, *, require_credentials: bool) -> None:
        if self.privacy_status not in {"private", "unlisted", "public"}:
            raise ValueError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public.")
        if not self.category_id.isdigit():
            raise ValueError("YOUTUBE_CATEGORY_ID must be numeric.")
        if not self.default_language:
            raise ValueError("YOUTUBE_DEFAULT_LANGUAGE cannot be empty.")
        if (
            self.chunk_size_bytes < CHUNK_GRANULARITY
            or self.chunk_size_bytes % CHUNK_GRANULARITY != 0
        ):
            raise ValueError(
                "YOUTUBE_UPLOAD_CHUNK_SIZE must be a positive multiple of 256 KiB."
            )
        if self.maximum_retries < 1:
            raise ValueError("YOUTUBE_UPLOAD_MAX_RETRIES must be positive.")
        if require_credentials and not self.client_secrets_file.is_file():
            raise FileNotFoundError(
                "YouTube OAuth client JSON is missing: "
                f"{self.client_secrets_file}"
            )


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Upload manifest not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Upload manifest is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Upload manifest must contain a JSON object.")
    return payload


def _parse_chapter_time(value: str) -> int:
    parts = value.split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        raise ValueError(f"Invalid chapter timestamp: {value}")
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        minutes, seconds = numbers
        hours = 0
    else:
        hours, minutes, seconds = numbers
    if seconds >= 60 or (len(numbers) == 3 and minutes >= 60):
        raise ValueError(f"Invalid chapter timestamp: {value}")
    return hours * 3600 + minutes * 60 + seconds


def validate_chapters(description: str) -> list[dict[str, Any]]:
    lines = description.splitlines()
    try:
        start = lines.index("CHAPTERS") + 1
    except ValueError as exc:
        raise ValueError("Description must contain a CHAPTERS heading.") from exc
    parsed: list[dict[str, Any]] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            break
        pieces = stripped.split(maxsplit=1)
        if len(pieces) != 2:
            raise ValueError(f"Invalid chapter line: {line}")
        seconds = _parse_chapter_time(pieces[0])
        parsed.append(
            {"timestamp": pieces[0], "seconds": seconds, "title": pieces[1]}
        )
    if len(parsed) < MINIMUM_CHAPTERS:
        raise ValueError("YouTube chapters require at least three timestamps.")
    if parsed[0]["seconds"] != 0:
        raise ValueError("The first YouTube chapter must start at 0:00.")
    for first, second in zip(parsed, parsed[1:]):
        if second["seconds"] - first["seconds"] < MINIMUM_CHAPTER_SECONDS:
            raise ValueError("Every YouTube chapter must be at least 10 seconds.")
    return parsed


def validate_upload_manifest(
    manifest: dict[str, Any], settings: YouTubeUploadSettings
) -> dict[str, Any]:
    settings.validate(require_credentials=False)
    if manifest.get("provider") != "youtube_data_api_v3":
        raise ValueError("Manifest provider must be youtube_data_api_v3.")
    video = Path(str(manifest.get("video_file", ""))).resolve()
    thumbnail = Path(str(manifest.get("thumbnail_file", ""))).resolve()
    if not video.is_file():
        raise FileNotFoundError(f"Final video is missing: {video}")
    if not thumbnail.is_file():
        raise FileNotFoundError(f"Final thumbnail is missing: {thumbnail}")
    if sha256_file(video) != manifest.get("video_sha256"):
        raise RuntimeError("Final video checksum no longer matches the manifest.")
    if sha256_file(thumbnail) != manifest.get("thumbnail_sha256"):
        raise RuntimeError("Thumbnail checksum no longer matches the manifest.")
    if thumbnail.suffix.casefold() not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("YouTube thumbnail must be PNG or JPEG.")
    if thumbnail.stat().st_size > 2_000_000:
        raise ValueError("YouTube thumbnail exceeds the 2 MB API limit.")

    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Manifest metadata is missing.")
    snippet = metadata.get("snippet")
    status = metadata.get("status")
    if not isinstance(snippet, dict) or not isinstance(status, dict):
        raise ValueError("Manifest snippet/status metadata is missing.")
    title = format_youtube_title(str(snippet.get("title", "")))
    snippet["title"] = title
    description = remove_sources_section(str(snippet.get("description", ""))).strip()
    snippet["description"] = description
    if not title or len(title) > 100:
        raise ValueError("YouTube title must contain 1 to 100 characters.")
    if not description or len(description.encode("utf-8")) > 5000:
        raise ValueError("YouTube description must contain 1 to 5000 characters.")
    tags = snippet.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        raise ValueError("YouTube tags must be a list of strings.")
    if len(",".join(tags)) > 500:
        raise ValueError("Combined YouTube tags exceed 500 characters.")
    chapters = validate_chapters(description)

    # Environment values are applied at upload time. Scheduled publication must be
    # inserted as private because that pairing is required by the YouTube Data API.
    snippet["categoryId"] = settings.category_id
    snippet["defaultLanguage"] = settings.default_language
    snippet["defaultAudioLanguage"] = settings.default_language
    scheduled_raw = manifest.get("scheduled_publish_at") or status.get("publishAt")
    scheduled = None
    if scheduled_raw:
        try:
            parsed = datetime.fromisoformat(str(scheduled_raw).replace("Z", "+00:00"))
            local = parsed.astimezone(TURKEY_TIMEZONE)
            last_uploaded = _last_uploaded_publish_time(settings.schedule_state_file)
            if (
                last_uploaded is not None
                and parsed > last_uploaded
                and parsed > datetime.now(timezone.utc)
                and local.hour in PUBLISH_HOURS
                and local.minute == local.second == 0
            ):
                scheduled = local
        except ValueError:
            scheduled = None
    scheduled = scheduled or next_publish_slot(state_file=settings.schedule_state_file)
    status["privacyStatus"] = "private"
    status["publishAt"] = scheduled.isoformat()
    status["selfDeclaredMadeForKids"] = False
    upload = manifest.get("upload", {})
    if upload.get("mode") != "resumable":
        raise ValueError("The uploader requires resumable mode.")
    if upload.get("explicit_confirmation_required") is not True:
        raise ValueError("Manifest must require explicit upload confirmation.")
    return {
        "status": "UPLOAD_VALIDATED",
        "video_file": str(video),
        "video_sha256": manifest["video_sha256"],
        "video_bytes": video.stat().st_size,
        "thumbnail_file": str(thumbnail),
        "thumbnail_sha256": manifest["thumbnail_sha256"],
        "thumbnail_bytes": thumbnail.stat().st_size,
        "title": title,
        "privacy_status": "private",
        "scheduled_publish_at": scheduled.isoformat(),
        "scheduled_publish_at_utc": scheduled.astimezone(timezone.utc).isoformat(),
        "video_language": settings.default_language,
        "automatic_captions": "youtube_automatic_when_eligible",
        "chapter_count": len(chapters),
        "notify_subscribers": settings.notify_subscribers,
        "resumable_chunk_size_bytes": settings.chunk_size_bytes,
    }


def _google_dependencies() -> dict[str, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "YouTube upload libraries are missing. Run: "
            r".\.venv\Scripts\python.exe -m pip install -r requirements.txt"
        ) from exc
    return {
        "Request": Request,
        "RefreshError": RefreshError,
        "Credentials": Credentials,
        "InstalledAppFlow": InstalledAppFlow,
        "build": build,
        "HttpError": HttpError,
        "MediaFileUpload": MediaFileUpload,
    }


def _credentials(settings: YouTubeUploadSettings, google: dict[str, Any]) -> Any:
    credentials = None
    if settings.token_file.is_file():
        try:
            credentials = google["Credentials"].from_authorized_user_file(
                str(settings.token_file), [YOUTUBE_UPLOAD_SCOPE]
            )
        except (OSError, ValueError):
            credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(google["Request"]())
        except google["RefreshError"]:
            credentials = None
    if not credentials or not credentials.valid:
        flow = google["InstalledAppFlow"].from_client_secrets_file(
            str(settings.client_secrets_file), [YOUTUBE_UPLOAD_SCOPE]
        )
        credentials = flow.run_local_server(port=0, access_type="offline")
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(settings.token_file, credentials.to_json())
    return credentials


def authorize(settings: YouTubeUploadSettings) -> dict[str, Any]:
    """Complete or refresh OAuth without creating a YouTube video."""
    settings.validate(require_credentials=True)
    google = _google_dependencies()
    credentials = _credentials(settings, google)
    if not credentials or not credentials.valid:
        raise RuntimeError(
            "YouTube OAuth authorization did not produce valid credentials."
        )
    return {
        "status": "YOUTUBE_AUTHORIZED",
        "token_file": str(settings.token_file),
        "scope": YOUTUBE_UPLOAD_SCOPE,
    }


def _http_error_is_retriable(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in RETRIABLE_STATUS_CODES:
        return True
    if status != 403:
        return False
    try:
        payload = json.loads(getattr(exc, "content", b"{}").decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    reasons = {
        str(item.get("reason", ""))
        for item in payload.get("error", {}).get("errors", [])
        if isinstance(item, dict)
    }
    return bool(reasons & RETRIABLE_403_REASONS)


def _next_chunk_with_retry(
    request: Any, *, maximum_retries: int, http_error: type[Exception]
) -> tuple[Any, Any]:
    attempt = 0
    while True:
        try:
            return request.next_chunk()
        except http_error as exc:
            if not _http_error_is_retriable(exc) or attempt >= maximum_retries:
                raise
        except (OSError, TimeoutError, ConnectionError):
            if attempt >= maximum_retries:
                raise
        wait_seconds = min(2**attempt + random.random(), 64)
        time.sleep(wait_seconds)
        attempt += 1


def _execute_with_retry(
    request: Any, *, maximum_retries: int, http_error: type[Exception]
) -> Any:
    attempt = 0
    while True:
        try:
            return request.execute()
        except http_error as exc:
            if not _http_error_is_retriable(exc) or attempt >= maximum_retries:
                raise
        except (OSError, TimeoutError, ConnectionError):
            if attempt >= maximum_retries:
                raise
        wait_seconds = min(2**attempt + random.random(), 64)
        time.sleep(wait_seconds)
        attempt += 1


def _upload_thumbnail(
    youtube: Any,
    google: dict[str, Any],
    *,
    video_id: str,
    thumbnail_path: Path,
    maximum_retries: int,
) -> Any:
    thumbnail_type = mimetypes.guess_type(thumbnail_path.name)[0] or "image/png"
    request = youtube.thumbnails().set(
        videoId=video_id,
        media_body=google["MediaFileUpload"](
            str(thumbnail_path), mimetype=thumbnail_type, resumable=False
        ),
    )
    return _execute_with_retry(
        request,
        maximum_retries=maximum_retries,
        http_error=google["HttpError"],
    )


def upload_from_manifest(
    manifest_path: Path,
    settings: YouTubeUploadSettings,
    *,
    confirm_upload: bool,
    force_new_upload: bool = False,
) -> dict[str, Any]:
    if not confirm_upload:
        raise RuntimeError("Actual upload requires --confirm-upload.")
    settings.validate(require_credentials=True)
    manifest = load_manifest(manifest_path)
    validation = validate_upload_manifest(manifest, settings)
    result_path = manifest_path.parent / "youtube_upload_result.json"
    previous: dict[str, Any] | None = None
    if result_path.is_file() and not force_new_upload:
        previous = load_manifest(result_path)
        if (
            previous.get("video_id")
            and previous.get("video_sha256") == validation["video_sha256"]
        ):
            if previous.get("status") == "UPLOAD_COMPLETE":
                if previous.get("scheduled_publish_at"):
                    save_schedule_state(
                        settings.schedule_state_file,
                        video_number=manifest.get("video_number"),
                        video_id=str(previous["video_id"]),
                        scheduled_publish_at=str(previous["scheduled_publish_at"]),
                    )
                return previous
            if previous.get("status") != "VIDEO_UPLOADED_THUMBNAIL_PENDING":
                raise RuntimeError(
                    "An upload result already contains this video's YouTube ID but "
                    "has an unknown recovery state. Inspect youtube_upload_result.json "
                    "before using --force-new-upload."
                )
        elif previous.get("video_id"):
            raise RuntimeError(
                "This project already uploaded a different final-video checksum as "
                f"{previous['video_id']}. Use --force-new-upload only if another "
                "YouTube video is intentional."
            )

    google = _google_dependencies()
    credentials = _credentials(settings, google)
    youtube = google["build"](
        "youtube", "v3", credentials=credentials, cache_discovery=False
    )
    if previous and previous.get("status") == "VIDEO_UPLOADED_THUMBNAIL_PENDING":
        video_id = str(previous["video_id"])
        partial_result = previous
        print(f"Resuming thumbnail setup for uploaded video {video_id}.")
    else:
        video_path = Path(validation["video_file"])
        video_media = google["MediaFileUpload"](
            str(video_path),
            mimetype="video/mp4",
            chunksize=settings.chunk_size_bytes,
            resumable=True,
        )
        request = youtube.videos().insert(
            part="snippet,status",
            body=manifest["metadata"],
            notifySubscribers=settings.notify_subscribers,
            media_body=video_media,
        )
        response = None
        while response is None:
            progress, response = _next_chunk_with_retry(
                request,
                maximum_retries=settings.maximum_retries,
                http_error=google["HttpError"],
            )
            if progress is not None:
                print(
                    f"Upload progress: {progress.progress() * 100:.1f}%",
                    flush=True,
                )
        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("YouTube returned no video ID after upload.")

        partial_result = {
            "schema_version": 1,
            "status": "VIDEO_UPLOADED_THUMBNAIL_PENDING",
            "video_id": video_id,
            "watch_url": f"https://www.youtube.com/watch?v={video_id}",
            "video_sha256": validation["video_sha256"],
            "privacy_status": validation["privacy_status"],
            "uploaded_at_utc": utc_now(),
            "scheduled_publish_at": validation["scheduled_publish_at"],
            "scheduled_publish_at_utc": validation["scheduled_publish_at_utc"],
            "video_language": validation["video_language"],
            "automatic_captions": validation["automatic_captions"],
        }
        atomic_write_json(result_path, partial_result)
        save_schedule_state(
            settings.schedule_state_file,
            video_number=manifest.get("video_number"),
            video_id=str(video_id),
            scheduled_publish_at=validation["scheduled_publish_at"],
        )

    thumbnail_path = Path(validation["thumbnail_file"])
    thumbnail_response = _upload_thumbnail(
        youtube,
        google,
        video_id=video_id,
        thumbnail_path=thumbnail_path,
        maximum_retries=settings.maximum_retries,
    )
    result = {
        **partial_result,
        "status": "UPLOAD_COMPLETE",
        "thumbnail_sha256": validation["thumbnail_sha256"],
        "thumbnail_response": thumbnail_response,
        "completed_at_utc": utc_now(),
    }
    atomic_write_json(result_path, result)
    return result


def default_manifest(video_number: int) -> Path:
    return project_dir(video_number) / "09_publish" / "youtube_upload_manifest.json"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or explicitly upload a Milestone 9 YouTube package."
    )
    parser.add_argument("--video", type=int)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--action", choices=("validate", "authorize", "upload"), default="validate"
    )
    parser.add_argument("--confirm-upload", action="store_true")
    parser.add_argument("--force-new-upload", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if (
            args.action != "authorize"
            and args.manifest is None
            and (args.video is None or args.video < 1)
        ):
            raise ValueError("Provide --video N or --manifest PATH.")
        settings = YouTubeUploadSettings.from_environment()
        if args.action == "authorize":
            result = authorize(settings)
        else:
            manifest_path = (
                args.manifest.resolve()
                if args.manifest is not None
                else default_manifest(args.video)
            )
        if args.action == "validate":
            result = validate_upload_manifest(load_manifest(manifest_path), settings)
        elif args.action == "upload":
            result = upload_from_manifest(
                manifest_path,
                settings,
                confirm_upload=args.confirm_upload,
                force_new_upload=args.force_new_upload,
            )
        if args.json or args.action in {"validate", "authorize"}:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"YouTube upload complete: {result['watch_url']}")
            print(f"Scheduled public release (Turkey): {result['scheduled_publish_at']}")
        return 0
    except Exception as exc:  # noqa: BLE001
        report_cli_error("YOUTUBE UPLOAD", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
