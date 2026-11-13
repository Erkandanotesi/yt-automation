from __future__ import annotations

import difflib
import math
import re
from datetime import datetime, timezone
from typing import Any

from file_utils import sha256_text


ALIGNMENT_SCHEMA_VERSION = 1
ALIGNMENT_BUILDER_VERSION = 1
EMOTION_TAG_PATTERN = re.compile(r"\[([a-zA-Z][a-zA-Z\s_-]{0,30})\]")


class AlignmentValidationError(ValueError):
    pass


COMMON_SENTENCE_STARTS = {
    "A",
    "An",
    "And",
    "As",
    "At",
    "But",
    "For",
    "However",
    "If",
    "In",
    "It",
    "Meanwhile",
    "Now",
    "So",
    "Still",
    "That",
    "The",
    "This",
    "Those",
    "To",
    "While",
    "With",
}

KNOWN_PLACES = {
    "America",
    "Asia",
    "California",
    "Canada",
    "China",
    "Europe",
    "European Union",
    "France",
    "Germany",
    "India",
    "Japan",
    "South Korea",
    "Turkey",
    "United Kingdom",
    "United States",
    "Washington",
}

ORGANIZATION_HINTS = {
    "AI",
    "AMD",
    "Amazon",
    "Android",
    "Anthropic",
    "Apple",
    "ChatGPT",
    "ElevenLabs",
    "Google",
    "Intel",
    "Meta",
    "Microsoft",
    "Nvidia",
    "OpenAI",
    "Samsung",
    "Steam",
    "Valve",
    "Windows",
    "YouTube",
}

STOP_WORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "because",
    "been",
    "before",
    "being",
    "between",
    "could",
    "from",
    "have",
    "into",
    "more",
    "most",
    "only",
    "other",
    "over",
    "said",
    "says",
    "some",
    "such",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "through",
    "under",
    "very",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
}

HIGH_IMPACT_TERMS = {
    "announced",
    "ban",
    "breach",
    "court",
    "data",
    "deal",
    "government",
    "hack",
    "launch",
    "lawsuit",
    "million",
    "new",
    "privacy",
    "released",
    "security",
    "shutdown",
    "vulnerability",
}


def alignment_input_fingerprint(
    *,
    text: str,
    audio_sha256: str,
) -> str:
    from file_utils import json_fingerprint

    return json_fingerprint(
        {
            "schema_version": ALIGNMENT_SCHEMA_VERSION,
            "builder_version": ALIGNMENT_BUILDER_VERSION,
            "text_sha256": sha256_text(text),
            "audio_sha256": audio_sha256,
        }
    )


def _float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AlignmentValidationError(f"{label} is not numeric: {value!r}") from exc
    if number < 0:
        raise AlignmentValidationError(f"{label} cannot be negative: {number}")
    if not math.isfinite(number):
        raise AlignmentValidationError(f"{label} must be finite: {number}")
    return number


def strip_emotion_tags(text: str) -> str:
    """Remove inline bracketed ElevenLabs audio tags while keeping narration clean."""
    if "[" not in text:
        return text
    stripped = re.sub(r"\[[a-zA-Z][a-zA-Z\s_-]{0,30}\]\s?", "", text)
    stripped = re.sub(r" {2,}", " ", stripped)
    return stripped.strip()


