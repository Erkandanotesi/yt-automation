from __future__ import annotations

import re
from typing import Any


MIN_NARRATION_WORDS = 150
MAX_NARRATION_WORDS = 200
MIN_SOURCE_CHARACTERS = 200


class ContentValidationError(ValueError):
    pass


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\wÀ-ž'-]+\b", text, flags=re.UNICODE))


def _searchable(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


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


def _validate_evidence_list(
    label: str,
    values: list[str],
    source_searchable: str,
) -> None:
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if cleaned and cleaned.casefold() not in source_searchable:
            raise ContentValidationError(
                f"Factual evidence item is not present in the source "
                f"({label}): {cleaned}"
            )


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
    source_numbers = {token.casefold() for token in _numeric_tokens(source_text)}
    unsupported_numbers = sorted(
        token
        for token in _numeric_tokens(narration)
        if token.casefold() not in source_numbers
    )
    if unsupported_numbers:
        raise ContentValidationError(
            "Narration contains numbers absent from the source: "
            + ", ".join(unsupported_numbers)
        )

    unsupported_quotes = [
        quote
        for quote in _direct_quotes(narration)
        if _searchable(quote) not in source_searchable
    ]
    if unsupported_quotes:
        raise ContentValidationError(
            "Narration contains a direct quote absent from the source: "
            + unsupported_quotes[0]
        )

    review = factual_review or {}
    for key in (
        "names_entities_checked",
        "numbers_dates_checked",
        "quotes_checked",
        "attributions_preserved",
    ):
        values = review.get(key, [])
        if not isinstance(values, list):
            raise ContentValidationError(f"Factual review field {key} must be a list.")
        _validate_evidence_list(key, values, source_searchable)

    names_checked = review.get("names_entities_checked", [])
    if not names_checked:
        raise ContentValidationError(
            "Factual review must record at least one checked name or entity."
        )

    narration_numbers = _numeric_tokens(narration)
    numbers_checked = " ".join(review.get("numbers_dates_checked", [])).casefold()
    missing_number_evidence = sorted(
        token for token in narration_numbers if token.casefold() not in numbers_checked
    )
    if missing_number_evidence:
        raise ContentValidationError(
            "Factual review did not record narration numbers/dates: "
            + ", ".join(missing_number_evidence)
        )

    narration_quotes = _direct_quotes(narration)
    checked_quotes = {
        _searchable(value) for value in review.get("quotes_checked", [])
    }
    missing_quote_evidence = [
        quote for quote in narration_quotes if _searchable(quote) not in checked_quotes
    ]
    if missing_quote_evidence:
        raise ContentValidationError(
            "Factual review did not record a narration quotation: "
            + missing_quote_evidence[0]
        )

    return {
        "status": "passed",
        "word_count": count,
        "source_characters": len(source_text),
        "checked_numbers": sorted(narration_numbers),
        "checked_direct_quotes": narration_quotes,
        "factual_review": review,
    }
