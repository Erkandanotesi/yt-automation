from __future__ import annotations

import unittest

from alignment_utils import (
    AlignmentValidationError,
    build_alignment_artifact,
    merge_timestamp_alignments,
)


def proportional_alignment(text: str, duration: float) -> dict:
    unit = duration / len(text)
    return {
        "characters": list(text),
        "character_start_times_seconds": [
            round(index * unit, 6) for index in range(len(text))
        ],
        "character_end_times_seconds": [
            round((index + 1) * unit, 6) for index in range(len(text))
        ],
    }


class AlignmentUtilityTests(unittest.TestCase):
    def test_builds_words_sentences_phrases_and_visual_beats(self) -> None:
        text = (
            "OpenAI announced a privacy update in 2026. "
            "Microsoft said customers can disable it."
        )
        artifact = build_alignment_artifact(
            story_id="story_123456789abc",
            canonical_text=text,
            raw_alignment=proportional_alignment(text, 4.0),
            method="test",
            audio_sha256="a" * 64,
            audio_duration_seconds=4.0,
        )

        self.assertEqual(artifact["quality"]["status"], "passed")
        self.assertEqual(len(artifact["sentences"]), 2)
        self.assertGreater(len(artifact["words"]), 5)
        self.assertGreater(len(artifact["phrases"]), 1)
        self.assertEqual(
            sum(
                beat["importance"] == "must_show"
                for beat in artifact["visual_beats"]
            ),
            2,
        )
        self.assertIn(
            "OpenAI",
            artifact["visual_beats"][0]["named_entities"][
                "companies_organizations"
            ],
        )
        self.assertLessEqual(
            artifact["quality"]["final_word_to_alignment_end_seconds"],
            0.1,
        )

    def test_rejects_provider_text_substitution(self) -> None:
        canonical = "Approved narration."
        changed = "Changed narration."
        with self.assertRaisesRegex(AlignmentValidationError, "differs"):
            build_alignment_artifact(
                story_id="story_123456789abc",
                canonical_text=canonical,
                raw_alignment=proportional_alignment(changed, 1.0),
                method="test",
                audio_sha256="a" * 64,
                audio_duration_seconds=1.0,
            )

    def test_rejects_non_monotonic_timestamps(self) -> None:
        text = "Test."
        raw = proportional_alignment(text, 1.0)
        raw["character_start_times_seconds"][3] = 0.1
        with self.assertRaisesRegex(AlignmentValidationError, "not monotonic"):
            build_alignment_artifact(
                story_id="story_123456789abc",
                canonical_text=text,
                raw_alignment=raw,
                method="test",
                audio_sha256="a" * 64,
                audio_duration_seconds=1.0,
            )

    def test_merges_pooled_chunks_back_to_exact_canonical_text(self) -> None:
        canonical = "Hello world. Next story."
        chunks = [
            ("Hello world.", proportional_alignment("Hello world.", 1.0)),
            ("Next story.", proportional_alignment("Next story.", 1.0)),
        ]
        merged = merge_timestamp_alignments(canonical, chunks)
        self.assertEqual("".join(merged["characters"]), canonical)
        self.assertEqual(len(merged["characters"]), len(canonical))
        self.assertGreaterEqual(
            merged["character_start_times_seconds"][-1],
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