def reconcile_alignment_characters_with_canonical(
    characters: list[dict],
    canonical_text: str,
) -> list[dict]:
    """Reconcile character alignment spans from emotion-tagged text back to canonical_text."""
    raw_text = "".join(item["text"] for item in characters)
    if raw_text == canonical_text:
        return characters

    tag_indices: set[int] = set()
    for match in EMOTION_TAG_PATTERN.finditer(raw_text):
        tag_indices.update(range(match.start(), match.end()))

    tagless_chars = [
        dict(item)
        for index, item in enumerate(characters)
        if index not in tag_indices
    ]
    tagless_text = "".join(item["text"] for item in tagless_chars)
    if tagless_text == canonical_text:
        for index, item in enumerate(tagless_chars):
            item["index"] = index
        return tagless_chars

    # Never reconcile if non-whitespace text differs; real text substitution must fail.
    if re.sub(r"\s+", "", tagless_text) != re.sub(r"\s+", "", canonical_text):
        return characters

    matcher = difflib.SequenceMatcher(
        None,
        tagless_text,
        canonical_text,
        autojunk=False,
    )
    reconciled: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            reconciled.extend(dict(item) for item in tagless_chars[i1:i2])
        elif tag == "delete":
            if tagless_text[i1:i2].strip() != "":
                return characters
        elif tag == "insert":
            if canonical_text[j1:j2].strip() != "":
                return characters
            if reconciled:
                anchor = float(reconciled[-1]["end"])
            elif i1 < len(tagless_chars):
                anchor = float(tagless_chars[i1]["start"])
            else:
                anchor = 0.0
            for ch in canonical_text[j1:j2]:
                reconciled.append(
                    {
                        "index": 0,
                        "text": ch,
                        "start": anchor,
                        "end": anchor,
                    }
                )
        else:
            return characters

    for index, item in enumerate(reconciled):
        item["index"] = index
    return reconciled


def normalize_provider_alignment(
    raw_alignment: dict,
    canonical_text: str,
) -> tuple[list[dict], list[dict]]:
    if not isinstance(raw_alignment, dict):
        raise AlignmentValidationError("Provider alignment must be an object.")

    provider_words = raw_alignment.get("words", [])
    characters: list[dict] = []

    if {
        "characters",
        "character_start_times_seconds",
        "character_end_times_seconds",
    }.issubset(raw_alignment):
        raw_characters = raw_alignment["characters"]
        starts = raw_alignment["character_start_times_seconds"]
        ends = raw_alignment["character_end_times_seconds"]
        if not (len(raw_characters) == len(starts) == len(ends)):
            raise AlignmentValidationError(
                "Character, start-time, and end-time arrays have different lengths."
            )
        for index, (character, start, end) in enumerate(
            zip(raw_characters, starts, ends, strict=True)
        ):
            character_text = str(character)
            if len(character_text) != 1:
                raise AlignmentValidationError(
                    f"Character {index} must contain exactly one character."
                )
            characters.append(
                {
                    "index": index,
                    "text": character_text,
                    "start": _float(start, f"character {index} start"),
                    "end": _float(end, f"character {index} end"),
                }
            )
    elif isinstance(raw_alignment.get("characters"), list):
        for index, item in enumerate(raw_alignment["characters"]):
            if not isinstance(item, dict):
                raise AlignmentValidationError(
                    f"Forced-alignment character {index} is not an object."
                )
            character_text = str(item.get("text", ""))
            if len(character_text) != 1:
                raise AlignmentValidationError(
                    f"Character {index} must contain exactly one character."
                )
            characters.append(
                {
                    "index": index,
                    "text": character_text,
                    "start": _float(item.get("start"), f"character {index} start"),
                    "end": _float(item.get("end"), f"character {index} end"),
                }
            )
    else:
        raise AlignmentValidationError("Provider response has no character alignment.")

    reconstructed = "".join(item["text"] for item in characters)
    if reconstructed != canonical_text:
        characters = reconcile_alignment_characters_with_canonical(
            characters,
            canonical_text,
        )
        reconstructed = "".join(item["text"] for item in characters)
    if reconstructed != canonical_text:
        raise AlignmentValidationError(
            "Provider alignment text differs from the approved narration "
            f"(expected SHA-256 {sha256_text(canonical_text)}, "
            f"received SHA-256 {sha256_text(reconstructed)})."
        )

    previous_start = 0.0
    previous_end = 0.0
    for item in characters:
        start = item["start"]
        end = item["end"]
        if end < start:
            raise AlignmentValidationError(
                f"Character {item['index']} ends before it starts."
            )
        if start + 0.001 < previous_start or end + 0.001 < previous_end:
            raise AlignmentValidationError(
                f"Character timing is not monotonic at index {item['index']}."
            )
        previous_start = start
        previous_end = end

    if not characters:
        raise AlignmentValidationError("Provider alignment is empty.")
    return characters, provider_words if isinstance(provider_words, list) else []


