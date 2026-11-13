from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_locations import (  # noqa: E402
    ASSETS_ROOT,
    MEDIA_INDEX_ROOT,
    WORKSPACE_ROOT,
    project_dir,
    resolve_portable_path,
)
from file_utils import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_json,
    sha256_bytes,
    sha256_file,
)
from media_index import normalize_search_text  # noqa: E402
from provider_rate_limits import record_observation  # noqa: E402
from storyboard_stage import probe_image  # noqa: E402


UNSPLASH_SEARCH_API = "https://api.unsplash.com/search/photos"
UNSPLASH_LICENSE_URL = "https://unsplash.com/license"
UNSPLASH_APP_NAME = "tech_news_automation"
SCHEMA_VERSION = 1
SUPPORTED_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
STOP_WORDS = {
    "a", "an", "and", "are", "for", "from", "in", "is", "of", "on",
    "the", "to", "with",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class UnsplashMediaSettings:
    api_key: str = ""
    api_url: str = UNSPLASH_SEARCH_API
    timeout_seconds: float = 30.0
    request_interval_seconds: float = 0.25
    request_result_limit: int = 24
    search_limit: int = 8
    maximum_download_bytes: int = 25_000_000
    minimum_width: int = 640
    minimum_height: int = 360
    minimum_relevance_score: float = 12.0
    content_filter: str = "high"
    lock_timeout_seconds: float = 30.0
    destination: Path = (
        ASSETS_ROOT / "Media" / "Online Retrieved" / "Unsplash"
    )

    @classmethod
    def from_environment(cls) -> "UnsplashMediaSettings":
        destination = Path(
            os.getenv(
                "UNSPLASH_MEDIA_DESTINATION",
                str(
                    ASSETS_ROOT
                    / "Media"
                    / "Online Retrieved"
                    / "Unsplash"
                ),
            )
        ).expanduser()
        if not destination.is_absolute():
            destination = WORKSPACE_ROOT / destination
        return cls(
            api_key=os.getenv("UNSPLASH_API_KEY", "").strip(),
            timeout_seconds=float(os.getenv("UNSPLASH_TIMEOUT", "30")),
            request_interval_seconds=float(
                os.getenv("UNSPLASH_REQUEST_INTERVAL", "0.25")
            ),
            request_result_limit=int(
                os.getenv("UNSPLASH_REQUEST_RESULT_LIMIT", "24")
            ),
            search_limit=int(os.getenv("UNSPLASH_SEARCH_LIMIT", "8")),
            maximum_download_bytes=int(
                os.getenv("UNSPLASH_MAX_DOWNLOAD_BYTES", "25000000")
            ),
            minimum_width=int(os.getenv("UNSPLASH_MIN_WIDTH", "640")),
            minimum_height=int(os.getenv("UNSPLASH_MIN_HEIGHT", "360")),
            minimum_relevance_score=float(
                os.getenv("UNSPLASH_MIN_RELEVANCE_SCORE", "12")
            ),
            content_filter=os.getenv(
                "UNSPLASH_CONTENT_FILTER",
                "high",
            ).strip().lower(),
            lock_timeout_seconds=float(
                os.getenv("UNSPLASH_LOCK_TIMEOUT", "30")
            ),
            destination=destination.resolve(),
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError(
                "UNSPLASH_API_KEY is required when Unsplash is enabled."
            )
        if self.timeout_seconds <= 0:
            raise ValueError("UNSPLASH_TIMEOUT must be positive.")
        if not 0 <= self.request_interval_seconds <= 10:
            raise ValueError(
                "UNSPLASH_REQUEST_INTERVAL must be between 0 and 10."
            )
        if not 1 <= self.request_result_limit <= 30:
            raise ValueError(
                "UNSPLASH_REQUEST_RESULT_LIMIT must be between 1 and 30."
            )
        if not 1 <= self.search_limit <= self.request_result_limit:
            raise ValueError(
                "UNSPLASH_SEARCH_LIMIT must not exceed the result limit."
            )
        if self.maximum_download_bytes < 100_000:
            raise ValueError("UNSPLASH_MAX_DOWNLOAD_BYTES is too small.")
        if self.minimum_relevance_score < 0:
            raise ValueError("UNSPLASH_MIN_RELEVANCE_SCORE cannot be negative.")
        if self.content_filter not in {"low", "high"}:
            raise ValueError("UNSPLASH_CONTENT_FILTER must be low or high.")
        if self.lock_timeout_seconds <= 0:
            raise ValueError("UNSPLASH_LOCK_TIMEOUT must be positive.")


def _plain(value: Any) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _terms(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", normalize_search_text(value))
        if len(token) >= 3 and token not in STOP_WORDS
    ]


def _slug(value: str, maximum: int = 90) -> str:
    normalized = normalize_search_text(value)
    result = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return result[:maximum].rstrip("-") or "unsplash-image"


def _with_attribution_utm(url: str) -> str:
    if not url.startswith("https://"):
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        {
            "utm_source": UNSPLASH_APP_NAME,
            "utm_medium": "referral",
        }
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _api_request(url: str, settings: UnsplashMediaSettings) -> Request:
    return Request(
        url,
        headers={
            "Authorization": f"Client-ID {settings.api_key}",
            "Accept": "application/json",
            "Accept-Version": "v1",
            "User-Agent": "TechNewsAutomation/1.0",
        },
    )


def _api_error(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        errors = payload.get("errors", []) if isinstance(payload, dict) else []
        if isinstance(errors, list) and errors:
            return "; ".join(_plain(item) for item in errors)
    except (OSError, json.JSONDecodeError):
        pass
    return str(exc.reason or f"HTTP {exc.code}")


def _fetch_json(
    url: str,
    settings: UnsplashMediaSettings,
) -> tuple[dict, dict]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        delay = 0.5 * (2 ** (attempt - 1))
        try:
            with urlopen(
                _api_request(url, settings),
                timeout=settings.timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                rate = {
                    "limit": response.headers.get("X-Ratelimit-Limit", ""),
                    "remaining": response.headers.get(
                        "X-Ratelimit-Remaining",
                        "",
                    ),
                }
                record_observation("unsplash", response.headers)
            if not isinstance(payload, dict):
                raise RuntimeError(
                    "Unsplash returned a non-object JSON response."
                )
            return payload, rate
        except HTTPError as exc:
            last_error = RuntimeError(
                f"Unsplash API returned HTTP {exc.code}: {_api_error(exc)}"
            )
            if exc.code == 429:
                retry_after = str(exc.headers.get("Retry-After", "")).strip()
                if retry_after.isdigit():
                    delay = min(30.0, max(delay, float(retry_after)))
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                break
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 3:
                break
        time.sleep(delay)
    raise RuntimeError(f"Unsplash API request failed: {last_error}") from last_error


def _track_download(
    download_location: str,
    settings: UnsplashMediaSettings,
) -> dict:
    if not download_location.startswith("https://api.unsplash.com/"):
        raise RuntimeError("Unsplash candidate has an invalid download location.")
    payload, _ = _fetch_json(download_location, settings)
    if not str(payload.get("url", "")).startswith("https://"):
        raise RuntimeError("Unsplash download tracking returned no image URL.")
    return payload


def _fetch_bytes(
    url: str,
    settings: UnsplashMediaSettings,
) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        delay = 0.5 * (2 ** (attempt - 1))
        request = Request(
            url,
            # The key authenticates api.unsplash.com only; never send it to CDN.
            headers={
                "Accept": "image/*",
                "User-Agent": "TechNewsAutomation/1.0",
            },
        )
        try:
            with urlopen(request, timeout=settings.timeout_seconds) as response:
                content_type = response.headers.get_content_type().lower()
                length = str(response.headers.get("Content-Length", "")).strip()
                expected_length = int(length) if length else 0
                if expected_length > settings.maximum_download_bytes:
                    raise RuntimeError(
                        "Unsplash image exceeds the configured size limit."
                    )
                data = response.read(settings.maximum_download_bytes + 1)
            if len(data) > settings.maximum_download_bytes:
                raise RuntimeError(
                    "Unsplash image exceeds the configured size limit."
                )
            if expected_length and len(data) != expected_length:
                raise RuntimeError("Unsplash image download was incomplete.")
            if content_type not in SUPPORTED_MIME_EXTENSIONS:
                raise RuntimeError(
                    f"Unsupported downloaded MIME type: {content_type}"
                )
            return data, content_type
        except HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                retry_after = str(exc.headers.get("Retry-After", "")).strip()
                if retry_after.isdigit():
                    delay = min(30.0, max(delay, float(retry_after)))
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                break
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 3:
                break
        except RuntimeError:
            raise
        time.sleep(delay)
    raise RuntimeError(f"Unsplash download failed: {last_error}") from last_error


def _candidate(
    photo: dict,
    *,
    rank: int,
    query: str,
    required_entity: str,
    context: str,
    settings: UnsplashMediaSettings,
) -> tuple[dict | None, dict]:
    photo_id = str(photo.get("id", "")).strip()
    width = int(photo.get("width", 0) or 0)
    height = int(photo.get("height", 0) or 0)
    alt = _plain(photo.get("alt_description", ""))
    description = _plain(photo.get("description", ""))
    tag_titles = [
        _plain(tag.get("title", ""))
        for tag in photo.get("tags_preview", [])
        if isinstance(tag, dict)
    ]
    visible_description = _plain(" ".join([alt, description, *tag_titles]))
    links = photo.get("links", {}) if isinstance(photo.get("links"), dict) else {}
    urls = photo.get("urls", {}) if isinstance(photo.get("urls"), dict) else {}
    user = photo.get("user", {}) if isinstance(photo.get("user"), dict) else {}
    user_links = (
        user.get("links", {}) if isinstance(user.get("links"), dict) else {}
    )
    source_page = _with_attribution_utm(str(links.get("html", "")))
    download_url = str(urls.get("regular") or urls.get("full") or "")
    download_location = str(links.get("download_location", ""))
    if (
        not photo_id
        or not source_page.startswith("https://unsplash.com/")
        or not download_url.startswith("https://images.unsplash.com/")
        or not download_location.startswith("https://api.unsplash.com/")
    ):
        return None, {
            "photo_id": photo_id,
            "reason": "missing_required_url_or_id",
        }
    if width < settings.minimum_width or height < settings.minimum_height:
        return None, {"photo_id": photo_id, "reason": "resolution_too_small"}

    searchable = normalize_search_text(
        " ".join([visible_description, source_page])
    )
    query_terms = list(dict.fromkeys(_terms(query)))
    entity_terms = list(dict.fromkeys(_terms(required_entity)))
    context_terms = list(dict.fromkeys(_terms(context)))
    query_hits = [term for term in query_terms if term in searchable]
    entity_hits = [term for term in entity_terms if term in searchable]
    context_hits = [term for term in context_terms if term in searchable]
    meaningful_query_hits = [term for term in query_hits if len(term) >= 4]
    if not meaningful_query_hits and not entity_hits:
        return None, {
            "photo_id": photo_id,
            "reason": "metadata_does_not_ground_query",
            "description": visible_description,
        }

    aspect = width / max(1, height)
    crop_retention = min(aspect / (16 / 9), (16 / 9) / aspect)
    rank_bonus = max(0.0, 4.0 - (rank - 1) * 0.2)
    score = (
        3.0 * min(4, len(meaningful_query_hits))
        + 2.0 * min(3, len(entity_hits))
        + 0.35 * min(6, len(context_hits))
        + 2.0 * max(0.0, crop_retention)
        + rank_bonus
    )
    if score < settings.minimum_relevance_score:
        return None, {
            "photo_id": photo_id,
            "reason": "relevance_score_below_threshold",
            "score": round(score, 4),
            "description": visible_description,
        }
    candidate = {
        "provider": "unsplash",
        "photo_id": photo_id,
        "title": alt or description or f"Unsplash photo {photo_id}",
        "description": visible_description,
        "creator": _plain(user.get("name", "")),
        "creator_url": _with_attribution_utm(str(user_links.get("html", ""))),
        "source_page": source_page,
        "download_url": download_url,
        "download_location": download_location,
        "original_url": str(urls.get("raw") or urls.get("full") or ""),
        "width": width,
        "height": height,
        "aspect_ratio": round(aspect, 6),
        "license": {
            "allowed": True,
            "status": "unsplash_license",
            "name": "Unsplash License",
            "url": UNSPLASH_LICENSE_URL,
            "attribution_required": True,
        },
        "relevance_score": round(score, 4),
        "relevance_evidence": {
            "required_entity": required_entity,
            "entity_hits": entity_hits,
            "query_hits": query_hits,
            "context_hits": context_hits,
            "api_result_rank": rank,
            "crop_retention_16_9": round(crop_retention, 6),
            "metadata_source": "unsplash_description_and_tags_preview",
        },
    }
    return candidate, {}


def search_unsplash(
    *,
    query: str,
    required_entity: str,
    context: str = "",
    settings: UnsplashMediaSettings | None = None,
) -> dict:
    settings = settings or UnsplashMediaSettings.from_environment()
    settings.validate()
    query = re.sub(r"\s+", " ", query).strip()
    required_entity = re.sub(r"\s+", " ", required_entity).strip()
    context = re.sub(r"\s+", " ", context).strip()
    if not query:
        raise ValueError("An Unsplash search query is required.")
    params = {
        "query": query,
        "orientation": "landscape",
        "content_filter": settings.content_filter,
        "order_by": "relevant",
        "per_page": str(settings.request_result_limit),
    }
    payload, rate = _fetch_json(f"{settings.api_url}?{urlencode(params)}", settings)
    accepted: list[dict] = []
    rejected: list[dict] = []
    for rank, photo in enumerate(payload.get("results", []), start=1):
        candidate, rejection = _candidate(
            photo,
            rank=rank,
            query=query,
            required_entity=required_entity,
            context=context,
            settings=settings,
        )
        if candidate is None:
            rejected.append(rejection)
        else:
            accepted.append(candidate)
    accepted.sort(
        key=lambda item: (
            item["relevance_score"],
            -item["relevance_evidence"]["api_result_rank"],
            item["width"] * item["height"],
        ),
        reverse=True,
    )
    accepted = accepted[: settings.search_limit]
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "unsplash",
        "created_at_utc": utc_now(),
        "query": query,
        "required_entity": required_entity,
        "context": context,
        "configuration": {
            **asdict(settings),
            "api_key": "configured" if settings.api_key else "missing",
            "destination": str(settings.destination),
        },
        "rate_limit": rate,
        "candidate_count": len(accepted),
        "candidates": accepted,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "quality": {"status": "passed" if accepted else "no_safe_match"},
    }


def save_search_manifest(result: dict, *, video_number: int | None = None) -> Path:
    directory = (
        project_dir(video_number) / "05_visuals" / "online_searches"
        if video_number is not None
        else MEDIA_INDEX_ROOT / "online_searches"
    )
    path = directory / (
        f"unsplash_{_slug(result['required_entity'], 45)}_"
        f"{_slug(result['query'], 45)}.json"
    )
    atomic_write_json(path, result)
    return path


def _sidecar_image_path(sidecar_path: Path) -> Path:
    suffix = ".metadata.json"
    return sidecar_path.with_name(sidecar_path.name.removesuffix(suffix))


def _valid_cached_result(
    image: Path,
    sidecar_path: Path,
    *,
    photo_id: str,
    settings: UnsplashMediaSettings,
) -> dict | None:
    if not image.is_file() or not sidecar_path.is_file():
        return None
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if (
            not isinstance(sidecar, dict)
            or sidecar.get("provider") != "unsplash"
            or str(sidecar.get("provider_page_id", "")) != photo_id
        ):
            return None
        media = probe_image(image)
        if (
            int(media["width"]) < settings.minimum_width
            or int(media["height"]) < settings.minimum_height
        ):
            return None
        checksum = sha256_file(image)
        if sidecar.get("sha256") != checksum:
            return None
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError):
        return None
    return _download_result(image, sidecar_path, sidecar, cached=True)


def _find_cached_download(
    photo_id: str,
    settings: UnsplashMediaSettings,
) -> dict | None:
    if not settings.destination.is_dir():
        return None
    for sidecar_path in sorted(settings.destination.glob("*.metadata.json")):
        result = _valid_cached_result(
            _sidecar_image_path(sidecar_path),
            sidecar_path,
            photo_id=photo_id,
            settings=settings,
        )
        if result is not None:
            return result
    return None


@contextmanager
def _photo_lock(
    photo_id: str,
    settings: UnsplashMediaSettings,
) -> Iterator[None]:
    settings.destination.mkdir(parents=True, exist_ok=True)
    lock = settings.destination / f".unsplash-{_slug(photo_id, 80)}.lock"
    deadline = time.monotonic() + settings.lock_timeout_seconds
    stale_after = max(
        600.0,
        settings.timeout_seconds * 10,
        settings.lock_timeout_seconds * 10,
    )
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(
                    descriptor,
                    f"{os.getpid()}\n{utc_now()}\n".encode("utf-8"),
                )
            except OSError:
                os.close(descriptor)
                descriptor = None
                lock.unlink(missing_ok=True)
                raise
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > stale_after:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for Unsplash photo lock: {photo_id}"
                )
            time.sleep(0.1)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock.unlink(missing_ok=True)


def _download_result(
    image: Path,
    sidecar_path: Path,
    sidecar: dict,
    *,
    cached: bool,
) -> dict:
    return {
        "file": str(image),
        "sidecar": str(sidecar_path),
        "sha256": sidecar["sha256"],
        "source_url": sidecar["source_url"],
        "license": {
            "status": sidecar["license_status"],
            "name": sidecar["license_name"],
            "url": sidecar["license_url"],
            "attribution_required": True,
        },
        "cached": cached,
    }


def _record_cached_usage(
    result: dict,
    *,
    query: str,
    required_entity: str,
    context: str,
) -> dict:
    """Add new grounding provenance without changing the downloaded bytes."""
    sidecar_path = resolve_portable_path(result["sidecar"])
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cached Unsplash sidecar is unreadable: {exc}") from exc
    history = sidecar.get("query_history", [])
    if not isinstance(history, list):
        history = []
    entry = {
        "query": query,
        "required_entity": required_entity,
        "context": context,
    }
    identity = (
        normalize_search_text(query),
        normalize_search_text(required_entity),
    )
    identities = {
        (
            normalize_search_text(str(item.get("query", ""))),
            normalize_search_text(str(item.get("required_entity", ""))),
        )
        for item in history
        if isinstance(item, dict)
    }
    if identity not in identities:
        history.append(entry)
        sidecar["query_history"] = history
        atomic_write_json(sidecar_path, sidecar)
    return _download_result(
        resolve_portable_path(result["file"]),
        sidecar_path,
        sidecar,
        cached=True,
    )


def _sidecar(
    candidate: dict,
    *,
    query: str,
    required_entity: str,
    context: str,
    checksum: str,
) -> dict:
    creator = candidate.get("creator") or "Unsplash photographer"
    attribution_text = f"Photo by {creator} on Unsplash; {candidate['source_page']}"
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "unsplash",
        "provider_page_id": candidate["photo_id"],
        "source_url": candidate["source_page"],
        "download_url": candidate["download_url"],
        "download_location": candidate["download_location"],
        "original_url": candidate.get("original_url", ""),
        "creator": candidate.get("creator", ""),
        "creator_url": candidate.get("creator_url", ""),
        "license_status": "unsplash_license",
        "license_name": "Unsplash License",
        "license_url": UNSPLASH_LICENSE_URL,
        "attribution_required": True,
        "attribution_text": attribution_text,
        "retrieved_at_utc": utc_now(),
        "query": query,
        "required_entity": required_entity,
        "context": context,
        "query_history": [
            {
                "query": query,
                "required_entity": required_entity,
                "context": context,
            }
        ],
        "relevance_score": candidate["relevance_score"],
        "relevance_evidence": candidate["relevance_evidence"],
        "title": candidate["title"],
        "description": candidate.get("description", ""),
        "sha256": checksum,
    }


