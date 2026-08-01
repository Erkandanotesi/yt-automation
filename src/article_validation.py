from __future__ import annotations

import re
import textwrap
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

STRONG_BROKEN_PAGE_PATTERNS = (
    ("access_denied", re.compile(r"^\s*(?:error\s*\d+\s*)?access denied\b", re.I)),
    ("captcha", re.compile(r"^\s*(?:please\s+)?(?:verify you are human|complete the captcha)\b", re.I)),
    ("javascript_wall", re.compile(r"^\s*enable javascript and cookies to continue\b", re.I)),
    ("cloudflare_error", re.compile(r"\bcloudflare ray id\b", re.I)),
    ("cloudflare_wait", re.compile(r"^\s*just a moment\.\.\.\s*", re.I)),
    ("missing_page", re.compile(r"^\s*(?:error\s*)?(?:404|page) not found\b", re.I)),
    ("service_unavailable", re.compile(r"^\s*(?:service temporarily unavailable|bad gateway)\b", re.I)),
    ("robots_denied", re.compile(r"^\s*blocked by robots\.txt\b", re.I)),
    (
        "paywall",
        re.compile(r"\b(?:subscribe|sign in|register) to (?:continue|keep) reading\b", re.I),
    ),
    ("automated_access_block", re.compile(r"\bautomated access (?:has been )?blocked\b", re.I)),
)
SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘(])")


def _split_long_paragraph(paragraph: str, target: int = 520, maximum: int = 760) -> list[str]:
    if len(paragraph) <= maximum:
        return [paragraph]
    sentences = SENTENCE_BOUNDARY.split(paragraph)
    if len(sentences) == 1:
        return textwrap.wrap(
            paragraph,
            width=maximum,
            break_long_words=False,
            break_on_hyphens=False,
        )
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for sentence in sentences:
        added = len(sentence) + (1 if current else 0)
        if current and current_length + added > maximum:
            chunks.append(" ".join(current))
            current = []
            current_length = 0
        current.append(sentence)
        current_length += added
        if current_length >= target:
            chunks.append(" ".join(current))
            current = []
            current_length = 0
    if current:
        chunks.append(" ".join(current))
    return chunks


def format_article_text(source_text: str, *, maximum_characters: int = 35000) -> str:
    """Normalize extracted prose into stable, readable article paragraphs."""
    normalized = unicodedata.normalize("NFKC", source_text).replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")
    raw_paragraphs = re.split(r"\n\s*\n|(?<!\n)\n(?!\n)", normalized)
    paragraphs: list[str] = []
    previous = ""
    for raw in raw_paragraphs:
        paragraph = re.sub(r"\s+", " ", raw).strip()
        if not paragraph or paragraph == previous:
            continue
        previous = paragraph
        paragraphs.extend(_split_long_paragraph(paragraph))

    formatted = "\n\n".join(paragraphs).strip()
    if len(formatted) <= maximum_characters:
        return formatted
    shortened = formatted[:maximum_characters].rstrip()
    last_break = shortened.rfind("\n\n")
    if last_break >= maximum_characters // 2:
        shortened = shortened[:last_break]
    return shortened.rstrip()


def article_quality_report(
    title: str,
    source_text: str,
    *,
    minimum_characters: int = 500,
) -> dict:
    text = source_text.strip()
    words = normalize_search_text(text).split()
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text))
        if len(item.strip()) >= 20
    ]
    errors: list[str] = []
    warnings: list[str] = []

    if len(text) < minimum_characters:
        errors.append(
            f"source_too_short:{len(text)}<{minimum_characters}"
        )
    if len(words) < 80:
        errors.append(f"too_few_words:{len(words)}<80")
    if len(sentences) < 4:
        errors.append(f"too_few_sentences:{len(sentences)}<4")

    opening = re.sub(r"\s+", " ", text[:1200])
    inspection_text = opening + " " + re.sub(r"\s+", " ", text[-700:])
    broken_markers = [
        label
        for label, pattern in STRONG_BROKEN_PAGE_PATTERNS
        if pattern.search(inspection_text)
    ]
    if broken_markers:
        errors.append("broken_page_marker:" + ",".join(broken_markers))

    if words:
        unique_ratio = len(set(words)) / len(words)
        if len(words) >= 100 and unique_ratio < 0.18:
            errors.append(f"low_lexical_diversity:{unique_ratio:.3f}<0.18")
    else:
        unique_ratio = 0.0

    normalized_sentences = [normalize_search_text(item) for item in sentences]
    counts = Counter(item for item in normalized_sentences if len(item) >= 35)
    repeated_sentence_count = sum(count - 1 for count in counts.values() if count > 1)
    repetition_ratio = repeated_sentence_count / max(1, len(normalized_sentences))
    if repeated_sentence_count >= 3 and repetition_ratio >= 0.25:
        errors.append(f"repeated_prose:{repetition_ratio:.3f}>=0.25")

    relevance = title_source_relevance(title, text)
    if relevance["status"] != "passed":
        errors.append(
            "title_source_mismatch:"
            f"ratio={relevance['match_ratio']},matches={len(relevance['matched_tokens'])}"
        )

    paragraphs = [item for item in re.split(r"\n\s*\n", text) if item.strip()]
    if len(paragraphs) == 1 and len(text) > 1200:
        warnings.append("single_long_paragraph")

    return {
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "warnings": warnings,
        "characters": len(text),
        "words": len(words),
        "sentences": len(sentences),
        "paragraphs": len(paragraphs),
        "unique_word_ratio": round(unique_ratio, 4),
        "repetition_ratio": round(repetition_ratio, 4),
        "title_relevance": relevance,
    }


def validate_article_quality(
    title: str,
    source_text: str,
    *,
    minimum_characters: int = 500,
) -> dict:
    report = article_quality_report(
        title,
        source_text,
        minimum_characters=minimum_characters,
    )
    if report["status"] != "passed":
        raise ValueError("Broken or non-article source rejected: " + "; ".join(report["errors"]))
    return report


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