def merge_timestamp_alignments(
    canonical_text: str,
    chunks: list[tuple[str, dict]],
) -> dict:
    merged_characters: list[str] = []
    merged_starts: list[float] = []
    merged_ends: list[float] = []
    cursor = 0
    offset = 0.0

    for chunk_text, raw_alignment in chunks:
        position = canonical_text.find(chunk_text, cursor)
        if position < 0:
            raise AlignmentValidationError(
                "A pooled TTS chunk cannot be mapped back to the canonical text."
            )
        for character in canonical_text[cursor:position]:
            merged_characters.append(character)
            merged_starts.append(offset)
            merged_ends.append(offset)

        chunk_characters, _ = normalize_provider_alignment(
            raw_alignment,
            chunk_text,
        )
        for item in chunk_characters:
            merged_characters.append(item["text"])
            merged_starts.append(round(offset + item["start"], 6))
            merged_ends.append(round(offset + item["end"], 6))
        offset = merged_ends[-1] if merged_ends else offset
        cursor = position + len(chunk_text)

    for character in canonical_text[cursor:]:
        merged_characters.append(character)
        merged_starts.append(offset)
        merged_ends.append(offset)

    return {
        "characters": merged_characters,
        "character_start_times_seconds": merged_starts,
        "character_end_times_seconds": merged_ends,
    }


def _timed_spans(
    canonical_text: str,
    characters: list[dict],
    pattern: str,
) -> list[dict]:
    spans: list[dict] = []
    for index, match in enumerate(re.finditer(pattern, canonical_text, flags=re.DOTALL)):
        raw_start, raw_end = match.span()
        while raw_start < raw_end and canonical_text[raw_start].isspace():
            raw_start += 1
        while raw_end > raw_start and canonical_text[raw_end - 1].isspace():
            raw_end -= 1
        if raw_start >= raw_end:
            continue
        spans.append(
            {
                "index": index,
                "text": canonical_text[raw_start:raw_end],
                "character_start": raw_start,
                "character_end": raw_end,
                "start": round(characters[raw_start]["start"], 6),
                "end": round(characters[raw_end - 1]["end"], 6),
            }
        )
    return spans


def build_words(canonical_text: str, characters: list[dict]) -> list[dict]:
    words = _timed_spans(canonical_text, characters, r"\S+")
    for index, word in enumerate(words):
        word["word_index"] = index
    return words


def build_sentences(canonical_text: str, characters: list[dict]) -> list[dict]:
    sentences = _timed_spans(
        canonical_text,
        characters,
        r".+?(?:[.!?](?=\s|$)|$)",
    )
    words = build_words(canonical_text, characters)
    for sentence in sentences:
        included = [
            word["word_index"]
            for word in words
            if (
                word["character_start"] >= sentence["character_start"]
                and word["character_end"] <= sentence["character_end"]
            )
        ]
        sentence["word_start"] = included[0] if included else None
        sentence["word_end"] = included[-1] + 1 if included else None
    return sentences


