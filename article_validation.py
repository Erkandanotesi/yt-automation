from __future__ import annotations

import re
import unicodedata
from collections import Counter

from file_utils import sha256_text


TITLE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "more",
    "new",
    "not",
    "of",
    "on",
    "or",
    "our",
    "than",
    "that",
    "the",
    "their",
    "this",
    "to",
    "up",
    "with",
}


def normalize_search_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.casefold()).strip()


def significant_title_tokens(title: str) -> list[str]:
    return [
        token
        for token in normalize_search_text(title).split()
        if token not in TITLE_STOP_WORDS and len(token) >= 3
    ]


def title_source_relevance(title: str, source_text: str) -> dict:
    title_tokens = significant_title_tokens(title)
    source_tokens = set(normalize_search_text(source_text).split())
    matched = [token for token in title_tokens if token in source_tokens]
    missing = [token for token in title_tokens if token not in source_tokens]
    ratio = len(matched) / max(1, len(title_tokens))
    required_matches = 1 if len(title_tokens) <= 2 else 2
    passed = (
        len(source_text.strip()) >= 200
        and len(matched) >= required_matches
        and ratio >= 0.35
    )
    return {
        "status": "passed" if passed else "failed",
        "title_tokens": title_tokens,
        "matched_tokens": matched,
        "missing_tokens": missing,
        "match_ratio": round(ratio, 4),
        "required_matches": required_matches,
    }


def source_matches_title(title: str, source_text: str) -> bool:
    return title_source_relevance(title, source_text)["status"] == "passed"


def source_content_fingerprint(source_text: str) -> str:
    return sha256_text(normalize_search_text(source_text))


def duplicate_source_fingerprints(items: list[dict]) -> dict[str, list[str]]:
    fingerprints = [
        source_content_fingerprint(str(item.get("source_text", "")))
        for item in items
    ]
    counts = Counter(fingerprints)
    duplicates: dict[str, list[str]] = {}
    for item, fingerprint in zip(items, fingerprints, strict=True):
        if counts[fingerprint] <= 1:
            continue
        duplicates.setdefault(fingerprint, []).append(
            str(item.get("story_id") or item.get("title") or "unknown")
        )
    return duplicates


def validate_unique_source_content(items: list[dict]) -> None:
    duplicates = duplicate_source_fingerprints(items)
    if not duplicates:
        return
    groups = [", ".join(values) for values in duplicates.values()]
    raise ValueError(
        "Different stories resolved to identical source content: "
        + "; ".join(groups)
    )