def download_candidate(
    candidate: dict,
    *,
    query: str,
    required_entity: str,
    context: str,
    settings: UnsplashMediaSettings | None = None,
) -> dict:
    settings = settings or UnsplashMediaSettings.from_environment()
    settings.validate()
    if candidate.get("provider") != "unsplash":
        raise RuntimeError("Only Unsplash candidates may use this downloader.")
    if not candidate.get("relevance_evidence", {}).get("query_hits"):
        raise RuntimeError("Unsplash candidate does not prove the search query.")
    photo_id = str(candidate.get("photo_id", "")).strip()
    if not photo_id:
        raise RuntimeError("Unsplash candidate has no photo ID.")

    cached = _find_cached_download(photo_id, settings)
    if cached is not None:
        return _record_cached_usage(
            cached,
            query=query,
            required_entity=required_entity,
            context=context,
        )

    with _photo_lock(photo_id, settings):
        cached = _find_cached_download(photo_id, settings)
        if cached is not None:
            return _record_cached_usage(
                cached,
                query=query,
                required_entity=required_entity,
                context=context,
            )

        def validate_image(path: Path) -> None:
            media = probe_image(path)
            if (
                int(media["width"]) < settings.minimum_width
                or int(media["height"]) < settings.minimum_height
            ):
                raise RuntimeError(
                    "Downloaded Unsplash image failed the resolution gate."
                )

        # Recover an image committed just before an interrupted sidecar write.
        # The stable provider ID at the end of every filename makes this lookup
        # independent of the query or title that originally found the photo.
        orphan_pattern = f"unsplash_*_{_slug(photo_id, 32)}.*"
        for orphan in sorted(settings.destination.glob(orphan_pattern)):
            if orphan.suffix.lower() not in SUPPORTED_MIME_EXTENSIONS.values():
                continue
            try:
                validate_image(orphan)
                sidecar_path = orphan.with_name(orphan.name + ".metadata.json")
                sidecar = _sidecar(
                    candidate,
                    query=query,
                    required_entity=required_entity,
                    context=context,
                    checksum=sha256_file(orphan),
                )
                atomic_write_json(sidecar_path, sidecar)
                return _download_result(
                    orphan,
                    sidecar_path,
                    sidecar,
                    cached=True,
                )
            except (OSError, ValueError, KeyError, RuntimeError):
                continue

        data, mime = _fetch_bytes(candidate["download_url"], settings)
        extension = SUPPORTED_MIME_EXTENSIONS[mime]
        filename = (
            f"unsplash_{_slug(required_entity or query, 36)}_"
            f"{_slug(candidate['title'], 60)}_{_slug(photo_id, 32)}{extension}"
        )
        destination = settings.destination / filename
        sidecar_path = destination.with_name(destination.name + ".metadata.json")

        # Unsplash requires this event when a photo is selected for download.
        # It is intentionally after byte retrieval/validation and before commit,
        # and is never called for a verified cached photo.
        validate_probe = settings.destination / (
            f".unsplash-{_slug(photo_id, 32)}-probe{extension}"
        )
        try:
            atomic_write_bytes(validate_probe, data, validate_temporary=validate_image)
            _track_download(candidate["download_location"], settings)
            os.replace(validate_probe, destination)
        finally:
            validate_probe.unlink(missing_ok=True)

        sidecar = _sidecar(
            candidate,
            query=query,
            required_entity=required_entity,
            context=context,
            checksum=sha256_bytes(data),
        )
        atomic_write_json(sidecar_path, sidecar)
        return _download_result(
            destination,
            sidecar_path,
            sidecar,
            cached=False,
        )