def build_phrases(
    canonical_text: str,
    characters: list[dict],
    *,
    maximum_words: int = 9,
    pause_threshold_seconds: float = 0.32,
) -> list[dict]:
    words = build_words(canonical_text, characters)
    phrases: list[dict] = []
    phrase_start = 0

    for index, word in enumerate(words):
        next_word = words[index + 1] if index + 1 < len(words) else None
        word_count = index - phrase_start + 1
        punctuation_break = bool(re.search(r"[,;:—–.!?]$", word["text"]))
        pause_break = bool(
            next_word
            and next_word["start"] - word["end"] >= pause_threshold_seconds
        )
        if (
            next_word is None
            or word_count >= maximum_words
            or punctuation_break
            or pause_break
        ):
            first = words[phrase_start]
            last = word
            phrases.append(
                {
                    "index": len(phrases),
                    "text": canonical_text[
                        first["character_start"] : last["character_end"]
                    ],
                    "start": first["start"],
                    "end": last["end"],
                    "word_start": phrase_start,
                    "word_end": index + 1,
                    "boundary_reason": (
                        "end"
                        if next_word is None
                        else "pause"
                        if pause_break
                        else "punctuation"
                        if punctuation_break
                        else "maximum_words"
                    ),
                }
            )
            phrase_start = index + 1
    return phrases


def extract_named_entities(text: str) -> dict[str, list[str]]:
    candidates = re.findall(
        r"\b(?:[A-Z][A-Za-z0-9&.-]*)(?:\s+[A-Z][A-Za-z0-9&.-]*){0,3}\b",
        text,
    )
    unique: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip(" .,-")
        if candidate in COMMON_SENTENCE_STARTS or candidate in unique:
            continue
        unique.append(candidate)

    places = [item for item in unique if item in KNOWN_PLACES]
    organizations = [
        item
        for item in unique
        if (
            item in ORGANIZATION_HINTS
            or any(hint in item.split() for hint in ORGANIZATION_HINTS)
            or re.search(r"\b(?:Inc|Corp|Corporation|Labs|University)\b", item)
        )
        and item not in places
    ]
    people = [
        item
        for item in unique
        if len(item.split()) >= 2 and item not in organizations and item not in places
    ]
    products = [
        item
        for item in unique
        if item not in organizations and item not in people and item not in places
    ]
    return {
        "people": people,
        "companies_organizations": organizations,
        "products": products,
        "places": places,
        "unclassified": [],
    }


def suggested_visual_queries(text: str, entities: dict[str, list[str]]) -> list[str]:
    entity_terms = [
        *entities["companies_organizations"],
        *entities["products"],
        *entities["people"],
        *entities["places"],
    ]
    keywords: list[str] = []
    for token in re.findall(r"\b[A-Za-z][A-Za-z0-9-]{3,}\b", text):
        normalized = token.casefold()
        if normalized in STOP_WORDS or normalized in keywords:
            continue
        keywords.append(normalized)
    primary = " ".join([*entity_terms[:2], *keywords[:3]]).strip()
    queries = [primary] if primary else []
    if entity_terms:
        queries.append(f"{entity_terms[0]} technology news")
    return list(dict.fromkeys(query for query in queries if query))


def build_visual_beats(sentences: list[dict]) -> list[dict]:
    beats: list[dict] = []
    for sentence in sentences:
        text = sentence["text"]
        entities = extract_named_entities(text)
        entity_count = sum(len(values) for values in entities.values())
        number_count = len(re.findall(r"\b\d[\d,.%]*\b", text))
        impact_hits = sum(
            1
            for term in HIGH_IMPACT_TERMS
            if re.search(rf"\b{re.escape(term)}\b", text, flags=re.IGNORECASE)
        )
        score = round(
            1.0
            + min(3, entity_count) * 0.7
            + min(2, number_count) * 0.8
            + min(3, impact_hits) * 0.6,
            2,
        )
        beats.append(
            {
                "index": sentence["index"],
                "start": sentence["start"],
                "end": sentence["end"],
                "important_claim": text,
                "named_entities": entities,
                "suggested_visual_queries": suggested_visual_queries(text, entities),
                "importance_score": score,
                "importance": "supporting",
                "selection_reason": {
                    "named_entity_count": entity_count,
                    "number_count": number_count,
                    "impact_term_count": impact_hits,
                },
            }
        )

    strongest = sorted(
        range(len(beats)),
        key=lambda index: (beats[index]["importance_score"], -index),
        reverse=True,
    )[:2]
    for index in strongest:
        beats[index]["importance"] = "must_show"
    return beats


