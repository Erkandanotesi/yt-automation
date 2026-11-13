from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from file_locations import (
    NEWS_ARCHIVE_ROOT,
    NEWS_HISTORY_RESET_FILE,
    PROCESSED_URLS_FILE,
    PROJECTS_ROOT,
    project_dir,
    project_name,
    resolve_portable_path,
)
from file_utils import atomic_write_json, atomic_write_text, sha256_text, stable_story_id


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCHEMA_VERSION = 1
WORD_PATTERN = re.compile(r"[a-z0-9][a-z0-9+._-]*")
STOP_WORDS = {
    "about", "after", "also", "and", "are", "been", "before", "being",
    "but", "can", "could", "for", "from", "had", "has", "have", "into",
    "its", "more", "new", "not", "now", "of", "on", "or", "over", "said",
    "says", "than", "that", "the", "their", "them", "there", "these", "they",
    "this", "to", "under", "up", "was", "were", "what", "when", "which",
    "who", "will", "with", "would", "you", "your",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


def content_sha256(value: str) -> str:
    return sha256_text(normalize_text(value))


def _slug(value: str, maximum: int = 70) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return (normalized[:maximum].rstrip("-") or "news")


def _tokens(value: str) -> list[str]:
    return [
        token
        for token in WORD_PATTERN.findall(normalize_text(value))
        if len(token) >= 3 and token not in STOP_WORDS
    ]


def _search_terms(value: str, maximum: int = 180) -> list[str]:
    counts = Counter(_tokens(value))
    return [token for token, _ in counts.most_common(maximum)]


def _overlap(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / math.sqrt(len(first) * len(second))


def local_similarity(
    *,
    new_title: str,
    new_text: str,
    previous_title: str,
    previous_terms: list[str],
) -> float:
    title_score = _overlap(set(_tokens(new_title)), set(_tokens(previous_title)))
    body_score = _overlap(set(_search_terms(new_text)), set(previous_terms))
    return round((0.72 * title_score) + (0.28 * body_score), 6)


@dataclass(frozen=True)
class ArchiveSettings:
    root: Path = NEWS_ARCHIVE_ROOT
    candidate_limit: int = 3
    local_similarity_threshold: float = 0.30
    ai_duplicate_min_confidence: float = 0.90
    ai_text_character_limit: int = 12000
    cache_namespace: str = "default"

    @classmethod
    def from_environment(cls, *, cache_namespace: str = "default") -> "ArchiveSettings":
        return cls(
            root=NEWS_ARCHIVE_ROOT,
            candidate_limit=int(os.getenv("NEWS_DUPLICATE_CANDIDATE_LIMIT", "3")),
            local_similarity_threshold=float(
                os.getenv("NEWS_DUPLICATE_LOCAL_THRESHOLD", "0.30")
            ),
            ai_duplicate_min_confidence=float(
                os.getenv("NEWS_DUPLICATE_AI_MIN_CONFIDENCE", "0.90")
            ),
            ai_text_character_limit=int(
                os.getenv("NEWS_DUPLICATE_AI_TEXT_LIMIT", "12000")
            ),
            cache_namespace=cache_namespace,
        )

    def validate(self) -> None:
        if self.candidate_limit < 1 or self.candidate_limit > 10:
            raise ValueError("News duplicate candidate limit must be between 1 and 10.")
        if not 0 <= self.local_similarity_threshold <= 1:
            raise ValueError("News duplicate similarity threshold must be between 0 and 1.")
        if not 0.5 <= self.ai_duplicate_min_confidence <= 1:
            raise ValueError(
                "News duplicate AI minimum confidence must be between 0.5 and 1."
            )
        if self.ai_text_character_limit < 1000:
            raise ValueError("News duplicate AI text limit must be at least 1000 characters.")


class NewsArchive:
    def __init__(self, settings: ArchiveSettings | None = None) -> None:
        self.settings = settings or ArchiveSettings()
        self.settings.validate()
        self.root = self.settings.root.resolve()
        self.articles_root = self.root / "articles"
        self.summaries_root = self.root / "summaries"
        self.index_path = self.root / "index.json"
        self.cache_path = self.root / "similarity_cache.json"

    def ensure_directories(self) -> None:
        self.articles_root.mkdir(parents=True, exist_ok=True)
        self.summaries_root.mkdir(parents=True, exist_ok=True)

    def _load_json(self, path: Path, fallback: dict) -> dict:
        if not path.is_file():
            return fallback
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"News archive data is unreadable: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"News archive data must be a JSON object: {path}")
        return payload

    def load_index(self) -> dict:
        payload = self._load_json(
            self.index_path,
            {"schema_version": SCHEMA_VERSION, "updated_at_utc": "", "records": {}},
        )
        payload.setdefault("records", {})
        return payload

    def load_cache(self) -> dict:
        payload = self._load_json(
            self.cache_path,
            {"schema_version": SCHEMA_VERSION, "updated_at_utc": "", "comparisons": {}},
        )
        payload.setdefault("comparisons", {})
        return payload

    def _save_index(self, payload: dict) -> None:
        payload["schema_version"] = SCHEMA_VERSION
        payload["updated_at_utc"] = utc_now()
        atomic_write_json(self.index_path, payload)

    def _save_cache(self, payload: dict) -> None:
        payload["schema_version"] = SCHEMA_VERSION
        payload["updated_at_utc"] = utc_now()
        atomic_write_json(self.cache_path, payload)

    @staticmethod
    def _is_reference_record(record: dict, active_record_ids: set[str]) -> bool:
        return bool(record.get("uses")) or record.get("archive_id") in active_record_ids

    def _candidate_records(
        self,
        *,
        index: dict,
        archive_id: str,
        canonical_url: str,
        title: str,
        article_text: str,
        active_record_ids: set[str],
    ) -> list[dict]:
        scored: list[tuple[float, dict]] = []
        for record in index["records"].values():
            if record.get("archive_id") == archive_id:
                continue
            if not self._is_reference_record(record, active_record_ids):
                continue
            score = local_similarity(
                new_title=title,
                new_text=article_text,
                previous_title=str(record.get("title", "")),
                previous_terms=list(record.get("search_terms", [])),
            )
            same_url = bool(record.get("canonical_url")) and (
                record.get("canonical_url") == canonical_url
            )
            if score >= self.settings.local_similarity_threshold or same_url:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].get("first_seen_utc", "")), reverse=True)
        return [record for _, record in scored[: self.settings.candidate_limit]]

    def _cache_key(self, content_hash: str, candidates: list[dict]) -> str:
        comparison_material = {
            "namespace": self.settings.cache_namespace,
            "new_content_sha256": content_hash,
            "candidate_content_sha256": sorted(
                str(item.get("content_sha256", "")) for item in candidates
            ),
        }
        return sha256_text(json.dumps(comparison_material, sort_keys=True))

    def _ai_payload(self, record: dict, candidates: list[dict]) -> dict:
        limit = self.settings.ai_text_character_limit
        previous: list[dict] = []
        for item in candidates:
            raw_source = str(item.get("source_file", "")).strip()
            source_path = (
                resolve_portable_path(raw_source) if raw_source else Path("")
            )
            previous_text = str(item.get("_article_text", ""))
            if raw_source and source_path.is_file():
                previous_text = source_path.read_text(encoding="utf-8")
            if not previous_text:
                continue
            previous.append(
                {
                    "archive_id": item["archive_id"],
                    "file_name": source_path.name,
                    "title": item.get("title", ""),
                    "source": item.get("source", ""),
                    "published_utc": item.get("published_utc", ""),
                    "source_url": item.get("source_url", ""),
                    "content_sha256": item.get("content_sha256", ""),
                    "text": previous_text[:limit],
                }
            )
        raw_current = str(record.get("source_file", "")).strip()
        current_path = (
            resolve_portable_path(raw_current) if raw_current else Path("")
        )
        current_text = str(record.get("_article_text", ""))
        if raw_current and current_path.is_file():
            current_text = current_path.read_text(encoding="utf-8")
        return {
            "new_file": {
                "archive_id": record["archive_id"],
                "file_name": current_path.name,
                "title": record.get("title", ""),
                "source": record.get("source", ""),
                "published_utc": record.get("published_utc", ""),
                "source_url": record.get("source_url", ""),
                "content_sha256": record.get("content_sha256", ""),
                "text": current_text[:limit],
            },
            "previous_files": previous,
        }

    def inspect_and_store(
        self,
        *,
        canonical_url: str,
        title: str,
        source: str,
        source_url: str,
        published_utc: str,
        category: str,
        article_text: str,
        video_name: str,
        active_record_ids: set[str] | None = None,
        compare_with_ai: Callable[[dict], dict] | None = None,
        skip_duplicate_check: bool = False,
        persist: bool = True,
    ) -> dict:
        if persist:
            self.ensure_directories()
        active_ids = set(active_record_ids or set())
        normalized = normalize_text(article_text)
        content_hash = sha256_text(normalized)
        story_id = stable_story_id(canonical_url)
        archive_id = f"pulled_{sha256_text(canonical_url + chr(10) + content_hash)[:16]}"
        filename = f"{story_id}__sha256-{content_hash[:16]}__{_slug(title)}.txt"
        source_path = self.articles_root / filename
        if persist and not source_path.is_file():
            atomic_write_text(source_path, article_text.strip() + "\n")

        index = self.load_index()
        existing = index["records"].get(archive_id, {})
        pulls = list(existing.get("pulls", []))
        observation = {"video_name": video_name, "seen_at_utc": utc_now()}
        if not any(item.get("video_name") == video_name for item in pulls):
            pulls.append(observation)
        record = {
            **existing,
            "archive_id": archive_id,
            "story_id": story_id,
            "content_sha256": content_hash,
            "canonical_url": canonical_url,
            "source_url": source_url,
            "title": title,
            "source": source,
            "published_utc": published_utc,
            "category": category,
            "source_file": str(source_path),
            "source_filename": filename,
            "search_terms": _search_terms(f"{title} {normalized}"),
            "first_seen_utc": existing.get("first_seen_utc") or utc_now(),
            "last_seen_utc": utc_now(),
            "pulls": pulls,
            "uses": list(existing.get("uses", [])),
            "_article_text": article_text if not persist else "",
        }
        if persist:
            index["records"][archive_id] = record

        exact_matches = [
            item
            for item in index["records"].values()
            if item.get("archive_id") != archive_id
            and self._is_reference_record(item, active_ids)
            and item.get("content_sha256") == content_hash
        ]
        if skip_duplicate_check:
            decision = {
                "is_duplicate": False,
                "method": "historical_backfill",
                "confidence": 1.0,
                "duplicate_archive_id": "",
                "reason": "Existing completed project imported without a paid comparison.",
                "ai_request_performed": False,
                "cache_hit": False,
            }
        elif exact_matches:
            decision = {
                "is_duplicate": True,
                "method": "exact_content_sha256",
                "confidence": 1.0,
                "duplicate_archive_id": exact_matches[0]["archive_id"],
                "reason": "Normalized source text has the same SHA-256 as an earlier news file.",
                "ai_request_performed": False,
                "cache_hit": False,
            }
        else:
            candidates = self._candidate_records(
                index=index,
                archive_id=archive_id,
                canonical_url=canonical_url,
                title=title,
                article_text=article_text,
                active_record_ids=active_ids,
            )
            if not candidates:
                decision = {
                    "is_duplicate": False,
                    "method": "local_no_match",
                    "confidence": 1.0,
                    "duplicate_archive_id": "",
                    "reason": "No sufficiently similar prior used news file was found locally.",
                    "ai_request_performed": False,
                    "cache_hit": False,
                }
            else:
                cache = self.load_cache()
                cache_key = self._cache_key(content_hash, candidates)
                cached = cache["comparisons"].get(cache_key)
                if cached:
                    decision = {**cached["decision"], "ai_request_performed": False, "cache_hit": True}
                else:
                    if compare_with_ai is None:
                        raise RuntimeError(
                            "AI duplicate comparison is required for plausible cross-feed matches."
                        )
                    payload = self._ai_payload(record, candidates)
                    if not payload["previous_files"]:
                        raise RuntimeError("Previous archive files required for AI comparison are missing.")
                    ai_decision = compare_with_ai(payload)
                    allowed_ids = {
                        item["archive_id"] for item in payload["previous_files"]
                    }
                    duplicate_id = str(ai_decision.get("duplicate_archive_id", ""))
                    raw_duplicate = bool(ai_decision.get("is_duplicate", False))
                    if raw_duplicate and duplicate_id not in allowed_ids:
                        raise RuntimeError("AI duplicate decision referenced an unknown archive ID.")
                    confidence = float(ai_decision.get("confidence", 0.0))
                    if not 0 <= confidence <= 1:
                        raise RuntimeError("AI duplicate confidence must be between 0 and 1.")
                    same_event = bool(
                        ai_decision.get("same_discrete_event", raw_duplicate)
                    )
                    core_facts_match = bool(
                        ai_decision.get("core_facts_match", raw_duplicate)
                    )
                    materially_new = bool(
                        ai_decision.get("materially_new_development", False)
                    )
                    is_duplicate = bool(
                        raw_duplicate
                        and same_event
                        and core_facts_match
                        and not materially_new
                        and confidence >= self.settings.ai_duplicate_min_confidence
                    )
                    reason = str(ai_decision.get("reason", "")).strip()
                    if raw_duplicate and not is_duplicate:
                        reason = (
                            "Allowed by the relaxed duplicate gate: related context is "
                            "not sufficient without a high-confidence match to the same "
                            f"discrete event and core facts. AI review: {reason}"
                        )
                    decision = {
                        "is_duplicate": is_duplicate,
                        "method": "ai_cross_feed_comparison",
                        "confidence": confidence,
                        "duplicate_archive_id": duplicate_id if is_duplicate else "",
                        "reason": reason,
                        "raw_ai_duplicate": raw_duplicate,
                        "same_discrete_event": same_event,
                        "core_facts_match": core_facts_match,
                        "materially_new_development": materially_new,
                        "minimum_confidence_required": (
                            self.settings.ai_duplicate_min_confidence
                        ),
                        "ai_request_performed": True,
                        "cache_hit": False,
                    }
                    cache["comparisons"][cache_key] = {
                        "created_at_utc": utc_now(),
                        "new_content_sha256": content_hash,
                        "candidate_archive_ids": sorted(allowed_ids),
                        "decision": {
                            key: value
                            for key, value in decision.items()
                            if key not in {"ai_request_performed", "cache_hit"}
                        },
                    }
                    if persist:
                        self._save_cache(cache)

        record["latest_duplicate_decision"] = decision
        record.pop("_article_text", None)
        if persist:
            index["records"][archive_id] = record
            self._save_index(index)
        return {"record": record, "decision": decision}

    def finalize_project(self, video_number: int) -> dict:
        """Archive news only after a checksum/QA-verified final render exists."""
        video_root = project_dir(video_number)
        render_report_path = video_root / "08_render" / "render_report.json"
        project_info_path = video_root / "project_info.json"
        if not render_report_path.is_file() or not project_info_path.is_file():
            raise RuntimeError("Project news cannot be archived before the final render exists.")
        render_report = self._load_json(render_report_path, {})
        if render_report.get("quality", {}).get("status") != "passed":
            raise RuntimeError("Project news cannot be archived before final render QA passes.")
        project = self._load_json(project_info_path, {})
        video_name = str(project.get("video_name") or project_name(video_number))
        items: list[dict] = []
        used_urls: set[str] = set()
        for story in sorted(project.get("stories", []), key=lambda item: int(item["order"])):
            raw_source = str(story.get("original", "")).strip()
            raw_narration = str(story.get("text", "")).strip()
            source_path = resolve_portable_path(raw_source) if raw_source else Path("")
            narration_path = resolve_portable_path(raw_narration) if raw_narration else Path("")
            if (
                not raw_source
                or not raw_narration
                or not source_path.is_file()
                or not narration_path.is_file()
            ):
                raise RuntimeError(f"Completed project story files are missing: {story.get('story_id')}")
            result = self.inspect_and_store(
                canonical_url=str(story.get("source_url", "")),
                title=str(story.get("source_title") or story.get("title") or "Untitled news"),
                source=str(story.get("source", "")),
                source_url=str(story.get("source_url", "")),
                published_utc=str(story.get("published_utc", "")),
                category=str(project.get("category", "tech")),
                article_text=source_path.read_text(encoding="utf-8"),
                video_name=video_name,
                skip_duplicate_check=True,
            )
            items.append({
                "archive_id": result["record"]["archive_id"],
                "order": int(story["order"]),
                "narration": narration_path.read_text(encoding="utf-8"),
            })
            if story.get("source_url"):
                used_urls.add(str(story["source_url"]))
        updated = self.mark_project_used(video_name=video_name, items=items)
        history = self._load_json(PROCESSED_URLS_FILE, {"used_urls": []})
        used_urls.update(str(item) for item in history.get("used_urls", []))
        atomic_write_json(PROCESSED_URLS_FILE, {
            "updated_at": utc_now(),
            "last_video": video_name,
            "used_urls": sorted(used_urls),
        })
        marker = video_root / "orchestration" / "news_archive_finalized.json"
        atomic_write_json(marker, {
            "status": "ARCHIVE_FINALIZED",
            "video_name": video_name,
            "finalized_at_utc": utc_now(),
            "record_count": len(updated),
            "archive_ids": [item["archive_id"] for item in updated],
        })
        return {"status": "ARCHIVE_FINALIZED", "record_count": len(updated), "marker": str(marker)}

    def mark_used(
        self,
        *,
        archive_id: str,
        video_name: str,
        order: int,
        narration: str,
    ) -> dict:
        records = self.mark_project_used(
            video_name=video_name,
            items=[
                {
                    "archive_id": archive_id,
                    "order": order,
                    "narration": narration,
                }
            ],
        )
        return records[0]

    def mark_project_used(self, *, video_name: str, items: list[dict]) -> list[dict]:
        """Record all stories from one completed project in one index update."""
        self.ensure_directories()
        index = self.load_index()
        prepared: list[tuple[str, dict, dict]] = []
        used_at = utc_now()
        for item in items:
            archive_id = str(item["archive_id"])
            record = index["records"].get(archive_id)
            if record is None:
                raise KeyError(f"News archive record does not exist: {archive_id}")
            narration = str(item["narration"])
            summary_name = Path(str(record["source_filename"])).with_suffix(
                ".summary.txt"
            ).name
            summary_path = self.summaries_root / summary_name
            use = {
                "video_name": video_name,
                "order": int(item["order"]),
                "used_at_utc": used_at,
                "summary_file": str(summary_path),
                "summary_sha256": content_sha256(narration),
            }
            prepared.append((narration, record, use))

        updated: list[dict] = []
        for narration, record, use in prepared:
            summary_path = Path(use["summary_file"])
            atomic_write_text(summary_path, narration.strip() + "\n")
            uses = [
                item
                for item in record.get("uses", [])
                if item.get("video_name") != video_name
            ]
            uses.append(use)
            record["uses"] = uses
            record["last_used_utc"] = used_at
            index["records"][record["archive_id"]] = record
            updated.append(record)
        self._save_index(index)
        return updated

    def backfill_projects(self, projects_root: Path = PROJECTS_ROOT) -> dict:
        imported = 0
        skipped = 0
        skipped_by_history_reset = 0
        already_present = 0
        reset_policy = self._load_json(NEWS_HISTORY_RESET_FILE, {})
        excluded_projects = {
            str(value)
            for value in reset_policy.get("excluded_backfill_projects", [])
        }
        existing = self.load_index()
        known_uses = {
            (
                str(record.get("canonical_url", "")),
                str(record.get("content_sha256", "")),
                str(use.get("video_name", "")),
            )
            for record in existing["records"].values()
            for use in record.get("uses", [])
        }
        for project_dir in sorted(projects_root.glob("Video *")):
            if project_dir.name in excluded_projects:
                skipped_by_history_reset += 1
                continue
            manifest_path = project_dir / "project_info.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                skipped += 1
                continue
            if manifest.get("status") != "complete":
                continue
            render_report = project_dir / "08_render" / "render_report.json"
            if not render_report.is_file():
                continue
            try:
                if json.loads(render_report.read_text(encoding="utf-8")).get("quality", {}).get("status") != "passed":
                    continue
            except (OSError, json.JSONDecodeError):
                continue
            for story in manifest.get("stories", []):
                raw_source = str(story.get("original", "")).strip()
                raw_summary = str(story.get("text", "")).strip()
                if not raw_source or not raw_summary:
                    skipped += 1
                    continue
                source_path = resolve_portable_path(raw_source, video_dir=project_dir)
                summary_path = resolve_portable_path(raw_summary, video_dir=project_dir)
                if not source_path.is_file() or not summary_path.is_file():
                    skipped += 1
                    continue
                source_url = str(story.get("source_url", "")).strip()
                canonical = source_url
                article_text = source_path.read_text(encoding="utf-8")
                identity = (
                    canonical,
                    content_sha256(article_text),
                    str(manifest.get("video_name") or project_dir.name),
                )
                if identity in known_uses:
                    already_present += 1
                    continue
                result = self.inspect_and_store(
                    canonical_url=canonical,
                    title=str(story.get("source_title") or story.get("title") or "Untitled news"),
                    source=str(story.get("source", "")),
                    source_url=source_url,
                    published_utc=str(story.get("published_utc", "")),
                    category=str(manifest.get("category", "tech")),
                    article_text=article_text,
                    video_name=str(manifest.get("video_name") or project_dir.name),
                    compare_with_ai=None,
                    skip_duplicate_check=True,
                )
                self.mark_used(
                    archive_id=result["record"]["archive_id"],
                    video_name=str(manifest.get("video_name") or project_dir.name),
                    order=int(story.get("order", 0)),
                    narration=summary_path.read_text(encoding="utf-8"),
                )
                imported += 1
                known_uses.add(identity)
        return {
            "imported": imported,
            "already_present": already_present,
            "skipped": skipped,
            "skipped_by_history_reset": skipped_by_history_reset,
            "archive_root": str(self.root),
        }

    def status(self) -> dict:
        index = self.load_index()
        records = list(index["records"].values())
        return {
            "archive_root": str(self.root),
            "record_count": len(records),
            "used_record_count": sum(bool(item.get("uses")) for item in records),
            "source_file_count": sum(
                bool(str(item.get("source_file", "")).strip())
                and resolve_portable_path(str(item.get("source_file", ""))).is_file()
                for item in records
            ),
            "comparison_cache_count": len(self.load_cache()["comparisons"]),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or backfill the pulled-news archive.")
    parser.add_argument("command", choices=("status", "backfill"))
    args = parser.parse_args()
    archive = NewsArchive()
    result = archive.backfill_projects() if args.command == "backfill" else archive.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
