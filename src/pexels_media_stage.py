from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_locations import MEDIA_INDEX_ROOT, PROJECTS_ROOT, project_dir  # noqa: E402
from file_utils import atomic_write_bytes, atomic_write_json, sha256_bytes  # noqa: E402
from media_index import normalize_search_text  # noqa: E402
from storyboard_stage import probe_image  # noqa: E402


PEXELS_SEARCH_API = "https://api.pexels.com/v1/search"
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
class PexelsMediaSettings:
    api_key: str = ""
    api_url: str = PEXELS_SEARCH_API
    timeout_seconds: float = 30.0
    request_interval_seconds: float = 0.25
    request_result_limit: int = 24
    search_limit: int = 8
    maximum_download_bytes: int = 25_000_000
    minimum_width: int = 640
    minimum_height: int = 360
    minimum_relevance_score: float = 12.0
    destination: Path = (
        PROJECTS_ROOT / "Assets" / "Media" / "Online Retrieved" / "Pexels"
    )

    @classmethod
    def from_environment(cls) -> "PexelsMediaSettings":
        destination = Path(
            os.getenv(
                "PEXELS_MEDIA_DESTINATION",
                str(
                    PROJECTS_ROOT
                    / "Assets"
                    / "Media"
                    / "Online Retrieved"
                    / "Pexels"
                ),
            )
        ).expanduser()
        if not destination.is_absolute():
            destination = PROJECTS_ROOT / destination
        api_key = os.getenv("PEXELS_API_KEY", "").strip()
        # Backward-compatible recovery for a common .env typo:
        # PEXELS_API_KEY_<secret> instead of PEXELS_API_KEY=<secret>.
        # Never print or persist the recovered value.
        if not api_key:
            env_path = SCRIPT_DIR / ".env"
            if env_path.is_file():
                for raw_line in env_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    line = raw_line.strip()
                    if line.startswith("PEXELS_API_KEY_") and "=" not in line:
                        api_key = line.removeprefix("PEXELS_API_KEY_").strip()
                        break
        return cls(
            api_key=api_key,
            timeout_seconds=float(os.getenv("PEXELS_TIMEOUT", "30")),
            request_interval_seconds=float(
                os.getenv("PEXELS_REQUEST_INTERVAL", "0.25")
            ),
            request_result_limit=int(os.getenv("PEXELS_REQUEST_RESULT_LIMIT", "24")),
            search_limit=int(os.getenv("PEXELS_SEARCH_LIMIT", "8")),
            maximum_download_bytes=int(
                os.getenv("PEXELS_MAX_DOWNLOAD_BYTES", "25000000")
            ),
            minimum_width=int(os.getenv("PEXELS_MIN_WIDTH", "640")),
            minimum_height=int(os.getenv("PEXELS_MIN_HEIGHT", "360")),
            minimum_relevance_score=float(
                os.getenv("PEXELS_MIN_RELEVANCE_SCORE", "12")
            ),
            destination=destination.resolve(),
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("PEXELS_API_KEY is required when Pexels is enabled.")
        if self.timeout_seconds <= 0:
            raise ValueError("PEXELS_TIMEOUT must be positive.")
        if not 0 <= self.request_interval_seconds <= 10:
            raise ValueError("PEXELS_REQUEST_INTERVAL must be between 0 and 10.")
        if not 1 <= self.request_result_limit <= 80:
            raise ValueError("PEXELS_REQUEST_RESULT_LIMIT must be between 1 and 80.")
        if not 1 <= self.search_limit <= self.request_result_limit:
            raise ValueError("PEXELS_SEARCH_LIMIT must not exceed the result limit.")
        if self.maximum_download_bytes < 100_000:
            raise ValueError("PEXELS_MAX_DOWNLOAD_BYTES is too small.")
        if self.minimum_relevance_score < 0:
            raise ValueError("PEXELS_MIN_RELEVANCE_SCORE cannot be negative.")


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
    return result[:maximum].rstrip("-") or "pexels-image"


def _fetch_json(url: str, settings: PexelsMediaSettings) -> tuple[dict, dict]:
    request = Request(
        url,
        headers={
            "Authorization": settings.api_key,
            "Accept": "application/json",
            "User-Agent": "TechNewsAutomation/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        delay = 0.5 * (2 ** (attempt - 1))
        try:
            with urlopen(request, timeout=settings.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
                rate = {
                    "limit": response.headers.get("X-Ratelimit-Limit", ""),
                    "remaining": response.headers.get("X-Ratelimit-Remaining", ""),
                    "reset": response.headers.get("X-Ratelimit-Reset", ""),
                }
            if not isinstance(payload, dict):
                raise RuntimeError("Pexels returned a non-object JSON response.")
            return payload, rate
        except HTTPError as exc:
            last_error = exc
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
    raise RuntimeError(f"Pexels search failed: {last_error}") from last_error


def _fetch_bytes(url: str, settings: PexelsMediaSettings) -> tuple[bytes, str]:
    request = Request(
        url,
        # The key authenticates api.pexels.com only; never forward it to the CDN.
        headers={"Accept": "image/*", "User-Agent": "TechNewsAutomation/1.0"},
    )
    try:
        with urlopen(request, timeout=settings.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            length = str(response.headers.get("Content-Length", "")).strip()
            if length and int(length) > settings.maximum_download_bytes:
                raise RuntimeError("Pexels image exceeds the configured size limit.")
            data = response.read(settings.maximum_download_bytes + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Pexels download failed: {exc}") from exc
    if len(data) > settings.maximum_download_bytes:
        raise RuntimeError("Pexels image exceeds the configured size limit.")
    if content_type not in SUPPORTED_MIME_EXTENSIONS:
        raise RuntimeError(f"Unsupported downloaded MIME type: {content_type}")
    return data, content_type


def _candidate(
    photo: dict,
    *,
    rank: int,
    query: str,
    required_entity: str,
    context: str,
    settings: PexelsMediaSettings,
) -> tuple[dict | None, dict]:
    photo_id = int(photo.get("id", 0) or 0)
    width = int(photo.get("width", 0) or 0)
    height = int(photo.get("height", 0) or 0)
    alt = _plain(photo.get("alt", ""))
    source_page = str(photo.get("url", ""))
    src = photo.get("src", {}) if isinstance(photo.get("src"), dict) else {}
    download_url = str(src.get("landscape") or src.get("large2x") or src.get("original") or "")
    if not photo_id or not source_page.startswith("https://") or not download_url.startswith("https://"):
        return None, {"photo_id": photo_id, "reason": "missing_required_url_or_id"}
    if width < settings.minimum_width or height < settings.minimum_height:
        return None, {"photo_id": photo_id, "reason": "resolution_too_small"}

    searchable = normalize_search_text(" ".join([alt, source_page]))
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
            "alt": alt,
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
            "alt": alt,
        }
    candidate = {
        "provider": "pexels",
        "photo_id": photo_id,
        "title": alt or f"Pexels photo {photo_id}",
        "description": alt,
        "creator": _plain(photo.get("photographer", "")),
        "creator_url": str(photo.get("photographer_url", "")),
        "source_page": source_page,
        "download_url": download_url,
        "original_url": str(src.get("original", "")),
        "width": width,
        "height": height,
        "aspect_ratio": round(aspect, 6),
        "license": {
            "allowed": True,
            "status": "pexels_license",
            "name": "Pexels License",
            "url": "https://www.pexels.com/license/",
            # Pexels asks API users to link back and credit photographers when possible.
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
            "metadata_source": "pexels_alt_text_and_photo_page",
        },
    }
    return candidate, {}


def search_pexels(
    *,
    query: str,
    required_entity: str,
    context: str = "",
    settings: PexelsMediaSettings | None = None,
) -> dict:
    settings = settings or PexelsMediaSettings.from_environment()
    settings.validate()
    query = re.sub(r"\s+", " ", query).strip()
    required_entity = re.sub(r"\s+", " ", required_entity).strip()
    context = re.sub(r"\s+", " ", context).strip()
    if not query:
        raise ValueError("A Pexels search query is required.")
    params = {
        "query": query,
        "orientation": "landscape",
        "size": "large",
        "locale": "en-US",
        "per_page": str(settings.request_result_limit),
    }
    payload, rate = _fetch_json(f"{settings.api_url}?{urlencode(params)}", settings)
    accepted: list[dict] = []
    rejected: list[dict] = []
    for rank, photo in enumerate(payload.get("photos", []), start=1):
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
        "provider": "pexels",
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
        f"pexels_{_slug(result['required_entity'], 45)}_"
        f"{_slug(result['query'], 45)}.json"
    )
    atomic_write_json(path, result)
    return path


def download_candidate(
    candidate: dict,
    *,
    query: str,
    required_entity: str,
    context: str,
    settings: PexelsMediaSettings | None = None,
) -> dict:
    settings = settings or PexelsMediaSettings.from_environment()
    settings.validate()
    if candidate.get("provider") != "pexels":
        raise RuntimeError("Only Pexels candidates may use this downloader.")
    if not candidate.get("relevance_evidence", {}).get("query_hits"):
        raise RuntimeError("Pexels candidate does not prove the search query.")
    data, mime = _fetch_bytes(candidate["download_url"], settings)
    extension = SUPPORTED_MIME_EXTENSIONS[mime]
    filename = (
        f"pexels_{_slug(required_entity or query, 36)}_"
        f"{_slug(candidate['title'], 68)}_{int(candidate['photo_id'])}{extension}"
    )
    destination = settings.destination / filename

    def validate_image(path: Path) -> None:
        media = probe_image(path)
        if (
            int(media["width"]) < settings.minimum_width
            or int(media["height"]) < settings.minimum_height
        ):
            raise RuntimeError("Downloaded Pexels image failed the resolution gate.")

    atomic_write_bytes(destination, data, validate_temporary=validate_image)
    creator = candidate.get("creator") or "Pexels photographer"
    attribution_text = f"Photo by {creator} on Pexels; {candidate['source_page']}"
    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "provider": "pexels",
        "provider_page_id": candidate["photo_id"],
        "source_url": candidate["source_page"],
        "download_url": candidate["download_url"],
        "original_url": candidate.get("original_url", ""),
        "creator": candidate.get("creator", ""),
        "creator_url": candidate.get("creator_url", ""),
        "license_status": "pexels_license",
        "license_name": "Pexels License",
        "license_url": "https://www.pexels.com/license/",
        "attribution_required": True,
        "attribution_text": attribution_text,
        "retrieved_at_utc": utc_now(),
        "query": query,
        "required_entity": required_entity,
        "context": context,
        "relevance_score": candidate["relevance_score"],
        "relevance_evidence": candidate["relevance_evidence"],
        "title": candidate["title"],
        "description": candidate.get("description", ""),
        "sha256": sha256_bytes(data),
    }
    sidecar_path = destination.with_name(destination.name + ".metadata.json")
    atomic_write_json(sidecar_path, sidecar)
    return {
        "file": str(destination),
        "sidecar": str(sidecar_path),
        "sha256": sidecar["sha256"],
        "source_url": sidecar["source_url"],
        "license": {
            "status": sidecar["license_status"],
            "name": sidecar["license_name"],
            "url": sidecar["license_url"],
            "attribution_required": True,
        },
        "cached": False,
    }