def build_alignment_artifact(
    *,
    story_id: str,
    canonical_text: str,
    raw_alignment: dict,
    method: str,
    audio_sha256: str,
    audio_duration_seconds: float,
    request_id: str = "",
) -> dict:
    characters, provider_words = normalize_provider_alignment(
        raw_alignment,
        canonical_text,
    )
    if audio_duration_seconds <= 0:
        raise AlignmentValidationError("Audio duration must be positive.")

    raw_alignment_end = characters[-1]["end"]
    max_allowed_overshoot = max(2.0, audio_duration_seconds * 0.05)
    if raw_alignment_end - audio_duration_seconds > max_allowed_overshoot:
        raise AlignmentValidationError(
            "Alignment extends beyond the audio by "
            f"{raw_alignment_end - audio_duration_seconds:.3f}s."
        )
    if raw_alignment_end > audio_duration_seconds and raw_alignment_end > 0:
        scale = audio_duration_seconds / raw_alignment_end
        prev_start = 0.0
        prev_end = 0.0
        for item in characters:
            scaled_start = min(
                audio_duration_seconds,
                round(item["start"] * scale, 6),
            )
            scaled_end = min(
                audio_duration_seconds,
                round(item["end"] * scale, 6),
            )
            scaled_start = max(prev_start, scaled_start)
            scaled_end = max(scaled_start, prev_end, scaled_end)
            item["start"] = scaled_start
            item["end"] = scaled_end
            prev_start = scaled_start
            prev_end = scaled_end

    words = build_words(canonical_text, characters)
    sentences = build_sentences(canonical_text, characters)
    phrases = build_phrases(canonical_text, characters)
    beats = build_visual_beats(sentences)

    alignment_start = characters[0]["start"]
    alignment_end = characters[-1]["end"]
    if alignment_end - audio_duration_seconds > 0.1:
        raise AlignmentValidationError(
            "Alignment extends beyond the audio by "
            f"{alignment_end - audio_duration_seconds:.3f}s."
        )
    final_word_end = words[-1]["end"] if words else alignment_end
    if words and alignment_end - final_word_end > 0.1:
        last_word_char_end = int(words[-1]["character_end"])
        for item in characters[last_word_char_end:]:
            item["start"] = min(item["start"], final_word_end)
            item["end"] = min(item["end"], final_word_end)
        alignment_end = characters[-1]["end"]
    final_word_delta = abs(alignment_end - final_word_end)
    if final_word_delta > 0.1:
        raise AlignmentValidationError(
            "The final narration word differs from the alignment end by "
            f"{final_word_delta:.3f}s; maximum is 0.100s."
        )

    return {
        "schema_version": ALIGNMENT_SCHEMA_VERSION,
        "story_id": story_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "request_id": request_id,
        "canonical_text": canonical_text,
        "text_sha256": sha256_text(canonical_text),
        "audio_sha256": audio_sha256,
        "audio_duration_seconds": round(audio_duration_seconds, 6),
        "alignment": {
            "start": round(alignment_start, 6),
            "end": round(alignment_end, 6),
            "duration": round(alignment_end - alignment_start, 6),
        },
        "quality": {
            "status": "passed",
            "reconstructed_text_matches": True,
            "monotonic_timestamps": True,
            "character_count": len(characters),
            "word_count": len(words),
            "sentence_count": len(sentences),
            "phrase_count": len(phrases),
            "final_word_end": round(final_word_end, 6),
            "final_word_to_alignment_end_seconds": round(final_word_delta, 6),
            "alignment_to_audio_end_seconds": round(
                audio_duration_seconds - alignment_end,
                6,
            ),
        },
        "characters": characters,
        "words": words,
        "phrases": phrases,
        "sentences": sentences,
        "visual_beats": beats,
        "provider_words": provider_words,
    }
