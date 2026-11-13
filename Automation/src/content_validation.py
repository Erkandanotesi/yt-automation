from __future__ import annotations

import os
import re
from typing import Any


def _positive_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


# Keep every caller, prompt, local trim, and validation gate on one policy.
# The target is editorial guidance; the min/max range is the enforceable gate.
MIN_NARRATION_WORDS = _positive_env_int("NARRATION_MIN_WORDS", 350)
TARGET_NARRATION_WORDS = _positive_env_int("NARRATION_TARGET_WORDS", 360)
MAX_NARRATION_WORDS = _positive_env_int("NARRATION_MAX_WORDS", 390)
MIN_SOURCE_CHARACTERS = 200

if not MIN_NARRATION_WORDS <= TARGET_NARRATION_WORDS <= MAX_NARRATION_WORDS:
    raise ValueError(
        "NARRATION_MIN_WORDS <= NARRATION_TARGET_WORDS <= "
        "NARRATION_MAX_WORDS is required."
    )


class ContentValidationError(ValueError):
    pass


def narration_length_guidance() -> str:
    """Return the single editorial instruction used in every summary prompt."""
    return (
        f"Aim for about {TARGET_NARRATION_WORDS} words. "
        f"Use at least {MIN_NARRATION_WORDS} words and never exceed "
        f"{MAX_NARRATION_WORDS} words."
    )


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\wÀ-ž'-]+\b", text, flags=re.UNICODE))


def _normalize_narration_paragraphs(text: str) -> str:
    """Collapse excess spaces within paragraphs while keeping blank-line beat breaks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [
        re.sub(r"\s+", " ", para).strip()
        for para in re.split(r"\n\s*\n+", normalized)
    ]
    return "\n\n".join(para for para in paragraphs if para)


def trim_narration_to_word_limit(
    text: str,
    max_words: int = MAX_NARRATION_WORDS,
) -> str:
    """Trim overlong model output locally, preferring complete sentences."""
    cleaned = _normalize_narration_paragraphs(text)
    if word_count(cleaned) <= max_words:
        return cleaned

    kept_paragraphs: list[str] = []
    running_count = 0
    stopped = False
    for para in cleaned.split("\n\n"):
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept_sentences: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            candidate_count = running_count + word_count(sentence)
            if candidate_count > max_words:
                stopped = True
                break
            kept_sentences.append(sentence)
            running_count = candidate_count
        if kept_sentences:
            kept_paragraphs.append(" ".join(kept_sentences))
        if stopped:
            break

    complete = "\n\n".join(kept_paragraphs).strip()
    if word_count(complete) >= MIN_NARRATION_WORDS:
        return complete

    # Rare fallback for an unusually long single sentence. The result still
    # goes through the normal source-grounding checks afterward.
    flat = re.sub(r"\s+", " ", cleaned).strip()
    matches = list(re.finditer(r"\b[\wÀ-ž'-]+\b", flat, flags=re.UNICODE))
    cutoff = matches[max_words - 1].end()
    return flat[:cutoff].rstrip(" ,;:-") + "."


def _searchable(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _evidence_searchable(text: str) -> str:
    """Normalize harmless quote/punctuation differences in audit metadata."""
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip().casefold()


def _numeric_tokens(text: str) -> set[str]:
    return set(
        re.findall(
            r"(?<![\w])(?:[$€£]?\d(?:[\d,.]*\d)?(?:%|[a-z]{2,4})?)(?![\w])",
            text,
            flags=re.IGNORECASE,
        )
    )


def _direct_quotes(text: str) -> list[str]:
    matches = re.findall(r'[“"]([^”"]{4,})[”"]', text)
    return [re.sub(r"\s+", " ", item).strip() for item in matches]


def _ground_evidence_list(
    values: list[str],
    source_searchable: str,
) -> tuple[list[str], list[str]]:
    normalized_source = _evidence_searchable(source_searchable)
    grounded: list[str] = []
    rejected: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip()
        normalized_value = _evidence_searchable(cleaned)
        if normalized_value and normalized_value not in normalized_source:
            rejected.append(cleaned)
        elif cleaned:
            grounded.append(cleaned)
    return grounded, rejected


def validate_narration(
    *,
    title: str,
    narration: str,
    source_text: str,
    factual_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title = title.strip()
    narration = narration.strip()
    source_text = source_text.strip()

    if not title:
        raise ContentValidationError("English title is empty.")
    if not narration:
        raise ContentValidationError("Narration is empty.")
    if len(source_text) < MIN_SOURCE_CHARACTERS:
        raise ContentValidationError(
            f"Source has {len(source_text)} characters; "
            f"at least {MIN_SOURCE_CHARACTERS} are required."
        )

    count = word_count(narration)
    if not MIN_NARRATION_WORDS <= count <= MAX_NARRATION_WORDS:
        raise ContentValidationError(
            f"Narration has {count} words; required range is "
            f"{MIN_NARRATION_WORDS}-{MAX_NARRATION_WORDS}."
        )

    if re.search(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", narration):
        raise ContentValidationError("Narration contains a list or bullet.")
    if re.search(
        r"\[(?:music|pause|scene|visual|sfx|voice|cut)\]",
        narration,
        flags=re.IGNORECASE,
    ):
        raise ContentValidationError("Narration contains stage directions.")

    source_searchable = _searchable(source_text)
    source_for_quotes = _evidence_searchable(source_text)
    unsupported_quotes = [
        quote
        for quote in _direct_quotes(narration)
        if _evidence_searchable(quote) not in source_for_quotes
    ]
    if unsupported_quotes:
        raise ContentValidationError(
            "Narration contains a direct quote absent from the source: "
            + unsupported_quotes[0]
        )

    review = dict(factual_review or {})
    discarded_review_items: dict[str, list[str]] = {}
    for key in (
        "names_entities_checked",
        "numbers_dates_checked",
        "quotes_checked",
        "attributions_preserved",
    ):
        values = review.get(key, [])
        if not isinstance(values, list):
            raise ContentValidationError(f"Factual review field {key} must be a list.")
        grounded, rejected = _ground_evidence_list(values, source_searchable)
        review[key] = grounded
        if rejected:
            discarded_review_items[key] = rejected

    names_checked = review.get("names_entities_checked", [])
    if not names_checked:
        raise ContentValidationError(
            "Factual review must record at least one checked name or entity."
        )

    # Numbers and direct quotations are already checked directly against the
    # source above. The model's factual_review is useful audit metadata, but an
    # incomplete audit list must not trigger another paid rewrite of otherwise
    # valid, source-grounded narration.
    narration_numbers = _numeric_tokens(narration)
    narration_quotes = _direct_quotes(narration)

    return {
        "status": "passed",
        "word_count": count,
        "source_characters": len(source_text),
        "checked_numbers": sorted(narration_numbers),
        "checked_direct_quotes": narration_quotes,
        "factual_review": review,
        "discarded_ungrounded_review_items": discarded_review_items,
    }
