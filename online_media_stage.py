from __future__ import annotations

import argparse
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

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

from file_utils import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_json,
    sha256_bytes,
)
from media_index import normalize_search_text  # noqa: E402
from project_paths import PROJECTS_ROOT, project_dir  # noqa: E402
from storyboard_stage import probe_image  # noqa: E402


WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
SCHEMA_VERSION = 1
SUPPORTED_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OnlineMediaSettings:
    provider: str = "wikimedia_commons"
    api_url: str = WIKIMEDIA_API
    user_agent: str = (
        "TechNewsAutomation/1.0 "
        "(personal low-volume Wikimedia Commons media research)"
    )
    timeout_seconds: float = 30.0
    request_interval_seconds: float = 1.0
    search_limit: int = 12
    request_result_limit: int = 36
    download_width: int = 1920
    maximum_download_bytes: int = 25_000_000
    minimum_width: int = 640
    minimum_height: int = 360
    minimum_relevance_score: float = 14.0
    destination: Path = (
        PROJECTS_ROOT / "Assets" / "Media" / "Online Retrieved" / "Wikimedia Commons"
    )

    @classmethod
    def from_environment(cls) -> "OnlineMediaSettings":
        destination = Path(
            os.getenv(
                "ONLINE_MEDIA_DESTINATION",
                str(
                    PROJECTS_ROOT
                    / "Assets"
                    / "Media"
                    / "Online Retrieved"
                    / "Wikimedia Commons"
                ),
            )
        ).expanduser()
        if not destination.is_absolute():
            destination = PROJECTS_ROOT / destination
        return cls(
            user_agent=os.getenv(
                "WIKIMEDIA_USER_AGENT",
                cls.user_agent,
            ).strip(),
            timeout_seconds=float(os.getenv("ONLINE_MEDIA_TIMEOUT", "30")),
            request_interval_seconds=float(
                os.getenv("ONLINE_MEDIA_REQUEST_INTERVAL", "1")
            ),
            search_limit=int(os.getenv("ONLINE_MEDIA_SEARCH_LIMIT", "12")),
            request_result_limit=int(
                os.getenv("ONLINE_MEDIA_REQUEST_RESULT_LIMIT", "36")
            ),
            download_width=int(os.getenv("ONLINE_MEDIA_DOWNLOAD_WIDTH", "1920")),
            maximum_download_bytes=int(
                os.getenv("ONLINE_MEDIA_MAX_DOWNLOAD_BYTES", "25000000")
            ),
            minimum_width=int(os.getenv("ONLINE_MEDIA_MIN_WIDTH", "640")),
            minimum_height=int(os.getenv("ONLINE_MEDIA_MIN_HEIGHT", "360")),
            minimum_relevance_score=float(
                os.getenv("ONLINE_MEDIA_MIN_RELEVANCE_SCORE", "14")
            ),
            destination=destination.resolve(),
        )

    def validate(self) -> None:
        if not self.user_agent or self.user_agent.startswith("Python-"):
            raise ValueError("WIKIMEDIA_USER_AGENT must be descriptive.")
        if self.timeout_seconds <= 0:
            raise ValueError("ONLINE_MEDIA_TIMEOUT must be positive.")
        if not 0 <= self.request_interval_seconds <= 10:
            raise ValueError("ONLINE_MEDIA_REQUEST_INTERVAL must be between 0 and 10.")
        if not 1 <= self.search_limit <= 30:
            raise ValueError("ONLINE_MEDIA_SEARCH_LIMIT must be between 1 and 30.")
        if not self.search_limit <= self.request_result_limit <= 50:
            raise ValueError(
                "ONLINE_MEDIA_REQUEST_RESULT_LIMIT must be between the search "
                "limit and 50."
            )
        if self.maximum_download_bytes < 100_000:
            raise ValueError("ONLINE_MEDIA_MAX_DOWNLOAD_BYTES is too small.")
        if self.minimum_relevance_score < 0:
            raise ValueError("ONLINE_MEDIA_MIN_RELEVANCE_SCORE cannot be negative.")


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value", "")
    without_tags = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _terms(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", normalize_search_text(value))
        if len(token) >= 2 and token not in STOP_WORDS
    ]


def _slug(value: str, maximum: int = 90) -> str:
    normalized = normalize_search_text(value)
    normalized = re.sub(r"^file\s*", "", normalized)
    normalized = re.sub(r"\.[a-z0-9]{2,5}$", "", normalized)
    result = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return (result[:maximum].rstrip("-") or "wikimedia-image")


