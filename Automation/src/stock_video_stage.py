from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_locations import ASSETS_ROOT, WORKSPACE_ROOT, project_dir  # noqa: E402
from file_utils import atomic_write_json, sha256_file  # noqa: E402
from media_index import normalize_search_text  # noqa: E402
from provider_rate_limits import record_observation  # noqa: E402
from tooling import find_executable  # noqa: E402


Provider = Literal["pexels", "pixabay"]
SCHEMA_VERSION = 1
PEXELS_SEARCH_API = "https://api.pexels.com/v1/videos/search"
PIXABAY_SEARCH_API = "https://pixabay.com/api/videos/"
PEXELS_LICENSE_URL = "https://www.pexels.com/license/"
PIXABAY_LICENSE_URL = "https://pixabay.com/service/license-summary/"
SEARCH_CACHE_TTL_SECONDS = 24 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StockVideoSettings:
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    timeout_seconds: float = 30.0
    request_interval_seconds: float = 0.25
    request_result_limit: int = 20
    candidate_limit: int = 6
    maximum_download_bytes: int = 200_000_000
    minimum_width: int = 1280
    minimum_height: int = 720
    maximum_source_duration_seconds: float = 120.0
    lock_timeout_seconds: float = 30.0
    pexels_destination: Path = (
        ASSETS_ROOT / "Media" / "Online Retrieved" / "Pexels Videos"
    )
    pixabay_destination: Path = (
        ASSETS_ROOT / "Media" / "Online Retrieved" / "Pixabay Videos"
    )

    @classmethod
    def from_environment(cls) -> "StockVideoSettings":
        root = ASSETS_ROOT / "Media" / "Online Retrieved"

        def destination(variable: str, default: Path) -> Path:
            raw = os.getenv(variable, "").strip()
            path = Path(raw).expanduser() if raw else default
            if not path.is_absolute():
                path = WORKSPACE_ROOT / path
            return path.resolve()

        return cls(
            pexels_api_key=os.getenv("PEXELS_API_KEY", "").strip(),
            pixabay_api_key=os.getenv("PIXABAY_API_KEY", "").strip(),
            timeout_seconds=float(os.getenv("STOCK_VIDEO_TIMEOUT", "30")),
            request_interval_seconds=float(
                os.getenv("STOCK_VIDEO_REQUEST_INTERVAL", "0.25")
            ),
            request_result_limit=int(
                os.getenv("STOCK_VIDEO_REQUEST_RESULT_LIMIT", "20")
            ),
            candidate_limit=int(os.getenv("STOCK_VIDEO_CANDIDATE_LIMIT", "6")),
            maximum_download_bytes=int(
                os.getenv("STOCK_VIDEO_MAX_DOWNLOAD_BYTES", "200000000")
            ),
            minimum_width=int(os.getenv("STOCK_VIDEO_MIN_WIDTH", "1280")),
            minimum_height=int(os.getenv("STOCK_VIDEO_MIN_HEIGHT", "720")),
            maximum_source_duration_seconds=float(
                os.getenv("STOCK_VIDEO_MAX_SOURCE_DURATION", "120")
            ),
            lock_timeout_seconds=float(
                os.getenv("STOCK_VIDEO_LOCK_TIMEOUT", "30")
            ),
            pexels_destination=destination(
                "PEXELS_VIDEO_DESTINATION", root / "Pexels Videos"
            ),
            pixabay_destination=destination(
                "PIXABAY_VIDEO_DESTINATION", root / "Pixabay Videos"
            ),
        )

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("STOCK_VIDEO_TIMEOUT must be positive.")
        if not 0 <= self.request_interval_seconds <= 10:
            raise ValueError("STOCK_VIDEO_REQUEST_INTERVAL must be between 0 and 10.")
        if not 3 <= self.request_result_limit <= 80:
            raise ValueError("STOCK_VIDEO_REQUEST_RESULT_LIMIT must be between 3 and 80.")
        if not 1 <= self.candidate_limit <= self.request_result_limit:
            raise ValueError("STOCK_VIDEO_CANDIDATE_LIMIT must not exceed the result limit.")
        if self.maximum_download_bytes < 1_000_000:
            raise ValueError("STOCK_VIDEO_MAX_DOWNLOAD_BYTES is too small.")
        if self.minimum_width < 320 or self.minimum_height < 180:
            raise ValueError("Stock-video minimum resolution is too small.")
        if self.maximum_source_duration_seconds < 2:
            raise ValueError("STOCK_VIDEO_MAX_SOURCE_DURATION must be at least 2 seconds.")
        if self.lock_timeout_seconds <= 0:
            raise ValueError("STOCK_VIDEO_LOCK_TIMEOUT must be positive.")

    def api_key(self, provider: Provider) -> str:
        return self.pexels_api_key if provider == "pexels" else self.pixabay_api_key

    def destination(self, provider: Provider) -> Path:
        return (
            self.pexels_destination
            if provider == "pexels"
            else self.pixabay_destination
        )


