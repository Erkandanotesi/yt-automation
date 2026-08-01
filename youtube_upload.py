from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

from file_utils import atomic_write_json, sha256_file  # noqa: E402
from project_paths import project_dir  # noqa: E402


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
MINIMUM_CHAPTERS = 3
MINIMUM_CHAPTER_SECONDS = 10
CHUNK_GRANULARITY = 256 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                ".youtube_credentials/client_secret.json",
            ),
            token_file=_env_path(
                "YOUTUBE_TOKEN_FILE",
                ".youtube_credentials/youtube_token.json",
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
    title = str(snippet.get("title", "")).strip()
    description = str(snippet.get("description", "")).strip()
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

    # Environment values are applied at upload time, keeping one manifest usable
    # across private validation and a later explicitly approved release policy.
    snippet["categoryId"] = settings.category_id
    snippet["defaultLanguage"] = settings.default_language
    status["privacyStatus"] = settings.privacy_status
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
        "privacy_status": settings.privacy_status,
        "chapter_count": len(chapters),
        "notify_subscribers": settings.notify_subscribers,
        "resumable_chunk_size_bytes": settings.chunk_size_bytes,
    }


def _google_dependencies() -> dict[str, Any]:
    try:
        from google.auth.transport.requests import Request
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
        "Credentials": Credentials,
        "InstalledAppFlow": InstalledAppFlow,
        "build": build,
        "HttpError": HttpError,
        "MediaFileUpload": MediaFileUpload,
    }


def _credentials(settings: YouTubeUploadSettings, google: dict[str, Any]) -> Any:
    credentials = None
    if settings.token_file.is_file():
        credentials = google["Credentials"].from_authorized_user_file(
            str(settings.token_file), [YOUTUBE_UPLOAD_SCOPE]
        )
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(google["Request"]())
    elif not credentials or not credentials.valid:
        flow = google["InstalledAppFlow"].from_client_secrets_file(
            str(settings.client_secrets_file), [YOUTUBE_UPLOAD_SCOPE]
        )
        credentials = flow.run_local_server(port=0, access_type="offline")
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def _next_chunk_with_retry(
    request: Any, *, maximum_retries: int, http_error: type[Exception]
) -> tuple[Any, Any]:
    attempt = 0
    while True:
        try:
            return request.next_chunk()
        except http_error as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in RETRIABLE_STATUS_CODES or attempt >= maximum_retries:
                raise
        except (OSError, TimeoutError, ConnectionError):
            if attempt >= maximum_retries:
                raise
        wait_seconds = min(2**attempt + random.random(), 64)
        time.sleep(wait_seconds)
        attempt += 1


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
    if result_path.is_file() and not force_new_upload:
        previous = load_manifest(result_path)
        if (
            previous.get("video_id")
            and previous.get("video_sha256") == validation["video_sha256"]
        ):
            raise RuntimeError(
                "This exact video was already uploaded as "
                f"{previous['video_id']}. Use --force-new-upload only if a duplicate "
                "YouTube video is intentional."
            )

    google = _google_dependencies()
    credentials = _credentials(settings, google)
    youtube = google["build"](
        "youtube", "v3", credentials=credentials, cache_discovery=False
    )
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
            print(f"Upload progress: {progress.progress() * 100:.1f}%")
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError("YouTube returned no video ID after upload.")

    partial_result = {
        "schema_version": 1,
        "status": "VIDEO_UPLOADED_THUMBNAIL_PENDING",
        "video_id": video_id,
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        "video_sha256": validation["video_sha256"],
        "privacy_status": settings.privacy_status,
        "uploaded_at_utc": utc_now(),
    }
    atomic_write_json(result_path, partial_result)

    thumbnail_path = Path(validation["thumbnail_file"])
    thumbnail_type = mimetypes.guess_type(thumbnail_path.name)[0] or "image/png"
    thumbnail_response = youtube.thumbnails().set(
        videoId=video_id,
        media_body=google["MediaFileUpload"](
            str(thumbnail_path), mimetype=thumbnail_type, resumable=False
        ),
    ).execute()
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
    parser.add_argument("--action", choices=("validate", "upload"), default="validate")
    parser.add_argument("--confirm-upload", action="store_true")
    parser.add_argument("--force-new-upload", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.manifest is None and (args.video is None or args.video < 1):
            raise ValueError("Provide --video N or --manifest PATH.")
        manifest_path = (
            args.manifest.resolve()
            if args.manifest is not None
            else default_manifest(args.video)
        )
        settings = YouTubeUploadSettings.from_environment()
        if args.action == "validate":
            result = validate_upload_manifest(load_manifest(manifest_path), settings)
        else:
            result = upload_from_manifest(
                manifest_path,
                settings,
                confirm_upload=args.confirm_upload,
                force_new_upload=args.force_new_upload,
            )
        if args.json or args.action == "validate":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"YouTube upload complete: {result['watch_url']}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[YOUTUBE UPLOAD ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