def _fetch_json(url: str, settings: OnlineMediaSettings) -> dict:
    request = Request(
        url,
        headers={
            "User-Agent": settings.user_agent,
            "Api-User-Agent": settings.user_agent,
            "Accept": "application/json",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        retry_delay = 0.5 * (2 ** (attempt - 1))
        try:
            with urlopen(request, timeout=settings.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("Wikimedia returned a non-object JSON response.")
            return payload
        except HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                raw_retry = str(exc.headers.get("Retry-After", "")).strip()
                if raw_retry.isdigit():
                    retry_delay = min(30.0, max(retry_delay, float(raw_retry)))
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                break
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 3:
                break
        time.sleep(retry_delay)
    raise RuntimeError(f"Wikimedia search failed: {last_error}") from last_error


def _fetch_bytes(url: str, settings: OnlineMediaSettings) -> tuple[bytes, str]:
    request = Request(
        url,
        headers={"User-Agent": settings.user_agent, "Accept": "image/*"},
    )
    try:
        with urlopen(request, timeout=settings.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            length = response.headers.get("Content-Length", "").strip()
            if length and int(length) > settings.maximum_download_bytes:
                raise RuntimeError("Online image exceeds the configured size limit.")
            data = response.read(settings.maximum_download_bytes + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Wikimedia download failed: {exc}") from exc
    if len(data) > settings.maximum_download_bytes:
        raise RuntimeError("Online image exceeds the configured size limit.")
    if content_type not in SUPPORTED_MIME_EXTENSIONS:
        raise RuntimeError(f"Unsupported downloaded MIME type: {content_type}")
    return data, content_type


def _license_record(metadata: dict) -> dict:
    name = _plain(metadata.get("LicenseShortName"))
    url = _plain(metadata.get("LicenseUrl"))
    normalized = normalize_search_text(name)
    disallowed = any(
        marker in normalized
        for marker in (
            " noncommercial",
            " nc",
            "-nc",
            " no derivatives",
            " nd",
            "-nd",
        )
    )
    public_domain = any(
        marker in normalized
        for marker in ("public domain", "cc0", "pd-old", "pd ")
    )
    creative_commons = "cc by" in normalized or "cc-by" in normalized
    allowed = not disallowed and (public_domain or creative_commons)
    return {
        "allowed": allowed,
        "name": name,
        "url": url,
        "status": (
            "public_domain"
            if public_domain
            else "creative_commons_commercial_reuse"
            if creative_commons and not disallowed
            else "rejected_or_unrecognized"
        ),
        "attribution_required": not public_domain,
    }


def _candidate_from_page(
    page: dict,
    *,
    query: str,
    required_entity: str,
    context: str,
    settings: OnlineMediaSettings,
) -> tuple[dict | None, dict]:
    info_list = page.get("imageinfo", [])
    if not info_list:
        return None, {"title": page.get("title", ""), "reason": "missing_imageinfo"}
    info = info_list[0]
    mime = str(info.get("mime", "")).lower()
    if mime not in SUPPORTED_MIME_EXTENSIONS:
        return None, {"title": page.get("title", ""), "reason": "unsupported_mime"}
    metadata = info.get("extmetadata", {})
    license_info = _license_record(metadata)
    if not license_info["allowed"]:
        return None, {
            "title": page.get("title", ""),
            "reason": "license_not_accepted",
            "license": license_info,
        }
    width = int(info.get("width", 0) or 0)
    height = int(info.get("height", 0) or 0)
    if width < settings.minimum_width or height < settings.minimum_height:
        return None, {
            "title": page.get("title", ""),
            "reason": "resolution_too_small",
            "width": width,
            "height": height,
        }
    categories = [
        str(item.get("title", "")).removeprefix("Category:")
        for item in page.get("categories", [])
    ]
    title = str(page.get("title", "")).removeprefix("File:")
    description = _plain(metadata.get("ImageDescription"))
    object_name = _plain(metadata.get("ObjectName"))
    title_identity = " ".join([title, object_name])
    normalized_title_identity = normalize_search_text(title_identity)
    identity_text = " ".join([title_identity, *categories])
    normalized_identity = normalize_search_text(identity_text)
    searchable = " ".join([identity_text, description])
    normalized_searchable = normalize_search_text(searchable)
    entity_terms = list(dict.fromkeys(_terms(required_entity)))
    query_terms = list(dict.fromkeys(_terms(query)))
    context_terms = list(dict.fromkeys(_terms(context)))
    entity_hits = [term for term in entity_terms if term in normalized_identity]
    query_hits = [term for term in query_terms if term in normalized_searchable]
    context_hits = [term for term in context_terms if term in normalized_searchable]
    entity_coverage = len(entity_hits) / max(1, len(entity_terms))
    exact_entity = normalize_search_text(required_entity) in normalized_title_identity
    if not entity_terms or entity_coverage < 1.0 or not exact_entity:
        return None, {
            "title": title,
            "reason": "required_entity_not_proven_in_title",
            "required_entity": required_entity,
            "entity_hits": entity_hits,
            "exact_entity_phrase_in_title": exact_entity,
        }
    aspect = width / max(1, height)
    crop_retention = min(aspect / (16 / 9), (16 / 9) / aspect)
    score = (
        10.0 * entity_coverage
        + (6.0 if exact_entity else 0.0)
        + 1.5 * len(query_hits)
        + 0.75 * min(6, len(context_hits))
        + 2.0 * max(0.0, crop_retention)
        + min(2.0, (width * height) / (1920 * 1080))
    )
    if score < settings.minimum_relevance_score:
        return None, {
            "title": title,
            "reason": "relevance_score_below_threshold",
            "score": round(score, 4),
        }
    creator = _plain(metadata.get("Artist")) or _plain(metadata.get("Credit"))
    download_url = str(info.get("thumburl") or info.get("url") or "")
    if not download_url.startswith("https://"):
        return None, {"title": title, "reason": "missing_https_download_url"}
    candidate = {
        "provider": "wikimedia_commons",
        "page_id": int(page.get("pageid", 0)),
        "title": title,
        "description": description,
        "categories": categories,
        "creator": creator,
        "source_page": str(info.get("descriptionurl", "")),
        "download_url": download_url,
        "original_url": str(info.get("url", "")),
        "mime": mime,
        "width": width,
        "height": height,
        "aspect_ratio": round(aspect, 6),
        "license": license_info,
        "relevance_score": round(score, 4),
        "relevance_evidence": {
            "required_entity": required_entity,
            "exact_entity_phrase": exact_entity,
            "entity_proof_fields": "title_and_object_name_exact_phrase",
            "entity_hits": entity_hits,
            "query_hits": query_hits,
            "context_hits": context_hits,
            "crop_retention_16_9": round(crop_retention, 6),
        },
    }
    return candidate, {}


def search_wikimedia_commons(
    *,
    query: str,
    required_entity: str,
    context: str = "",
    settings: OnlineMediaSettings | None = None,
) -> dict:
    settings = settings or OnlineMediaSettings.from_environment()
    settings.validate()
    query = re.sub(r"\s+", " ", query).strip()
    required_entity = re.sub(r"\s+", " ", required_entity).strip()
    context = re.sub(r"\s+", " ", context).strip()
    if not query or not required_entity:
        raise ValueError("Both query and required_entity are required.")
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",
        "gsrlimit": str(settings.request_result_limit),
        "prop": "imageinfo|categories",
        "iiprop": "url|mime|size|extmetadata",
        "iiurlwidth": str(settings.download_width),
        "iiextmetadatalanguage": "en",
        "iiextmetadatafilter": (
            "LicenseShortName|LicenseUrl|Artist|Credit|ImageDescription|"
            "ObjectName|UsageTerms"
        ),
        "cllimit": "max",
        "clshow": "!hidden",
    }
    payload = _fetch_json(f"{settings.api_url}?{urlencode(params)}", settings)
    accepted = []
    rejected = []
    for page in payload.get("query", {}).get("pages", []):
        candidate, rejection = _candidate_from_page(
            page,
            query=query,
            required_entity=required_entity,
            context=context,
            settings=settings,
        )
        if candidate is not None:
            accepted.append(candidate)
        else:
            rejected.append(rejection)
    accepted.sort(
        key=lambda item: (
            item["relevance_score"],
            item["width"] * item["height"],
            item["title"],
        ),
        reverse=True,
    )
    accepted = accepted[: settings.search_limit]
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "wikimedia_commons",
        "created_at_utc": utc_now(),
        "query": query,
        "required_entity": required_entity,
        "context": context,
        "configuration": {
            **asdict(settings),
            "destination": str(settings.destination),
        },
        "candidate_count": len(accepted),
        "candidates": accepted,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "quality": {
            "status": "passed" if accepted else "no_safe_match",
            "all_candidates_have_accepted_license": all(
                item["license"]["allowed"] for item in accepted
            ),
            "all_candidates_prove_required_entity": all(
                len(item["relevance_evidence"]["entity_hits"])
                == len(set(_terms(required_entity)))
                for item in accepted
            ),
        },
    }


def save_search_manifest(
    result: dict,
    *,
    video_number: int | None = None,
) -> Path:
    directory = (
        project_dir(video_number) / "05_visuals" / "online_searches"
        if video_number is not None
        else SCRIPT_DIR / "indexes" / "online_searches"
    )
    path = directory / (
        f"wikimedia_{_slug(result['required_entity'], 45)}_"
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
    settings: OnlineMediaSettings | None = None,
) -> dict:
    settings = settings or OnlineMediaSettings.from_environment()
    settings.validate()
    if not candidate.get("license", {}).get("allowed"):
        raise RuntimeError("Candidate license is not accepted for download.")
    evidence = candidate.get("relevance_evidence", {})
    if not evidence.get("entity_hits"):
        raise RuntimeError("Candidate does not prove the required entity.")
    data, mime = _fetch_bytes(candidate["download_url"], settings)
    extension = SUPPORTED_MIME_EXTENSIONS[mime]
    filename = (
        f"wikimedia_{_slug(required_entity, 36)}_"
        f"{_slug(candidate['title'], 72)}_{int(candidate['page_id'])}{extension}"
    )
    destination = settings.destination / filename

    def validate_image(path: Path) -> None:
        media = probe_image(path)
        if (
            int(media["width"]) < settings.minimum_width
            or int(media["height"]) < settings.minimum_height
        ):
            raise RuntimeError("Downloaded image failed the resolution gate.")

    atomic_write_bytes(destination, data, validate_temporary=validate_image)
    license_info = candidate["license"]
    attribution_text = (
        f"{candidate['title']} — {candidate.get('creator') or 'Wikimedia contributor'}; "
        f"{license_info.get('name') or license_info['status']}; "
        f"{candidate.get('source_page', '')}"
    )
    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "provider": "wikimedia_commons",
        "provider_page_id": candidate["page_id"],
        "source_url": candidate.get("source_page", ""),
        "download_url": candidate.get("download_url", ""),
        "original_url": candidate.get("original_url", ""),
        "creator": candidate.get("creator", ""),
        "license_status": license_info["status"],
        "license_name": license_info.get("name", ""),
        "license_url": license_info.get("url", ""),
        "attribution_required": license_info["attribution_required"],
        "attribution_text": attribution_text,
        "retrieved_at_utc": utc_now(),
        "query": query,
        "required_entity": required_entity,
        "context": context,
        "relevance_score": candidate["relevance_score"],
        "relevance_evidence": evidence,
        "title": candidate["title"],
        "description": candidate.get("description", ""),
        "categories": candidate.get("categories", []),
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
            "attribution_required": sidecar["attribution_required"],
        },
        "cached": False,
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Search Wikimedia Commons with entity/license gates and optionally "
            "download the highest-ranked safe candidate."
        )
    )
    parser.add_argument("--query", required=True)
    parser.add_argument("--entity", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--video", type=int)
    parser.add_argument("--download-best", action="store_true")
    parser.add_argument("--confirm-download", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.video is not None and args.video < 1:
            raise ValueError("--video must be 1 or greater.")
        if args.download_best and not args.confirm_download:
            raise RuntimeError(
                "Downloading is blocked until --confirm-download is supplied."
            )
        settings = OnlineMediaSettings.from_environment()
        result = search_wikimedia_commons(
            query=args.query,
            required_entity=args.entity,
            context=args.context,
            settings=settings,
        )
        manifest = save_search_manifest(result, video_number=args.video)
        output: dict[str, Any] = {
            "status": result["quality"]["status"],
            "manifest": str(manifest),
            "candidate_count": result["candidate_count"],
            "candidates": [
                {
                    "rank": index,
                    "title": item["title"],
                    "score": item["relevance_score"],
                    "license": item["license"]["name"],
                    "source_page": item["source_page"],
                    "preview": item["download_url"],
                }
                for index, item in enumerate(result["candidates"], start=1)
            ],
        }
        if args.download_best:
            if not result["candidates"]:
                raise RuntimeError("No safe Wikimedia candidate can be downloaded.")
            output["download"] = download_candidate(
                result["candidates"][0],
                query=args.query,
                required_entity=args.entity,
                context=args.context,
                settings=settings,
            )
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print(f"Status: {output['status']}")
            print(f"Manifest: {output['manifest']}")
            for item in output["candidates"]:
                print(
                    f"{item['rank']:02d}. {item['title']} | "
                    f"score={item['score']} | {item['license']}"
                )
            if output.get("download"):
                print(f"Downloaded: {output['download']['file']}")
                print(f"Metadata: {output['download']['sidecar']}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[ONLINE MEDIA ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