def _plain(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _slug(value: str, maximum: int = 80) -> str:
    normalized = normalize_search_text(value)
    result = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return result[:maximum].rstrip("-") or "stock-video"


def _safe_https_url(value: Any, allowed_hosts: tuple[str, ...]) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not any(
        host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts
    ):
        return ""
    return url


def _api_request(provider: Provider, url: str, settings: StockVideoSettings) -> Request:
    headers = {
        "Accept": "application/json",
        "User-Agent": "TechNewsAutomation/1.0",
    }
    if provider == "pexels":
        headers["Authorization"] = settings.pexels_api_key
    return Request(url, headers=headers)


def _fetch_json(
    provider: Provider,
    url: str,
    settings: StockVideoSettings,
) -> tuple[dict, dict]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        delay = 0.5 * (2 ** (attempt - 1))
        try:
            with urlopen(
                _api_request(provider, url, settings),
                timeout=settings.timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                rate = {
                    "limit": response.headers.get("X-Ratelimit-Limit", ""),
                    "remaining": response.headers.get("X-Ratelimit-Remaining", ""),
                    "reset": response.headers.get("X-Ratelimit-Reset", ""),
                }
                record_observation(provider, response.headers)
            if not isinstance(payload, dict):
                raise RuntimeError(f"{provider.title()} returned non-object JSON.")
            return payload, rate
        except HTTPError as exc:
            last_error = RuntimeError(
                f"{provider.title()} API returned HTTP {exc.code}: "
                f"{exc.reason or 'request failed'}"
            )
            if exc.code == 429:
                retry_after = str(exc.headers.get("Retry-After", "")).strip()
                if retry_after.isdigit():
                    delay = min(30.0, max(delay, float(retry_after)))
            if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt == 3:
                break
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
            if attempt == 3:
                break
        time.sleep(delay)
    raise RuntimeError(f"{provider.title()} video search failed: {last_error}") from last_error


def _choose_pexels_file(video: dict, settings: StockVideoSettings) -> dict | None:
    choices = []
    for item in video.get("video_files", []):
        if not isinstance(item, dict):
            continue
        url = _safe_https_url(item.get("link"), ("pexels.com",))
        width = int(item.get("width", 0) or 0)
        height = int(item.get("height", 0) or 0)
        if (
            not url
            or item.get("file_type") not in {None, "video/mp4"}
            or width < settings.minimum_width
            or height < settings.minimum_height
        ):
            continue
        aspect = width / max(1, height)
        crop = min(aspect / (16 / 9), (16 / 9) / aspect)
        if crop < 0.55:
            continue
        choices.append((crop, width * height, -int(item.get("id", 0) or 0), item))
    return max(choices)[-1] if choices else None


def _pexels_candidate(
    video: dict,
    *,
    rank: int,
    query: str,
    required_duration: float,
    settings: StockVideoSettings,
) -> tuple[dict | None, dict]:
    video_id = str(video.get("id", "")).strip()
    duration = float(video.get("duration", 0) or 0)
    source_page = _safe_https_url(video.get("url"), ("pexels.com",))
    preview_url = _safe_https_url(
        video.get("image"), ("pexels.com", "images.pexels.com")
    )
    file_info = _choose_pexels_file(video, settings)
    if not video_id or not source_page or not preview_url or file_info is None:
        return None, {"video_id": video_id, "reason": "missing_safe_urls_or_video_file"}
    if duration + 0.05 < required_duration:
        return None, {"video_id": video_id, "reason": "shorter_than_visual_window"}
    if duration > settings.maximum_source_duration_seconds:
        return None, {"video_id": video_id, "reason": "source_duration_too_long"}
    width = int(file_info.get("width", 0) or 0)
    height = int(file_info.get("height", 0) or 0)
    user = video.get("user", {}) if isinstance(video.get("user"), dict) else {}
    creator = _plain(user.get("name"))
    creator_url = _safe_https_url(user.get("url"), ("pexels.com",))
    aspect = width / max(1, height)
    crop = min(aspect / (16 / 9), (16 / 9) / aspect)
    return {
        "provider": "pexels",
        "candidate_id": f"pexels:{video_id}",
        "video_id": video_id,
        "title": f"Pexels video result for {query}",
        "description": query,
        "creator": creator,
        "creator_url": creator_url,
        "source_page": source_page,
        "preview_url": preview_url,
        "download_url": file_info["link"],
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "aspect_ratio": round(aspect, 6),
        "crop_retention_16_9": round(crop, 6),
        "api_result_rank": rank,
        "query": query,
        "license": {
            "allowed": True,
            "status": "pexels_license",
            "name": "Pexels License",
            "url": PEXELS_LICENSE_URL,
            "attribution_required": True,
        },
    }, {}


def _choose_pixabay_file(hit: dict, settings: StockVideoSettings) -> dict | None:
    videos = hit.get("videos", {}) if isinstance(hit.get("videos"), dict) else {}
    choices = []
    for priority, key in enumerate(("medium", "large", "small", "tiny")):
        item = videos.get(key, {}) if isinstance(videos.get(key), dict) else {}
        url = _safe_https_url(item.get("url"), ("pixabay.com",))
        width = int(item.get("width", 0) or 0)
        height = int(item.get("height", 0) or 0)
        if not url or width < settings.minimum_width or height < settings.minimum_height:
            continue
        aspect = width / max(1, height)
        crop = min(aspect / (16 / 9), (16 / 9) / aspect)
        if crop < 0.55:
            continue
        choices.append((crop, -priority, width * height, item))
    return max(choices)[-1] if choices else None


def _pixabay_candidate(
    hit: dict,
    *,
    rank: int,
    query: str,
    required_duration: float,
    settings: StockVideoSettings,
) -> tuple[dict | None, dict]:
    video_id = str(hit.get("id", "")).strip()
    duration = float(hit.get("duration", 0) or 0)
    source_page = _safe_https_url(hit.get("pageURL"), ("pixabay.com",))
    file_info = _choose_pixabay_file(hit, settings)
    preview_url = ""
    if file_info is not None:
        preview_url = _safe_https_url(file_info.get("thumbnail"), ("pixabay.com",))
    if not video_id or not source_page or not preview_url or file_info is None:
        return None, {"video_id": video_id, "reason": "missing_safe_urls_or_video_file"}
    if duration + 0.05 < required_duration:
        return None, {"video_id": video_id, "reason": "shorter_than_visual_window"}
    if duration > settings.maximum_source_duration_seconds:
        return None, {"video_id": video_id, "reason": "source_duration_too_long"}
    width = int(file_info.get("width", 0) or 0)
    height = int(file_info.get("height", 0) or 0)
    creator = _plain(hit.get("user"))
    user_id = str(hit.get("user_id", "")).strip()
    creator_url = (
        f"https://pixabay.com/users/{_slug(creator, 60)}-{user_id}/"
        if creator and user_id
        else ""
    )
    aspect = width / max(1, height)
    crop = min(aspect / (16 / 9), (16 / 9) / aspect)
    return {
        "provider": "pixabay",
        "candidate_id": f"pixabay:{video_id}",
        "video_id": video_id,
        "title": f"Pixabay video result for {query}",
        "description": _plain(hit.get("tags")) or query,
        "creator": creator,
        "creator_url": creator_url,
        "source_page": source_page,
        "preview_url": preview_url,
        "download_url": file_info["url"],
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "aspect_ratio": round(aspect, 6),
        "crop_retention_16_9": round(crop, 6),
        "api_result_rank": rank,
        "query": query,
        "license": {
            "allowed": True,
            "status": "pixabay_content_license",
            "name": "Pixabay Content License",
            "url": PIXABAY_LICENSE_URL,
            "attribution_required": True,
        },
    }, {}


def search_stock_videos(
    *,
    provider: Provider,
    query: str,
    required_duration: float,
    settings: StockVideoSettings | None = None,
) -> dict:
    settings = settings or StockVideoSettings.from_environment()
    settings.validate()
    query = re.sub(r"\s+", " ", query).strip()
    if not query:
        raise ValueError("A stock-video search query is required.")
    if required_duration <= 0:
        raise ValueError("The required stock-video duration must be positive.")
    if not settings.api_key(provider):
        raise ValueError(f"{provider.upper()}_API_KEY is required when {provider} is enabled.")

    if provider == "pexels":
        params = {
            "query": query,
            "orientation": "landscape",
            "size": "medium",
            "locale": "en-US",
            "per_page": str(settings.request_result_limit),
        }
        url = f"{PEXELS_SEARCH_API}?{urlencode(params)}"
    else:
        params = {
            "key": settings.pixabay_api_key,
            "q": query,
            "lang": "en",
            "video_type": "film",
            "safesearch": "true",
            "order": "popular",
            "per_page": str(max(3, settings.request_result_limit)),
        }
        url = f"{PIXABAY_SEARCH_API}?{urlencode(params)}"

    payload, rate = _fetch_json(provider, url, settings)
    raw_results = payload.get("videos", []) if provider == "pexels" else payload.get("hits", [])
    accepted: list[dict] = []
    rejected: list[dict] = []
    builder = _pexels_candidate if provider == "pexels" else _pixabay_candidate
    for rank, raw in enumerate(raw_results if isinstance(raw_results, list) else [], start=1):
        if not isinstance(raw, dict):
            continue
        candidate, rejection = builder(
            raw,
            rank=rank,
            query=query,
            required_duration=required_duration,
            settings=settings,
        )
        if candidate is None:
            rejected.append(rejection)
        else:
            accepted.append(candidate)
    accepted.sort(
        key=lambda item: (
            -int(item["api_result_rank"]),
            float(item["crop_retention_16_9"]),
            int(item["width"]) * int(item["height"]),
        ),
        reverse=True,
    )
    accepted = accepted[: settings.candidate_limit]
    time.sleep(settings.request_interval_seconds)
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "created_at_utc": utc_now(),
        "query": query,
        "required_duration": round(required_duration, 6),
        "configuration": {
            **{
                key: value
                for key, value in asdict(settings).items()
                if key not in {"pexels_api_key", "pixabay_api_key"}
            },
            "api_key": "configured",
        },
        "rate_limit": rate,
        "candidate_count": len(accepted),
        "candidates": accepted,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "quality": {"status": "passed" if accepted else "no_safe_match"},
    }


def search_cache_path(
    *,
    video_number: int,
    provider: Provider,
    query: str,
    required_duration: float,
) -> Path:
    identity = _slug(f"{query}-{required_duration:.2f}", 70)
    return (
        project_dir(video_number)
        / "05_visuals"
        / "stock_video_search_cache"
        / f"{provider}_{identity}.json"
    )


def load_fresh_search_cache(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(payload.get("created_at_utc", "")))
        age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
        if payload.get("schema_version") != SCHEMA_VERSION or not 0 <= age < SEARCH_CACHE_TTL_SECONDS:
            return None
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_search_manifest(result: dict, *, video_number: int) -> Path:
    path = search_cache_path(
        video_number=video_number,
        provider=result["provider"],
        query=result["query"],
        required_duration=float(result["required_duration"]),
    )
    atomic_write_json(path, result)
    return path


def probe_video(path: Path) -> dict:
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        raise RuntimeError("FFprobe is required to validate downloaded stock videos.")
    result = subprocess.run(
        [
            str(ffprobe),
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,pix_fmt",
            "-of", "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe could not read stock video: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    stream = next(
        (
            item
            for item in payload.get("streams", [])
            if item.get("codec_type") == "video"
        ),
        None,
    )
    if stream is None:
        raise RuntimeError("Downloaded stock asset has no video stream.")
    return {
        "duration_seconds": float(payload.get("format", {}).get("duration") or 0),
        "width": int(stream.get("width", 0) or 0),
        "height": int(stream.get("height", 0) or 0),
        "codec": str(stream.get("codec_name", "")),
        "pixel_format": str(stream.get("pix_fmt", "")),
    }


def _sidecar_path(video: Path) -> Path:
    return video.with_name(video.name + ".metadata.json")


def _download_result(video: Path, sidecar_path: Path, sidecar: dict, *, cached: bool) -> dict:
    return {
        "file": str(video.resolve()),
        "sidecar": str(sidecar_path.resolve()),
        "sha256": sidecar["sha256"],
        "provider": sidecar["provider"],
        "provider_page_id": sidecar["provider_page_id"],
        "source_url": sidecar["source_url"],
        "creator": sidecar.get("creator", ""),
        "width": sidecar["width"],
        "height": sidecar["height"],
        "duration_seconds": sidecar["duration_seconds"],
        "license": {
            "status": sidecar["license_status"],
            "name": sidecar["license_name"],
            "url": sidecar["license_url"],
            "attribution_required": sidecar["attribution_required"],
            "attribution_text": sidecar["attribution_text"],
        },
        "cached": cached,
    }


def _valid_cached_result(
    video: Path,
    sidecar_path: Path,
    *,
    provider: Provider,
    video_id: str,
    required_duration: float,
    settings: StockVideoSettings,
) -> dict | None:
    if not video.is_file() or not sidecar_path.is_file():
        return None
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if (
            sidecar.get("provider") != provider
            or str(sidecar.get("provider_page_id", "")) != video_id
            or sidecar.get("sha256") != sha256_file(video)
        ):
            return None
        media = probe_video(video)
        if (
            media["duration_seconds"] + 0.05 < required_duration
            or media["width"] < settings.minimum_width
            or media["height"] < settings.minimum_height
        ):
            return None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, RuntimeError):
        return None
    return _download_result(video, sidecar_path, sidecar, cached=True)


def _find_cached_download(
    provider: Provider,
    video_id: str,
    required_duration: float,
    settings: StockVideoSettings,
) -> dict | None:
    destination = settings.destination(provider)
    if not destination.is_dir():
        return None
    for sidecar_path in sorted(destination.glob("*.metadata.json")):
        video = sidecar_path.with_name(sidecar_path.name.removesuffix(".metadata.json"))
        result = _valid_cached_result(
            video,
            sidecar_path,
            provider=provider,
            video_id=video_id,
            required_duration=required_duration,
            settings=settings,
        )
        if result is not None:
            return result
    return None


@contextmanager
def _video_lock(
    provider: Provider,
    video_id: str,
    settings: StockVideoSettings,
) -> Iterator[None]:
    destination = settings.destination(provider)
    destination.mkdir(parents=True, exist_ok=True)
    lock = destination / f".{provider}-{_slug(video_id, 50)}.lock"
    deadline = time.monotonic() + settings.lock_timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for {provider} video lock: {video_id}")
            time.sleep(0.1)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock.unlink(missing_ok=True)


def _stream_download(url: str, temporary: Path, settings: StockVideoSettings) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            request = Request(
                url,
                headers={
                    "Accept": "video/mp4,application/octet-stream;q=0.8",
                    "User-Agent": "TechNewsAutomation/1.0",
                },
            )
            with urlopen(request, timeout=settings.timeout_seconds) as response:
                expected = int(response.headers.get("Content-Length", "0") or 0)
                if expected > settings.maximum_download_bytes:
                    raise RuntimeError("Stock video exceeds the configured size limit.")
                written = 0
                with temporary.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > settings.maximum_download_bytes:
                            raise RuntimeError("Stock video exceeds the configured size limit.")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if expected and written != expected:
                    raise RuntimeError("Stock-video download was incomplete.")
            return
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt == 3:
                break
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 3:
                break
        finally:
            if last_error is not None and attempt < 3:
                temporary.unlink(missing_ok=True)
        time.sleep(0.5 * (2 ** (attempt - 1)))
    raise RuntimeError(f"Stock-video download failed: {last_error}") from last_error


def _record_cached_usage(result: dict, *, query: str, context: str) -> dict:
    sidecar_path = Path(result["sidecar"])
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    history = sidecar.get("query_history", [])
    if not isinstance(history, list):
        history = []
    identity = (normalize_search_text(query), normalize_search_text(context))
    existing = {
        (
            normalize_search_text(str(item.get("query", ""))),
            normalize_search_text(str(item.get("context", ""))),
        )
        for item in history
        if isinstance(item, dict)
    }
    if identity not in existing:
        history.append({"query": query, "context": context})
        sidecar["query_history"] = history
        atomic_write_json(sidecar_path, sidecar)
    return _download_result(Path(result["file"]), sidecar_path, sidecar, cached=True)


def download_stock_video(
    candidate: dict,
    *,
    query: str,
    context: str,
    required_duration: float,
    settings: StockVideoSettings | None = None,
) -> dict:
    settings = settings or StockVideoSettings.from_environment()
    settings.validate()
    provider = str(candidate.get("provider", ""))
    if provider not in {"pexels", "pixabay"}:
        raise RuntimeError("Only Pexels or Pixabay candidates may use this downloader.")
    typed_provider: Provider = provider  # type: ignore[assignment]
    video_id = str(candidate.get("video_id", "")).strip()
    if not video_id:
        raise RuntimeError("Stock-video candidate has no provider video ID.")
    allowed_hosts = ("pexels.com",) if provider == "pexels" else ("pixabay.com",)
    download_url = _safe_https_url(candidate.get("download_url"), allowed_hosts)
    if not download_url:
        raise RuntimeError("Stock-video candidate has an unsafe download URL.")

    cached = _find_cached_download(
        typed_provider, video_id, required_duration, settings
    )
    if cached is not None:
        return _record_cached_usage(cached, query=query, context=context)

    with _video_lock(typed_provider, video_id, settings):
        cached = _find_cached_download(
            typed_provider, video_id, required_duration, settings
        )
        if cached is not None:
            return _record_cached_usage(cached, query=query, context=context)

        destination_dir = settings.destination(typed_provider)
        destination_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{provider}_{_slug(query, 55)}_{_slug(video_id, 30)}.mp4"
        )
        destination = destination_dir / filename
        temporary = destination_dir / f".{filename}.{uuid.uuid4().hex}.part"
        try:
            _stream_download(download_url, temporary, settings)
            media = probe_video(temporary)
            if (
                media["duration_seconds"] + 0.05 < required_duration
                or media["width"] < settings.minimum_width
                or media["height"] < settings.minimum_height
            ):
                raise RuntimeError(
                    "Downloaded stock video failed duration or resolution validation."
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

        checksum = sha256_file(destination)
        creator = _plain(candidate.get("creator")) or provider.title()
        source_page = str(candidate["source_page"])
        license_info = candidate.get("license", {})
        attribution_text = (
            f"Video by {creator} on {provider.title()}; {source_page}"
        )
        sidecar = {
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "provider_page_id": video_id,
            "source_url": source_page,
            "download_url": download_url,
            "preview_url": candidate.get("preview_url", ""),
            "creator": candidate.get("creator", ""),
            "creator_url": candidate.get("creator_url", ""),
            "license_status": license_info.get("status", f"{provider}_license"),
            "license_name": license_info.get("name", f"{provider.title()} License"),
            "license_url": license_info.get("url", ""),
            "attribution_required": True,
            "attribution_text": attribution_text,
            "retrieved_at_utc": utc_now(),
            "query": query,
            "context": context,
            "query_history": [{"query": query, "context": context}],
            "width": media["width"],
            "height": media["height"],
            "duration_seconds": round(media["duration_seconds"], 6),
            "codec": media["codec"],
            "pixel_format": media["pixel_format"],
            "sha256": checksum,
        }
        sidecar_path = _sidecar_path(destination)
        atomic_write_json(sidecar_path, sidecar)
        return _download_result(destination, sidecar_path, sidecar, cached=False)
